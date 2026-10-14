"""Staging and validation for renewable-material Blueprint hierarchies."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command
from .asset_extraction import (
    AssetExtractionError,
    ExtractedAsset,
    IoStoreAssetExtractor,
    MaterializedAsset,
    materialize_extracted_assets,
)
from .catalogs import MaterialLocation, is_world_map_package
from .world_loot_serialization import SUPPORTED_ENGINE_VERSION


MATERIAL_BLUEPRINT_PARENT_PREFIX = "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_"
_GENERATED_MATERIAL_MAP_PREFIXES = (
    "/Game/Map/MainWorld/MainWorld/_Generated_/",
    "/Game/Map/MainWorld/DlcWorld/_Generated_/",
)


class MaterialDependencyError(RuntimeError):
    """Raised when a resource class hierarchy or reset behavior cannot be validated."""


@dataclass(frozen=True, slots=True)
class MaterialClassProfile:
    """Resolved class ancestry and effective serialized reset property."""

    package_path: str
    class_name: str
    direct_parent_package: str | None
    material_hierarchy: bool
    effective_reset_on_yadorigi_rest: bool | None
    eligible: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class MaterialClassValidation:
    """Blueprint packages and profiles computed from real class defaults."""

    content_root: Path
    blueprint_packages: tuple[MaterializedAsset, ...]
    profiles: tuple[MaterialClassProfile, ...]

    # List eligible class packages
    @property
    def eligible_class_packages(self) -> frozenset[str]:
        return frozenset(profile.package_path for profile in self.profiles if profile.eligible)

    # Find invalid class reasons
    @property
    def invalid_class_reasons(self) -> dict[str, str]:
        return {
            profile.package_path.casefold(): profile.reason or "material class validation failed"
            for profile in self.profiles
            if not profile.eligible
        }


@dataclass(frozen=True, slots=True)
class MaterialDependencyStage:
    """Destination maps plus their validated assigned-class Blueprint hierarchy."""

    content_root: Path
    maps: tuple[MaterializedAsset, ...]
    blueprint_packages: tuple[MaterializedAsset, ...]
    profiles: tuple[MaterialClassProfile, ...]


class MaterialDependencyResolver:
    """Extract class assets, walk their Blueprint parents, and resolve inherited reset flags."""

    # Initialize this object
    def __init__(
        self,
        *,
        extractor: IoStoreAssetExtractor | None = None,
        helper_project: Path | None = None,
        dotnet_executable: str = "dotnet",
        engine_version: str = SUPPORTED_ENGINE_VERSION,
        timeout_seconds: float = 1800,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
    ) -> None:
        self.extractor = extractor or IoStoreAssetExtractor()
        self.helper_project = (helper_project or resolve_uassetapi_helper()).resolve()
        self.dotnet_executable = dotnet_executable
        self.engine_version = engine_version
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        if engine_version != SUPPORTED_ENGINE_VERSION:
            raise ValueError(f"Material Blueprint inspection supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Validate classes
    def validate_classes(
        self,
        *,
        game_root: Path,
        locations: Iterable[MaterialLocation],
        work_root: Path,
        mappings_path: Path,
        game_version: str,
        data_revision: str,
        extractor: IoStoreAssetExtractor | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> MaterialClassValidation:
        """Stage every referenced class and ancestor, then resolve effective reset values."""
        root = work_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not mappings.is_file():
            raise MaterialDependencyError("Material mapping file does not exist.")
        if not self.helper_project.is_file():
            raise MaterialDependencyError(f"UAssetAPI helper project was not found: {self.helper_project}")
        expected_names: dict[str, str] = {}
        expected_packages: dict[str, str] = {}
        for location in locations:
            key = location.resource_class_package.casefold()
            previous = expected_names.setdefault(key, location.resource_class_name)
            expected_packages.setdefault(key, location.resource_class_package)
            if previous != location.resource_class_name:
                raise MaterialDependencyError("One material class package has conflicting catalog class names.")
        if not expected_names:
            return MaterialClassValidation(root / "source" / "Content", (), ())

        asset_source = extractor or self.extractor
        content_root = root / "source" / "Content"
        known: dict[str, MaterializedAsset] = {}
        metadata: dict[str, dict[str, Any]] = {}
        pending = set(expected_packages.values())
        try:
            while pending:
                round_packages = sorted(
                    {package for package in pending if package.casefold() not in known},
                    key=str.casefold,
                )
                if not round_packages:
                    break
                if len(known) + len(round_packages) > 4096:
                    raise MaterialDependencyError("Material Blueprint hierarchy exceeded the package safety limit.")
                extracted = asset_source.extract_assets(
                    game_root,
                    round_packages,
                    game_version=game_version,
                    data_revision=data_revision,
                    progress=progress,
                )
                materialized = materialize_extracted_assets(extracted, content_root)
                for asset in materialized:
                    known[asset.virtual_path.casefold()] = asset
                if progress:
                    progress("resolving-material-class-defaults", len(known), 0)
                rows = self._run_blueprint_parents(root, mappings, materialized)
                if not isinstance(rows, list) or len(rows) != len(materialized):
                    raise MaterialDependencyError("UAssetAPI Blueprint-parent count did not match staged assets.")
                pending = set()
                for asset, row in zip(materialized, rows, strict=True):
                    class_name = row.get("classExportName")
                    parent_name = row.get("parentClassName")
                    parent_package = row.get("parentPackage")
                    reset_value = row.get("resetOnYadorigiRest")
                    if not isinstance(class_name, str) or not isinstance(parent_name, str):
                        raise MaterialDependencyError("Blueprint class metadata is incomplete.")
                    if parent_package is not None:
                        parent_package = _validate_blueprint_path(parent_package)
                        if parent_package.casefold() not in known:
                            pending.add(parent_package)
                    if reset_value is not None and type(reset_value) is not bool:
                        raise MaterialDependencyError("Blueprint reset default must be true, false, or absent.")
                    metadata[asset.virtual_path.casefold()] = {
                        "class_name": class_name,
                        "parent_package": parent_package,
                        "reset": reset_value,
                        "parent_class_name": parent_name,
                    }

            missing = set(expected_names) - metadata.keys()
            if missing:
                raise MaterialDependencyError(f"UAssetAPI omitted material classes: {sorted(missing)!r}")
            for package, expected_name in expected_names.items():
                actual_name = metadata[package]["class_name"]
                if actual_name != expected_name:
                    raise MaterialDependencyError(
                        f"Material class name mismatch for {package}: {actual_name} != {expected_name}."
                    )

            profiles = tuple(
                self._profile(expected_packages[key], expected_name, metadata)
                for key, expected_name in sorted(expected_names.items())
            )
            return MaterialClassValidation(
                content_root, tuple(known[key] for key in sorted(known)), profiles,
            )
        except (AssetExtractionError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            if isinstance(error, MaterialDependencyError):
                raise
            raise MaterialDependencyError(f"Could not validate material Blueprint hierarchy: {error}") from error

    # Stage for locations
    def stage_for_locations(
        self,
        *,
        game_root: Path,
        locations: Iterable[MaterialLocation],
        class_locations: Iterable[MaterialLocation],
        work_root: Path,
        mappings_path: Path,
        game_version: str,
        data_revision: str,
        extractor: IoStoreAssetExtractor | None = None,
        prevalidated_class_validation: MaterialClassValidation | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> MaterialDependencyStage:
        """Stage patch destination maps and validate both original and assigned classes."""
        root = work_root.expanduser().resolve()
        mappings = mappings_path.expanduser().resolve(strict=True)
        destination_locations = tuple(locations)
        by_package: dict[str, MaterialLocation] = {}
        for location in destination_locations:
            normalized_package = location.package_path.casefold()
            if (
                not is_world_map_package(location.package_path)
                or not normalized_package.startswith(tuple(
                    prefix.casefold() for prefix in _GENERATED_MATERIAL_MAP_PREFIXES
                ))
            ):
                raise MaterialDependencyError(f"Unsupported material map package: {location.package_path}")
            if PurePosixPath(location.package_path).suffix:
                raise MaterialDependencyError("Material map package paths must not include extensions.")
            by_package.setdefault(location.package_path.casefold(), location)
        content_root = root / "source" / "Content"
        if not by_package:
            return MaterialDependencyStage(content_root, (), (), ())
        asset_source = extractor or self.extractor
        try:
            extracted_maps = asset_source.extract_assets(
                game_root,
                sorted((item.package_path for item in by_package.values()), key=str.casefold),
                game_version=game_version,
                data_revision=data_revision,
                progress=progress,
            )
            if progress:
                progress("materializing-material-maps", len(extracted_maps), len(extracted_maps))
            maps = materialize_extracted_assets(extracted_maps, content_root)
            if prevalidated_class_validation is None:
                class_validation = self.validate_classes(
                    game_root=game_root,
                    locations=class_locations,
                    work_root=root,
                    mappings_path=mappings,
                    game_version=game_version,
                    data_revision=data_revision,
                    extractor=asset_source,
                    progress=progress,
                )
            else:
                if prevalidated_class_validation.content_root.resolve() != content_root.resolve():
                    raise MaterialDependencyError("Prevalidated material profiles belong to another generation workspace.")
                profiles_by_package = {
                    profile.package_path.casefold(): profile
                    for profile in prevalidated_class_validation.profiles
                }
                for location in class_locations:
                    profile = profiles_by_package.get(location.resource_class_package.casefold())
                    if profile is None or profile.class_name != location.resource_class_name:
                        raise MaterialDependencyError(
                            f"Prevalidated material profile is missing or changed: {location.resource_class_package}."
                        )
                class_validation = prevalidated_class_validation
            return MaterialDependencyStage(
                content_root, maps, class_validation.blueprint_packages, class_validation.profiles,
            )
        except (AssetExtractionError, OSError, ValueError) as error:
            if isinstance(error, MaterialDependencyError):
                raise
            raise MaterialDependencyError(f"Could not stage material maps and classes: {error}") from error

    # Build the source profile
    def _profile(
        self,
        package_path: str,
        class_name: str,
        metadata: dict[str, dict[str, Any]],
    ) -> MaterialClassProfile:
        current = package_path.casefold()
        visited: set[str] = set()
        material_hierarchy = False
        direct_parent: str | None = None
        effective_reset: bool | None = None
        for _ in range(64):
            if current in visited:
                raise MaterialDependencyError(f"Blueprint parent cycle at {current}.")
            visited.add(current)
            row = metadata.get(current)
            if row is None:
                raise MaterialDependencyError(f"Blueprint parent package was not staged: {current}.")
            parent = row["parent_package"]
            if current == package_path.casefold():
                direct_parent = parent
            if row["reset"] is not None:
                effective_reset = row["reset"]
                break
            if parent is None:
                break
            parent_name = parent.rsplit("/", 1)[-1]
            if parent.startswith(MATERIAL_BLUEPRINT_PARENT_PREFIX) and parent_name.startswith("BP_PickupMaterial_"):
                material_hierarchy = True
            current = parent.casefold()
        else:
            raise MaterialDependencyError(f"Blueprint parent hierarchy exceeded the depth limit for {package_path}.")

        # Continue ancestry lookup separately so a reset override cannot short-circuit material verification.
        current = package_path.casefold()
        visited.clear()
        for _ in range(64):
            if current in visited:
                raise MaterialDependencyError(f"Blueprint parent cycle at {current}.")
            visited.add(current)
            row = metadata.get(current)
            if row is None or row["parent_package"] is None:
                break
            parent = row["parent_package"]
            parent_name = parent.rsplit("/", 1)[-1]
            if parent.startswith(MATERIAL_BLUEPRINT_PARENT_PREFIX) and parent_name.startswith("BP_PickupMaterial_"):
                material_hierarchy = True
            current = parent.casefold()

        eligible = material_hierarchy and effective_reset is True
        if not material_hierarchy:
            reason = f"class ancestry is outside the renewable material hierarchy (parent {direct_parent or 'native/unknown'})"
        elif effective_reset is False:
            reason = "effective bResetOnYadorigiRest is false"
        elif effective_reset is None:
            reason = "bResetOnYadorigiRest is absent from the staged Blueprint ancestry"
        else:
            reason = None
        return MaterialClassProfile(
            package_path, class_name, direct_parent, material_hierarchy, effective_reset, eligible, reason,
        )

    # Run blueprint parents
    def _run_blueprint_parents(
        self, work_root: Path, mappings: Path, assets: tuple[MaterializedAsset, ...],
    ) -> Any:
        manifest_files = [str(asset.primary_file.resolve()) for asset in assets]
        command = uassetapi_command(
            self.helper_project, self.dotnet_executable,
            "blueprint-parents", str(work_root), str(mappings), self.engine_version, *manifest_files,
        )
        try:
            completed = self.runner(
                command, check=False, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self.timeout_seconds, cwd=work_root,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MaterialDependencyError(f"Could not inspect material Blueprint hierarchy: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-3000:]
            raise MaterialDependencyError(
                f"UAssetAPI material hierarchy inspection failed (exit {completed.returncode}): {detail}"
            )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        try:
            return json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError) as error:
            raise MaterialDependencyError(f"UAssetAPI returned invalid Blueprint hierarchy JSON: {error}") from error


# Validate blueprint path
def _validate_blueprint_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not isinstance(value, str) or "\\" in value or not value.startswith("/Game/")
        or not path.is_absolute() or path.suffix
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        or len(path.parts) < 3
    ):
        raise MaterialDependencyError(f"Invalid generated Blueprint parent package: {value!r}.")
    return "/" + path.as_posix().lstrip("/")
