"""Extract, structurally validate, and patch starting Article inventory slots."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command

from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    IoStoreAssetExtractor,
    MaterializedAsset,
    materialize_extracted_assets,
)
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION
from cv2randomizer.randomizer.initial_inventory import INITIAL_INVENTORY_PACKAGE, InitialInventoryPlan


class InitialInventoryGenerationError(RuntimeError):
    """Raised when the starting inventory source or staged patch is invalid."""


@dataclass(frozen=True, slots=True)
class InitialInventorySlotSnapshot:
    slot_id: str
    article_path: str
    export_index: int
    export_name: str
    property_path: str


@dataclass(frozen=True, slots=True)
class InitialInventoryInspection:
    virtual_package: str
    input_file: Path
    article_path_count: int
    slots: tuple[InitialInventorySlotSnapshot, ...]
    preserved: tuple[InitialInventorySlotSnapshot, ...]


@dataclass(frozen=True, slots=True)
class InitialInventoryPreparation:
    plan: InitialInventoryPlan | None
    source_asset: MaterializedAsset
    source_fingerprint: str
    inspection: InitialInventoryInspection
    motorcycle_unlocked: bool = False


@dataclass(frozen=True, slots=True)
class InitialInventoryGenerationResult:
    plan: InitialInventoryPlan | None
    source_fingerprint: str
    inspection: InitialInventoryInspection
    output_files: tuple[Path, ...]
    patched_slots: int
    reopened: bool
    motorcycle_unlocked: bool = False


class InitialInventorySerializer:
    """Use the shared UAssetAPI helper for the original Initial Inventory asset."""

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
            raise ValueError(f"Initial Inventory serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Inspect prepared game data
    def inspect(self, *, work_root: Path, mappings_path: Path, input_file: Path) -> InitialInventoryInspection:
        root, mappings = self._validate_roots(work_root, mappings_path)
        input_path = input_file.expanduser().resolve(strict=True)
        if not _is_within(input_path, root):
            raise InitialInventoryGenerationError("Initial Inventory source asset must stay inside the generation workspace.")
        result = self._run_json(root, mappings, "inspect-initial-inventory", {
            "virtualPackage": INITIAL_INVENTORY_PACKAGE,
            "inputFile": str(input_path),
        })
        return _parse_inspection(result, input_path)

    # Patch the prepared asset package
    def patch(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        input_file: Path,
        output_file: Path,
        plan: InitialInventoryPlan | None,
        motorcycle_unlocked: bool = False,
    ) -> tuple[InitialInventoryInspection, tuple[Path, ...], int, bool]:
        if type(motorcycle_unlocked) is not bool:
            raise InitialInventoryGenerationError("motorcycle_unlocked must be a boolean.")
        root, mappings = self._validate_roots(work_root, mappings_path)
        input_path = input_file.expanduser().resolve(strict=True)
        output_path = output_file.expanduser().resolve()
        if not _is_within(input_path, root) or not _is_within(output_path, root):
            raise InitialInventoryGenerationError("Initial Inventory source and output must stay inside the generation workspace.")
        document = {
            "virtualPackage": INITIAL_INVENTORY_PACKAGE,
            "inputFile": str(input_path),
            "outputFile": str(output_path),
            "assignments": [
                {
                    "slotId": item.slot_id,
                    "expectedArticlePath": item.vanilla_article_path,
                    "assignedArticlePath": item.assigned_article_path,
                }
                for item in plan.assignments
            ] if plan is not None else [],
            "motorcycleUnlocked": motorcycle_unlocked,
        }
        result = self._run_json(root, mappings, "patch-initial-inventory", document)
        if not isinstance(result, dict) or result.get("reopenedAndValidated") is not True:
            raise InitialInventoryGenerationError("UAssetAPI did not confirm the Initial Inventory output round-trip.")
        raw_files = result.get("outputFiles")
        if not isinstance(raw_files, list) or not raw_files:
            raise InitialInventoryGenerationError("UAssetAPI returned no Initial Inventory output sidecars.")
        output_files = tuple(Path(value).resolve(strict=True) for value in raw_files if isinstance(value, str))
        expected_files = _package_sidecars(output_path)
        if {str(path).casefold() for path in output_files} != {str(path).casefold() for path in expected_files if path.is_file()}:
            raise InitialInventoryGenerationError("UAssetAPI returned an unexpected Initial Inventory sidecar inventory.")
        if any(path.stat().st_size <= 0 for path in output_files):
            raise InitialInventoryGenerationError("Initial Inventory patch produced an empty package sidecar.")
        inspection_raw = result.get("inspection")
        if result.get("motorcycleUnlocked") is not motorcycle_unlocked:
            raise InitialInventoryGenerationError("UAssetAPI did not confirm the requested Motorcycle Forma starting item state.")
        expected_motorcycle_path = (
            "/Game/Blueprint/Article/Item/Important/DA_Item_Important_SpellForMotorcycle"
            if motorcycle_unlocked else None
        )
        if result.get("motorcycleArticlePath") != expected_motorcycle_path:
            raise InitialInventoryGenerationError("UAssetAPI returned an unexpected Motorcycle Forma Article identity.")
        inspection = _parse_inspection(
            inspection_raw, output_path, expected_article_path_count=7 + int(motorcycle_unlocked),
        )
        patched_slots = result.get("patchedSlots")
        expected_patched = len(plan.assignments) if plan is not None else 0
        if patched_slots != expected_patched:
            raise InitialInventoryGenerationError("UAssetAPI patched an unexpected number of starting inventory/Blood Code slots.")
        return inspection, output_files, patched_slots, True

    # Check package paths stay contained
    def _validate_roots(self, work_root: Path, mappings_path: Path) -> tuple[Path, Path]:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise InitialInventoryGenerationError("Initial Inventory helper, workspace, or CV2.usmap mapping is missing.")
        return root, mappings

    # Run helper and parse JSON
    def _run_json(self, root: Path, mappings: Path, operation: str, document: dict[str, Any]) -> Any:
        manifest = root / f".cv2-initial-inventory-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project, self.dotnet_executable, operation,
            str(root), str(mappings), self.engine_version, str(manifest),
        )
        try:
            try:
                completed = self.runner(
                    command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=self.timeout_seconds, cwd=root,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise InitialInventoryGenerationError(f"Could not run UAssetAPI Initial Inventory helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise InitialInventoryGenerationError(
                    f"UAssetAPI {operation} failed: {detail or 'no diagnostic output'}"
                )
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise InitialInventoryGenerationError(f"UAssetAPI {operation} returned no JSON result.")
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError as error:
                raise InitialInventoryGenerationError(f"UAssetAPI {operation} returned invalid JSON.") from error
        finally:
            manifest.unlink(missing_ok=True)


# Prepare initial inventory
def prepare_initial_inventory(
    plan: InitialInventoryPlan | None,
    *,
    game_root: Path,
    work_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: InitialInventorySerializer,
    motorcycle_unlocked: bool = False,
    progress: Callable[[str, int, int], None] | None = None,
) -> InitialInventoryPreparation:
    root = work_root.expanduser().resolve()
    if type(motorcycle_unlocked) is not bool:
        raise InitialInventoryGenerationError("motorcycle_unlocked must be a boolean.")
    try:
        if progress:
            progress("extracting-initial-inventory", 0, 1)
        extracted = extractor.extract_assets(
            game_root, (INITIAL_INVENTORY_PACKAGE,), game_version=game_version,
            data_revision=data_revision, progress=progress,
        )
        by_virtual = {asset.virtual_path.casefold(): asset for asset in extracted}
        source = by_virtual.get(INITIAL_INVENTORY_PACKAGE.casefold())
        if source is None or len(by_virtual) != 1:
            raise InitialInventoryGenerationError("Asset extraction returned a missing or unexpected source package.")
        materialized = materialize_extracted_assets(extracted, root / "source" / "initial-inventory" / "Content")
        assets = {asset.virtual_path.casefold(): asset for asset in materialized}
        source_asset = assets.get(INITIAL_INVENTORY_PACKAGE.casefold())
        if source_asset is None:
            raise InitialInventoryGenerationError("Initial Inventory source package was not materialized.")
        fingerprint = _source_fingerprint(source_asset)
        inspection = serializer.inspect(
            work_root=root, mappings_path=mappings_path, input_file=source_asset.primary_file,
        )
        _validate_inspection_for_plan(inspection, plan)
        if progress:
            progress("inspecting-initial-inventory", 1, 1)
        return InitialInventoryPreparation(plan, source_asset, fingerprint, inspection, motorcycle_unlocked)
    except (AssetExtractionError, OSError, ValueError, KeyError) as error:
        if isinstance(error, InitialInventoryGenerationError):
            raise
        raise InitialInventoryGenerationError(f"Could not prepare Initial Inventory source: {error}") from error


# Apply initial inventory plan
def apply_initial_inventory_plan(
    preparation: InitialInventoryPreparation,
    *,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    serializer: InitialInventorySerializer,
    progress: Callable[[str, int, int], None] | None = None,
) -> InitialInventoryGenerationResult:
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    source_file = preparation.source_asset.primary_file.resolve(strict=True)
    if not _is_within(output_root, root) or output_root in {
        root, (root / "source" / "initial-inventory" / "Content").resolve(),
    }:
        raise InitialInventoryGenerationError("Initial Inventory output must be a separate Content tree inside the workspace.")
    relative = _relative_package_path(INITIAL_INVENTORY_PACKAGE)
    output_file = output_root / relative.with_suffix(".uasset")
    if output_file.exists() or any(path.exists() for path in _package_sidecars(output_file)):
        raise InitialInventoryGenerationError("Refusing to overwrite an existing staged Initial Inventory package.")
    if preparation.plan is None and not preparation.motorcycle_unlocked:
        raise InitialInventoryGenerationError("Initial Inventory apply has no randomized slot to patch.")
    if progress:
        progress("patching-initial-inventory", 0, 1)
    inspection, output_files, patched_slots, reopened = serializer.patch(
        work_root=root, mappings_path=mappings_path, input_file=source_file,
        output_file=output_file, plan=preparation.plan,
        motorcycle_unlocked=preparation.motorcycle_unlocked,
    )
    _validate_inspection_for_plan(
        inspection, preparation.plan, verify_assignments=True,
    )
    if progress:
        progress("patching-initial-inventory", 1, 1)
    return InitialInventoryGenerationResult(
        preparation.plan, preparation.source_fingerprint, inspection, output_files, patched_slots, reopened,
        preparation.motorcycle_unlocked,
    )


# Parse inspection
def _parse_inspection(
    raw: Any, input_file: Path, *, expected_article_path_count: int = 7,
) -> InitialInventoryInspection:
    if not isinstance(raw, dict) or raw.get("virtualPackage") != INITIAL_INVENTORY_PACKAGE:
        raise InitialInventoryGenerationError("UAssetAPI inspected an unexpected Initial Inventory package.")
    try:
        slots = tuple(_parse_snapshot(item) for item in raw["slots"])
        preserved = tuple(_parse_snapshot(item) for item in raw["preserved"])
        count = raw["articlePathCount"]
    except (KeyError, TypeError, ValueError) as error:
        raise InitialInventoryGenerationError("UAssetAPI returned malformed Initial Inventory structure.") from error
    if type(count) is not int or count != expected_article_path_count or len(slots) != 3 or len(preserved) != 4:
        raise InitialInventoryGenerationError("Initial Inventory structure no longer matches the validated source/optional Motorcycle Forma layout.")
    if len({item.slot_id for item in slots}) != 3 or len({item.property_path for item in slots}) != 3:
        raise InitialInventoryGenerationError("Initial Inventory randomized slot inventory is ambiguous.")
    observed_file = Path(raw.get("inputFile", "")).resolve()
    if observed_file != input_file.resolve():
        raise InitialInventoryGenerationError("UAssetAPI inspected an unexpected source file.")
    return InitialInventoryInspection(INITIAL_INVENTORY_PACKAGE, observed_file, count, slots, preserved)


# Parse snapshot
def _parse_snapshot(raw: Any) -> InitialInventorySlotSnapshot:
    if not isinstance(raw, dict):
        raise TypeError("slot snapshot is not an object")
    values = (raw.get("slotId") or raw.get("identity"), raw["articlePath"], raw["exportIndex"], raw["exportName"], raw["propertyPath"])
    if not isinstance(values[0], str) or not isinstance(values[1], str) or type(values[2]) is not int \
            or not isinstance(values[3], str) or not isinstance(values[4], str):
        raise ValueError("slot snapshot has invalid fields")
    return InitialInventorySlotSnapshot(*values)


# Validate inspection for plan
def _validate_inspection_for_plan(
    inspection: InitialInventoryInspection,
    plan: InitialInventoryPlan | None,
    *,
    verify_assignments: bool = False,
) -> None:
    observed = {item.slot_id: item for item in inspection.slots}
    if plan is not None and set(observed) != {item.slot_id for item in plan.assignments}:
        raise InitialInventoryGenerationError("Initial Inventory structural slots do not match the planned three assignments.")
    if plan is not None:
        for assignment in plan.assignments:
            expected = assignment.assigned_article_path if verify_assignments else assignment.vanilla_article_path
            if observed[assignment.slot_id].article_path != expected:
                raise InitialInventoryGenerationError(f"Vanilla source changed for {assignment.display_name}.")
    required_preserved = {
        ("DA_BodyLight.Items", "/Game/Blueprint/Article/Item/DA_BodyLight", "Items[1].1.ArticlePath"),
        ("DA_BodyLight.ItemPalette", "/Game/Blueprint/Article/Item/DA_BodyLight", "ItemPalette[0].0.ArticlePath"),
        ("DA_Item_BasicRecovery", "/Game/Blueprint/Article/Item/Recover/DA_Item_BasicRecovery_1", "Items[0].0.ArticlePath"),
        ("DA_BloodCode_Lou_01", "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01", "BloodCodes[0].0.ArticlePath"),
    }
    observed_preserved = {(item.slot_id, item.article_path, item.property_path) for item in inspection.preserved}
    if observed_preserved != required_preserved:
        raise InitialInventoryGenerationError("A vanilla BodyLight, BasicRecovery, or Lou Blood Code ArticlePath changed.")


# Hash the source files
def _source_fingerprint(asset: MaterializedAsset) -> str:
    digest = hashlib.sha256()
    for path in sorted(asset.files, key=lambda value: value.suffix.casefold()):
        contents = path.read_bytes()
        digest.update(path.suffix.casefold().encode("ascii") + b"\0")
        digest.update(hashlib.sha256(contents).digest())
    return digest.hexdigest()


# Build package sidecars
def _package_sidecars(header: Path) -> tuple[Path, ...]:
    return tuple(Path(str(header.with_suffix("")) + extension) for extension in (".uasset", ".uexp", ".ubulk", ".uptnl"))


# Resolve relative package path
def _relative_package_path(virtual_package: str) -> Path:
    if not virtual_package.startswith("/Game/") or "\\" in virtual_package or virtual_package.endswith("/"):
        raise InitialInventoryGenerationError("Initial Inventory virtual package path is malformed.")
    relative = Path(*virtual_package.split("/")[2:])
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise InitialInventoryGenerationError("Initial Inventory virtual package path is unsafe.")
    return relative


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


__all__ = [
    "InitialInventoryGenerationError",
    "InitialInventoryGenerationResult",
    "InitialInventoryInspection",
    "InitialInventoryPreparation",
    "InitialInventorySerializer",
    "InitialInventorySlotSnapshot",
    "apply_initial_inventory_plan",
    "prepare_initial_inventory",
]
