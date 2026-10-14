"""Make the motorcycle available in new games."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command
from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.core.workspace import CacheManager
from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    materialize_extracted_assets,
)
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION


MOTORCYCLE_UNLOCK_FLAGS_PACKAGE = (
    "/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/DT_StoryFlagDefine_Beginning"
)
MOTORCYCLE_PLAYER_PACKAGE = "/Game/Blueprint/Characters/Player/BP_Player"
MOTORCYCLE_UNLOCK_PACKAGES = (MOTORCYCLE_UNLOCK_FLAGS_PACKAGE, MOTORCYCLE_PLAYER_PACKAGE)
MOTORCYCLE_FORMA_ARTICLE_PACKAGE = (
    "/Game/Blueprint/Article/Item/Important/DA_Item_Important_SpellForMotorcycle"
)
MOTORCYCLE_CACHE_PACKAGES = (*MOTORCYCLE_UNLOCK_PACKAGES, MOTORCYCLE_FORMA_ARTICLE_PACKAGE)
MOTORCYCLE_UNLOCK_FLAG_ROWS = ("BloodMagic_BikeCreator",)
MOTORCYCLE_MOUNT_GATE_PATH = "MountControlComponent.AvailableStoryFlagCondition.EvaluationFormula"
MOTORCYCLE_MOUNT_GATE_VANILLA = "MainEpisode.Beginning >= e10480 || CLear_e10515"
MOTORCYCLE_MOUNT_GATE_UNLOCKED = "BloodMagic_BikeCreator"


class _MotorcycleAssetExtractor(Protocol):
    # Inspect package inventory
    def inspect_package_inventory(self, game_root: Path) -> PackageInventory: ...

    # Extract requested game assets
    def extract_assets(
        self,
        game_root: Path,
        virtual_paths: Iterable[str],
        **kwargs: Any,
    ) -> tuple[Any, ...]: ...


# List inventory for motorcycle unlock packages
def inventory_for_motorcycle_unlock_packages(
    *,
    game_root: Path,
    cached_inventory: PackageInventory,
    inventory_extractor: _MotorcycleAssetExtractor,
) -> PackageInventory:
    """Reuse a cache inventory or query the configured cache-aware extractor."""
    if all(cached_inventory.contains(package) for package in MOTORCYCLE_UNLOCK_PACKAGES):
        return cached_inventory
    return inventory_extractor.inspect_package_inventory(game_root)


class MotorcycleUnlockError(RuntimeError):
    """Raised when a new-game motorcycle unlock override cannot be staged safely."""


@dataclass(frozen=True, slots=True)
class MotorcycleUnlockResult:
    output_files: tuple[Path, ...]
    patched_flags: tuple[str, ...]
    patched_mount_gate: bool
    reopened: bool


class MotorcycleUnlockSerializer:
    """Patch and validate the audited motorcycle unlock assets through UAssetAPI."""

    # Initialize this object
    def __init__(
        self,
        *,
        helper_project: Path | None = None,
        dotnet_executable: str = "dotnet",
        engine_version: str = SUPPORTED_ENGINE_VERSION,
        timeout_seconds: float = 1800,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
    ) -> None:
        self.helper_project = (helper_project or resolve_uassetapi_helper()).resolve()
        self.dotnet_executable = dotnet_executable
        self.engine_version = engine_version
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        if engine_version != SUPPORTED_ENGINE_VERSION:
            raise ValueError(f"Motorcycle unlock serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Patch the prepared asset package
    def patch(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        input_file: Path,
        output_file: Path,
        virtual_package: str = MOTORCYCLE_UNLOCK_FLAGS_PACKAGE,
    ) -> MotorcycleUnlockResult:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        source = input_file.expanduser().resolve(strict=True)
        output = output_file.expanduser().resolve()
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise MotorcycleUnlockError("Motorcycle unlock helper, generation workspace, or CV2.usmap is missing.")
        if not _is_within(output, root) or source == output:
            raise MotorcycleUnlockError("Motorcycle unlock output must be a separate package inside the generation workspace.")
        if virtual_package not in MOTORCYCLE_UNLOCK_PACKAGES:
            raise MotorcycleUnlockError(f"Unexpected motorcycle unlock package: {virtual_package}.")
        document = {
            "virtualPackage": virtual_package,
            "inputFile": str(source),
            "outputFile": str(output),
        }
        manifest = root / f".cv2-motorcycle-unlock-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project,
            self.dotnet_executable,
            "patch-motorcycle-unlock-asset",
            str(root),
            str(mappings),
            self.engine_version,
            str(manifest),
        )
        try:
            try:
                completed = self.runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    cwd=root,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise MotorcycleUnlockError(f"Could not run UAssetAPI motorcycle unlock helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise MotorcycleUnlockError(f"UAssetAPI motorcycle unlock patch failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise MotorcycleUnlockError("UAssetAPI motorcycle unlock patch returned no result.")
            try:
                response: Any = json.loads(lines[-1])
            except json.JSONDecodeError as error:
                raise MotorcycleUnlockError("UAssetAPI motorcycle unlock patch returned invalid JSON.") from error
            if not isinstance(response, dict) or response.get("virtualPackage") != virtual_package:
                raise MotorcycleUnlockError("UAssetAPI returned an unexpected motorcycle unlock package.")
            expected_flags = list(MOTORCYCLE_UNLOCK_FLAG_ROWS) if virtual_package == MOTORCYCLE_UNLOCK_FLAGS_PACKAGE else []
            expected_mount_gate = virtual_package == MOTORCYCLE_PLAYER_PACKAGE
            if (
                response.get("reopenedAndValidated") is not True
                or response.get("patchedFlags") != expected_flags
                or response.get("patchedMountGate") is not expected_mount_gate
            ):
                raise MotorcycleUnlockError("UAssetAPI did not validate all requested motorcycle unlock fields.")
            raw_files = response.get("outputFiles")
            if not isinstance(raw_files, list) or not raw_files:
                raise MotorcycleUnlockError("UAssetAPI returned no staged motorcycle package files.")
            output_files = tuple(Path(value).resolve(strict=True) for value in raw_files if isinstance(value, str))
            expected = {path for path in _package_sidecars(output) if path.is_file()}
            if not output_files or {str(path).casefold() for path in output_files} != {str(path).casefold() for path in expected}:
                raise MotorcycleUnlockError("UAssetAPI returned an unexpected motorcycle package sidecar inventory.")
            if any(not _is_within(path, root) or path.stat().st_size <= 0 for path in output_files):
                raise MotorcycleUnlockError("Motorcycle unlock output is empty or escaped the generation workspace.")
            return MotorcycleUnlockResult(
                output_files,
                tuple(expected_flags),
                expected_mount_gate,
                True,
            )
        finally:
            manifest.unlink(missing_ok=True)


# Apply motorcycle unlock
def apply_motorcycle_unlock(
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: _MotorcycleAssetExtractor,
    package_inventory: PackageInventory,
    serializer: MotorcycleUnlockSerializer,
    progress: Callable[[str, int, int], None] | None = None,
) -> MotorcycleUnlockResult:
    """Extract and stage the new-game unlock flag and the player's actual mount gate."""
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    cache_factory = getattr(extractor, "with_cache", None)
    source_extractor = (
        cache_factory(
            CacheManager(root=root / "motorcycle-unlock-extract", single_entry=True),
            package_inventory=package_inventory,
        )
        if callable(cache_factory) else extractor
    )
    try:
        if progress:
            progress("extracting-motorcycle-unlock-assets", 0, len(MOTORCYCLE_UNLOCK_PACKAGES))
        extracted = source_extractor.extract_assets(
            game_root,
            MOTORCYCLE_UNLOCK_PACKAGES,
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        by_virtual = {asset.virtual_path.casefold(): asset for asset in extracted}
        expected_packages = {package.casefold() for package in MOTORCYCLE_UNLOCK_PACKAGES}
        if set(by_virtual) != expected_packages:
            raise MotorcycleUnlockError("Extraction returned a missing or unexpected motorcycle unlock package.")
        materialized = materialize_extracted_assets(
            extracted,
            root / "source" / "motorcycle-unlock" / "Content",
        )
        materialized_by_virtual = {asset.virtual_path.casefold(): asset for asset in materialized}
        results: list[MotorcycleUnlockResult] = []
        owned_outputs: list[Path] = []
        try:
            for index, virtual_package in enumerate(MOTORCYCLE_UNLOCK_PACKAGES, start=1):
                source_asset = materialized_by_virtual.get(virtual_package.casefold())
                if source_asset is None:
                    raise MotorcycleUnlockError(f"{virtual_package} was not materialized for the motorcycle unlock.")
                relative = Path(*virtual_package.removeprefix("/Game/").split("/"))
                output_file = output_root / relative.with_suffix(".uasset")
                if output_file.exists() or any(path.exists() for path in _package_sidecars(output_file)):
                    raise MotorcycleUnlockError(f"Refusing to overwrite staged motorcycle package {virtual_package}.")
                if progress:
                    progress(f"patching-motorcycle-unlock-package-{index}", index - 1, len(MOTORCYCLE_UNLOCK_PACKAGES))
                result = serializer.patch(
                    work_root=root,
                    mappings_path=mappings_path,
                    input_file=source_asset.primary_file,
                    output_file=output_file,
                    virtual_package=virtual_package,
                )
                owned_outputs.extend(result.output_files)
                results.append(result)
                if progress:
                    progress(f"patching-motorcycle-unlock-package-{index}", index, len(MOTORCYCLE_UNLOCK_PACKAGES))
        except Exception:
            for path in owned_outputs:
                path.unlink(missing_ok=True)
            raise
        return MotorcycleUnlockResult(
            tuple(path for result in results for path in result.output_files),
            tuple(flag for result in results for flag in result.patched_flags),
            any(result.patched_mount_gate for result in results),
            all(result.reopened for result in results),
        )
    except (AssetExtractionError, OSError, ValueError) as error:
        if isinstance(error, MotorcycleUnlockError):
            raise
        raise MotorcycleUnlockError(f"Could not prepare motorcycle unlock flags: {error}") from error


# Build package sidecars
def _package_sidecars(header: Path) -> tuple[Path, ...]:
    return (header, *(header.with_suffix(extension) for extension in (".uexp", ".ubulk", ".uptnl")))


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return path.resolve() != root.resolve()
    except ValueError:
        return False
