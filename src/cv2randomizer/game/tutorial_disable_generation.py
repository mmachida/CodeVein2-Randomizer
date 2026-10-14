"""Mark all game tutorial StoryFlags as already viewed."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command
from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    IoStoreAssetExtractor,
    materialize_extracted_assets,
)
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION


TUTORIAL_FLAG_TABLES = (
    "/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/DT_StoryFlagTutorialDefine",
    "/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/DT_StoryFlagTutorialDefine_DLC",
    "/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/AfterEnding/DT_StoryFlagTutorialDefine_AfterEnding",
)
BASE_TUTORIAL_FLAG_TABLE = TUTORIAL_FLAG_TABLES[0]


class TutorialDisableError(RuntimeError):
    """Raised when tutorial flags cannot be safely patched."""


# Read inventory for tutorial flags
def inventory_for_tutorial_flags(
    *,
    game_root: Path,
    cached_inventory: PackageInventory,
    installed_extractor: IoStoreAssetExtractor,
) -> PackageInventory:
    """Use prepared package availability when complete, otherwise the installed index."""
    # Extract registers all three tables as optional sources. The base table
    # is required by this feature, and its presence means this cache was built
    # with the current tutorial source registry; any absent DLC table is not
    # installed and does not require a fresh IoStore inventory scan.
    if cached_inventory.contains(BASE_TUTORIAL_FLAG_TABLE):
        return cached_inventory
    return installed_extractor.inspect_package_inventory(game_root)


@dataclass(frozen=True, slots=True)
class TutorialTablePatch:
    virtual_package: str
    input_file: Path
    output_file: Path


@dataclass(frozen=True, slots=True)
class TutorialDisableResult:
    output_files: tuple[Path, ...]
    patched_tables: tuple[str, ...]
    patched_flag_count: int
    reopened: bool


class TutorialDisableSerializer:
    """Patch and reopen tutorial StoryFlag DataTables through the pinned UAssetAPI helper."""

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
            raise ValueError(f"Tutorial flag serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Patch the prepared asset package
    def patch(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        tables: tuple[TutorialTablePatch, ...],
    ) -> TutorialDisableResult:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise TutorialDisableError("Tutorial flag helper, generation workspace, or CV2.usmap is missing.")
        validated_tables = _validate_table_patches(root, tables)
        request = {
            "tables": [
                {
                    "virtualPackage": table.virtual_package,
                    "inputFile": str(table.input_file),
                    "outputFile": str(table.output_file),
                }
                for table in validated_tables
            ]
        }
        manifest = root / f".cv2-disable-tutorials-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project,
            self.dotnet_executable,
            "patch-disable-tutorials",
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
                raise TutorialDisableError(f"Could not run UAssetAPI tutorial flag helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise TutorialDisableError(f"UAssetAPI tutorial patch failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise TutorialDisableError("UAssetAPI tutorial flag helper returned no result.")
            try:
                response: Any = json.loads(lines[-1])
            except json.JSONDecodeError as error:
                raise TutorialDisableError("UAssetAPI tutorial flag helper returned invalid JSON.") from error
            expected_tables = [table.virtual_package for table in validated_tables]
            if (
                not isinstance(response, dict)
                or response.get("patchedTables") != expected_tables
                or response.get("reopenedAndValidated") is not True
                or not isinstance(response.get("patchedFlagCount"), int)
                or response["patchedFlagCount"] < len(expected_tables)
            ):
                raise TutorialDisableError("UAssetAPI did not validate every requested tutorial table.")
            raw_files = response.get("outputFiles")
            if not isinstance(raw_files, list) or not raw_files:
                raise TutorialDisableError("UAssetAPI returned no staged tutorial table files.")
            output_files = tuple(Path(value).resolve(strict=True) for value in raw_files if isinstance(value, str))
            expected = {
                path.resolve()
                for table in validated_tables
                for path in _package_sidecars(table.output_file)
                if path.is_file()
            }
            if not output_files or {str(path).casefold() for path in output_files} != {
                str(path).casefold() for path in expected
            }:
                raise TutorialDisableError("UAssetAPI returned an unexpected tutorial package sidecar inventory.")
            if any(not _is_within(path, root) or path.stat().st_size <= 0 for path in output_files):
                raise TutorialDisableError("Tutorial flag output is empty or escaped the generation workspace.")
            return TutorialDisableResult(output_files, tuple(expected_tables), response["patchedFlagCount"], True)
        finally:
            manifest.unlink(missing_ok=True)


# Apply tutorial disable
def apply_tutorial_disable(
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    package_inventory: PackageInventory,
    serializer: TutorialDisableSerializer,
    table_packages: tuple[str, ...],
    progress: Callable[[str, int, int], None] | None = None,
) -> TutorialDisableResult:
    """Stage every installed tutorial flag table and validate its all-seen defaults."""
    if BASE_TUTORIAL_FLAG_TABLE not in table_packages:
        raise TutorialDisableError("The base-game tutorial StoryFlag table is unavailable.")
    if len(set(table_packages)) != len(table_packages) or any(
        package not in TUTORIAL_FLAG_TABLES for package in table_packages
    ):
        raise TutorialDisableError("The installed tutorial table inventory is invalid or duplicated.")
    if any(not package_inventory.contains(package) for package in table_packages):
        raise TutorialDisableError("A requested tutorial StoryFlag table is absent from the installed inventory.")
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    try:
        if progress:
            progress("extracting-tutorial-flags", 0, len(table_packages))
        # IoStoreAssetExtractor keeps this small on-demand set in the same
        # content-addressed app cache used by Generate. Repeated seeds then
        # reuse these exact package bytes; CachedAssetExtractor reads READY.
        extracted = extractor.extract_assets(
            game_root,
            table_packages,
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        by_virtual = {asset.virtual_path.casefold(): asset for asset in extracted}
        if set(by_virtual) != {package.casefold() for package in table_packages}:
            raise TutorialDisableError("Extraction returned a missing or unexpected tutorial StoryFlag table.")
        materialized = materialize_extracted_assets(
            extracted,
            root / "source" / "tutorial-flags" / "Content",
        )
        materialized_by_virtual = {asset.virtual_path.casefold(): asset for asset in materialized}
        patches: list[TutorialTablePatch] = []
        for package in table_packages:
            asset = materialized_by_virtual.get(package.casefold())
            if asset is None:
                raise TutorialDisableError(f"Tutorial StoryFlag source was not materialized: {package}.")
            relative = Path(*package.removeprefix("/Game/").split("/"))
            output_file = output_root / relative.with_suffix(".uasset")
            if output_file.exists() or any(path.exists() for path in _package_sidecars(output_file)):
                raise TutorialDisableError(f"Refusing to overwrite an existing staged tutorial table: {package}.")
            patches.append(TutorialTablePatch(package, asset.primary_file, output_file))
        if progress:
            progress("patching-tutorial-flags", 0, len(patches))
        result = serializer.patch(
            work_root=root,
            mappings_path=mappings_path,
            tables=tuple(patches),
        )
        if progress:
            progress("patching-tutorial-flags", len(patches), len(patches))
        return result
    except (AssetExtractionError, OSError, ValueError) as error:
        if isinstance(error, TutorialDisableError):
            raise
        raise TutorialDisableError(f"Could not prepare tutorial StoryFlag tables: {error}") from error


# Validate table patches
def _validate_table_patches(root: Path, tables: tuple[TutorialTablePatch, ...]) -> tuple[TutorialTablePatch, ...]:
    if not isinstance(tables, tuple) or not tables:
        raise TutorialDisableError("At least one tutorial StoryFlag table must be requested.")
    order = {package: index for index, package in enumerate(TUTORIAL_FLAG_TABLES)}
    if any(not isinstance(table, TutorialTablePatch) or table.virtual_package not in order for table in tables):
        raise TutorialDisableError("Unsupported tutorial StoryFlag table requested.")
    packages = tuple(table.virtual_package for table in tables)
    if len(set(packages)) != len(packages) or BASE_TUTORIAL_FLAG_TABLE not in packages:
        raise TutorialDisableError("Tutorial StoryFlag table requests must be unique and include the base table.")
    if packages != tuple(sorted(packages, key=order.__getitem__)):
        raise TutorialDisableError("Tutorial StoryFlag tables must use their stable package order.")
    for table in tables:
        source = table.input_file.expanduser().resolve(strict=True)
        output = table.output_file.expanduser().resolve()
        if not source.is_file() or not _is_within(source, root) or not _is_within(output, root) or source == output:
            raise TutorialDisableError("Tutorial table inputs and outputs must be separate files inside the generation workspace.")
        if source.suffix.casefold() != ".uasset" or output.suffix.casefold() != ".uasset":
            raise TutorialDisableError("Tutorial StoryFlag table files must use .uasset package headers.")
    return tables


# Build package sidecars
def _package_sidecars(header: Path) -> tuple[Path, ...]:
    return (header, *(header.with_suffix(extension) for extension in (".uexp", ".ubulk", ".uptnl")))


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve(strict=False).relative_to(root.expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True
