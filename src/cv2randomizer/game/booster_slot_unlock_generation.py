"""Patch new-game StoryFlags for optional equipment slots."""

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


BOOSTER_SLOT_FLAGS_PACKAGE = (
    "/Game/Blueprint/GameSystem/StoryFlag/StoryFlag/Define/DT_StoryFlagDefine_GameSystem"
)
BOOSTER_SLOT_FLAG_ROWS = ("UnlockEquipSlot_Booster5", "UnlockEquipSlot_Booster6")
SECOND_WEAPON_SLOT_FLAG_ROW = "UnlockEquipSlot_Weapon2"
SUPPORTED_UNLOCK_FLAG_ROWS = (*BOOSTER_SLOT_FLAG_ROWS, SECOND_WEAPON_SLOT_FLAG_ROW)


# Read inventory for booster slot flags
def inventory_for_booster_slot_flags(
    *,
    game_root: Path,
    cached_inventory: PackageInventory,
    installed_extractor: IoStoreAssetExtractor,
) -> PackageInventory:
    """Use the cache inventory when it contains the table, otherwise query IoStore.

    The prepared asset cache inventory contains only extracted source packages;
    optional Generate-time tables may be absent from it while still being present
    in the installed game. IoStoreAssetExtractor reuses its in-memory or persisted
    package index before it needs to enumerate the game's containers again.
    """
    if cached_inventory.contains(BOOSTER_SLOT_FLAGS_PACKAGE):
        return cached_inventory
    return installed_extractor.inspect_package_inventory(game_root)


class BoosterSlotUnlockError(RuntimeError):
    """Raised when an equipment-slot StoryFlag package cannot be patched safely."""


@dataclass(frozen=True, slots=True)
class BoosterSlotUnlockResult:
    output_files: tuple[Path, ...]
    patched_flags: tuple[str, ...]
    reopened: bool


class BoosterSlotUnlockSerializer:
    """Use the pinned UAssetAPI helper to set audited equipment-slot defaults."""

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
            raise ValueError(f"Equipment-slot serialization supports only {SUPPORTED_ENGINE_VERSION}.")
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
        target_flags: tuple[str, ...] = BOOSTER_SLOT_FLAG_ROWS,
    ) -> BoosterSlotUnlockResult:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        source = input_file.expanduser().resolve(strict=True)
        output = output_file.expanduser().resolve()
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise BoosterSlotUnlockError("Equipment-slot helper, generation workspace, or CV2.usmap is missing.")
        if not _is_within(output, root) or source == output:
            raise BoosterSlotUnlockError("Equipment-slot output must be a separate package inside the generation workspace.")
        requested_flags = _validate_target_flags(target_flags)
        document = {
            "virtualPackage": BOOSTER_SLOT_FLAGS_PACKAGE,
            "inputFile": str(source),
            "outputFile": str(output),
            "targetFlags": list(requested_flags),
        }
        manifest = root / f".cv2-booster-slot-unlock-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project,
            self.dotnet_executable,
            "patch-booster-slot-unlocks",
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
                raise BoosterSlotUnlockError(f"Could not run UAssetAPI equipment-slot helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise BoosterSlotUnlockError(f"UAssetAPI equipment-slot patch failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise BoosterSlotUnlockError("UAssetAPI equipment-slot patch returned no result.")
            try:
                response: Any = json.loads(lines[-1])
            except json.JSONDecodeError as error:
                raise BoosterSlotUnlockError("UAssetAPI equipment-slot patch returned invalid JSON.") from error
            if not isinstance(response, dict) or response.get("virtualPackage") != BOOSTER_SLOT_FLAGS_PACKAGE:
                raise BoosterSlotUnlockError("UAssetAPI returned an unexpected equipment-slot package.")
            if response.get("reopenedAndValidated") is not True or response.get("patchedFlags") != list(requested_flags):
                raise BoosterSlotUnlockError("UAssetAPI did not validate every requested equipment-slot flag.")
            raw_files = response.get("outputFiles")
            if not isinstance(raw_files, list) or not raw_files:
                raise BoosterSlotUnlockError("UAssetAPI returned no staged equipment-slot package files.")
            output_files = tuple(Path(value).resolve(strict=True) for value in raw_files if isinstance(value, str))
            expected = {path for path in _package_sidecars(output) if path.is_file()}
            if not output_files or {path.casefold() for path in map(str, output_files)} != {str(path).casefold() for path in expected}:
                raise BoosterSlotUnlockError("UAssetAPI returned an unexpected equipment-slot package sidecar inventory.")
            if any(not _is_within(path, root) or path.stat().st_size <= 0 for path in output_files):
                raise BoosterSlotUnlockError("Equipment-slot output is empty or escaped the generation workspace.")
            return BoosterSlotUnlockResult(output_files, requested_flags, True)
        finally:
            manifest.unlink(missing_ok=True)


# Apply booster slot unlocks
def apply_booster_slot_unlocks(
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    package_inventory: PackageInventory,
    serializer: BoosterSlotUnlockSerializer,
    unlock_all_booster_slots: bool = True,
    unlock_second_weapon: bool = False,
    progress: Callable[[str, int, int], None] | None = None,
) -> BoosterSlotUnlockResult:
    """Patch requested slot defaults, reading the READY cache when available."""
    if type(unlock_all_booster_slots) is not bool or type(unlock_second_weapon) is not bool:
        raise BoosterSlotUnlockError("Equipment-slot unlock choices must be booleans.")
    target_flags = tuple(
        flag for flag, enabled in (
            *((name, unlock_all_booster_slots) for name in BOOSTER_SLOT_FLAG_ROWS),
            (SECOND_WEAPON_SLOT_FLAG_ROW, unlock_second_weapon),
        ) if enabled
    )
    if not target_flags:
        raise BoosterSlotUnlockError("No equipment-slot unlock was requested.")
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    try:
        if progress:
            progress("extracting-equipment-slot-flags", 0, 1)
        # A READY Extract cache supplies CachedAssetExtractor here. If this is
        # an older/incomplete cache, keep the installed extractor's persistent
        # content-addressed cache instead of creating a workspace-only cache.
        extracted = extractor.extract_assets(
            game_root,
            (BOOSTER_SLOT_FLAGS_PACKAGE,),
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        by_virtual = {asset.virtual_path.casefold(): asset for asset in extracted}
        source = by_virtual.get(BOOSTER_SLOT_FLAGS_PACKAGE.casefold())
        if source is None or len(by_virtual) != 1:
            raise BoosterSlotUnlockError("Extraction returned a missing or unexpected StoryFlag DataTable package.")
        materialized = materialize_extracted_assets(
            extracted,
            root / "source" / "booster-slot-unlock" / "Content",
        )
        source_asset = next(
            (asset for asset in materialized if asset.virtual_path.casefold() == BOOSTER_SLOT_FLAGS_PACKAGE.casefold()),
            None,
        )
        if source_asset is None:
            raise BoosterSlotUnlockError("StoryFlag DataTable was not materialized in the generation workspace.")
        relative = Path(*BOOSTER_SLOT_FLAGS_PACKAGE.removeprefix("/Game/").split("/"))
        output_file = output_root / relative.with_suffix(".uasset")
        if output_file.exists() or any(path.exists() for path in _package_sidecars(output_file)):
            raise BoosterSlotUnlockError("Refusing to overwrite an existing staged StoryFlag DataTable package.")
        if progress:
            progress("patching-equipment-slot-flags", 0, 1)
        try:
            result = serializer.patch(
                work_root=root,
                mappings_path=mappings_path,
                input_file=source_asset.primary_file,
                output_file=output_file,
                target_flags=target_flags,
            )
        except Exception:
            for path in _package_sidecars(output_file):
                path.unlink(missing_ok=True)
            raise
        if progress:
            progress("patching-equipment-slot-flags", 1, 1)
        return result
    except (AssetExtractionError, OSError, ValueError) as error:
        if isinstance(error, BoosterSlotUnlockError):
            raise
        raise BoosterSlotUnlockError(f"Could not prepare equipment-slot unlock flags: {error}") from error


# Validate target flags
def _validate_target_flags(target_flags: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(target_flags, tuple) or not target_flags:
        raise BoosterSlotUnlockError("At least one equipment-slot StoryFlag must be requested.")
    if any(not isinstance(flag, str) for flag in target_flags) or len(set(target_flags)) != len(target_flags):
        raise BoosterSlotUnlockError("Equipment-slot StoryFlag names must be unique strings.")
    requested = set(target_flags)
    if not requested <= set(SUPPORTED_UNLOCK_FLAG_ROWS):
        unknown = ", ".join(sorted(requested - set(SUPPORTED_UNLOCK_FLAG_ROWS)))
        raise BoosterSlotUnlockError(f"Unsupported equipment-slot StoryFlag requested: {unknown}.")
    # Keep manifest/helper output ordering stable regardless of caller order.
    return tuple(flag for flag in SUPPORTED_UNLOCK_FLAG_ROWS if flag in requested)


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
