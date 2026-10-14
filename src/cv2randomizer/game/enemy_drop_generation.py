"""Extract, inspect, plan, and patch the three Enemy Drop DataTables."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
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
from cv2randomizer.game.enemy_drop_catalog import EnemyDropCatalog
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION
from cv2randomizer.randomizer.enemy_drops import (
    EnemyDropPlan,
    EnemyDropPlanError,
    EnemyDropRowSnapshot,
    EnemyDropSlotSnapshot,
    EnemyDropTableSnapshot,
    build_enemy_drop_plan,
    validate_patched_tables,
)


ENEMY_DROP_TABLES: tuple[tuple[str, str], ...] = (
    ("BASE", "/Game/Blueprint/Article/DropItem/DT_DropItemData"),
    ("AFTER_ENDING", "/Game/Blueprint/Article/DropItem/DT_DropItemData_AfterEnding"),
    ("DLC", "/Game/Blueprint/Article/DropItem/DT_DropItemData_DLC"),
)
_TABLE_NAMES = tuple(table for table, _ in ENEMY_DROP_TABLES)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
LOGGER = logging.getLogger(__name__)


class EnemyDropGenerationError(RuntimeError):
    """Raised when source tables or Enemy Drop output fail closed validation."""


@dataclass(frozen=True, slots=True)
class EnemyDropPreparation:
    """Validated original tables and deterministic assignments, before patching."""

    plan: EnemyDropPlan
    catalog: EnemyDropCatalog
    source_tables: tuple[MaterializedAsset, ...]
    original_snapshots: tuple[EnemyDropTableSnapshot, ...]


@dataclass(frozen=True, slots=True)
class EnemyDropGenerationResult:
    """Reopened staged DataTables and the validated assignment counts."""

    plan: EnemyDropPlan
    output_files: tuple[Path, ...]
    patched_tables: tuple[str, ...]
    reopened: bool


class EnemyDropSerializer:
    """Invoke the pinned UAssetAPI helper for DataTable reads and writes."""

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
        self.helper_project = (
            helper_project or resolve_uassetapi_helper()
        ).resolve()
        self.dotnet_executable = dotnet_executable
        self.engine_version = engine_version
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        if engine_version != SUPPORTED_ENGINE_VERSION:
            raise ValueError(f"Enemy Drop serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Inspect tables
    def inspect_tables(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        table_files: Mapping[str, Path],
    ) -> tuple[EnemyDropTableSnapshot, ...]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        _validate_available_tables(tuple(table_files), "inspection")
        requests = []
        for table, _virtual_path in ENEMY_DROP_TABLES:
            if table not in table_files:
                continue
            package = table_files[table].expanduser().resolve(strict=True)
            _validate_table_package(package, root, f"{table} inspection source")
            requests.append({"table": table, "inputFile": str(package)})
        result = self._run_json(
            root,
            mappings,
            "inspect-enemy-drops",
            {"tables": requests},
        )
        expected_tables = tuple(table_files)
        if not isinstance(result, list) or len(result) != len(expected_tables):
            raise EnemyDropGenerationError("UAssetAPI Enemy Drop inspection returned the wrong number of tables.")
        snapshots: list[EnemyDropTableSnapshot] = []
        for expected_table, output in zip(expected_tables, result, strict=True):
            if not isinstance(output, dict) or output.get("table") != expected_table:
                raise EnemyDropGenerationError("UAssetAPI Enemy Drop inspection returned tables in an unexpected order.")
            expected_file = str(table_files[expected_table].resolve())
            if Path(str(output.get("inputFile", ""))).resolve() != Path(expected_file):
                raise EnemyDropGenerationError(f"UAssetAPI returned an unexpected {expected_table} source path.")
            rows_raw = output.get("rows")
            if not isinstance(rows_raw, list):
                raise EnemyDropGenerationError(f"UAssetAPI omitted parsed rows for {expected_table}.")
            rows: list[EnemyDropRowSnapshot] = []
            for row in rows_raw:
                if not isinstance(row, dict):
                    raise EnemyDropGenerationError(f"UAssetAPI returned an invalid {expected_table} row.")
                row_id = _nonempty_string(row.get("rowId"), "rowId")
                slots_raw = row.get("slots")
                if not isinstance(slots_raw, list):
                    raise EnemyDropGenerationError(f"{expected_table}:{row_id} has no slot inspection list.")
                slots: list[EnemyDropSlotSnapshot] = []
                for slot in slots_raw:
                    if not isinstance(slot, dict):
                        raise EnemyDropGenerationError(f"{expected_table}:{row_id} contains an invalid slot.")
                    index = slot.get("slotIndex")
                    if type(index) is not int:
                        raise EnemyDropGenerationError(f"{expected_table}:{row_id} contains a non-integer slot index.")
                    slots.append(EnemyDropSlotSnapshot(
                        expected_table,
                        row_id,
                        index,
                        _fingerprint(slot.get("weightFingerprint"), "Weight"),
                        _fingerprint(slot.get("nonRewardFingerprint"), "non-reward"),
                        _fingerprint(slot.get("bundleFingerprint"), "Articles bundle"),
                        _reward_labels(slot.get("rewardLabels")),
                    ))
                if row.get("slotCount") != len(slots):
                    raise EnemyDropGenerationError(f"{expected_table}:{row_id} reported an inconsistent slot count.")
                rows.append(EnemyDropRowSnapshot(expected_table, row_id, tuple(slots)))
            if output.get("rowCount") != len(rows) or output.get("slotCount") != sum(len(row.slots) for row in rows):
                raise EnemyDropGenerationError(f"{expected_table} reported inconsistent row or slot totals.")
            snapshots.append(EnemyDropTableSnapshot(expected_table, tuple(rows)))
        return tuple(snapshots)

    # Patch tables
    def patch_tables(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        patch_tables: Mapping[str, tuple[Path, Path, tuple[dict[str, Any], ...]]],
        source_tables: Mapping[str, Path] | None = None,
    ) -> list[dict[str, Any]]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        active_names = _validate_available_tables(tuple(patch_tables), "patch", require_base=False)
        selected_sources = source_tables or {table: request[0] for table, request in patch_tables.items()}
        source_names = _validate_available_tables(tuple(selected_sources), "source")
        if not set(active_names).issubset(source_names):
            raise EnemyDropGenerationError("Enemy Drop patch request is missing an active source table.")
        requests = []
        seen_destinations: set[tuple[str, str, int]] = set()
        for table, _virtual_path in ENEMY_DROP_TABLES:
            if table not in patch_tables:
                continue
            input_file, output_file, assignments = patch_tables[table]
            source = input_file.expanduser().resolve(strict=True)
            target = output_file.expanduser().resolve()
            if not _is_within(source, root) or not _is_within(target, root) or source == target:
                raise EnemyDropGenerationError("Enemy Drop input and output packages must be separate files inside the generation workspace.")
            _validate_table_package(source, root, f"{table} patch source")
            if not assignments:
                raise EnemyDropGenerationError(f"{table} patch request has no changed assignments.")
            _validate_assignments(table, assignments, source_names, seen_destinations)
            requests.append({
                "table": table,
                "inputFile": str(source),
                "outputFile": str(target),
                "assignments": list(assignments),
            })
        source_requests = []
        for table, _virtual_path in ENEMY_DROP_TABLES:
            if table not in selected_sources:
                continue
            source = selected_sources[table].expanduser().resolve(strict=True)
            _validate_table_package(source, root, f"{table} assignment source")
            source_requests.append({"table": table, "inputFile": str(source)})
        if not source_requests:
            raise EnemyDropGenerationError("Enemy Drop patch has no available source tables.")
        return self._run_json(
            root, mappings, "patch-enemy-drops",
            {"tables": requests, "sourceTables": source_requests},
        )

    # Check package paths stay contained
    def _validate_roots(self, work_root: Path, mappings_path: Path) -> tuple[Path, Path]:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise EnemyDropGenerationError("Enemy Drop helper, generation workspace, or CV2.usmap mapping is missing.")
        return root, mappings

    # Run helper and parse JSON
    def _run_json(self, root: Path, mappings: Path, operation: str, document: dict[str, Any]) -> Any:
        manifest = root / f".cv2-enemy-drops-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project, self.dotnet_executable, operation,
            str(root), str(mappings), self.engine_version, str(manifest),
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
                raise EnemyDropGenerationError(f"Could not run UAssetAPI Enemy Drop helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                if operation == "patch-enemy-drops":
                    self._save_failure_request(root, document, detail)
                raise EnemyDropGenerationError(f"UAssetAPI {operation} failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise EnemyDropGenerationError(f"UAssetAPI {operation} returned no JSON result.")
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError as error:
                if operation == "patch-enemy-drops":
                    self._save_failure_request(root, document, completed.stdout[-4000:])
                raise EnemyDropGenerationError(f"UAssetAPI {operation} did not return valid JSON.") from error
        finally:
            manifest.unlink(missing_ok=True)

    # Save failure request
    @staticmethod
    def _save_failure_request(root: Path, document: dict[str, Any], detail: str) -> None:
        """Keep a path-sanitized, asset-free helper request for developer diagnosis."""
        diagnostics = root / "logs"
        diagnostics.mkdir(parents=True, exist_ok=True)

        # Resolve safe path
        def safe_path(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            try:
                return Path(value).resolve().relative_to(root.resolve()).as_posix()
            except (OSError, ValueError):
                return Path(value).name

        sanitized = json.loads(json.dumps(document))
        for collection_name in ("tables", "sourceTables"):
            items = sanitized.get(collection_name)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        for key in ("inputFile", "outputFile"):
                            if key in item:
                                item[key] = safe_path(item[key])
        payload = {
            "operation": "patch-enemy-drops",
            "request": sanitized,
            "helper_error": detail[-4000:],
        }
        destination = diagnostics / f"enemy-drop-failure-{uuid.uuid4().hex[:12]}.json"
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        LOGGER.error("Enemy Drop helper request failed; sanitized diagnostic saved to %s", destination)


# Prepare enemy drop plan
def prepare_enemy_drop_plan(
    catalog: EnemyDropCatalog,
    seed: str | int,
    *,
    game_root: Path,
    work_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: EnemyDropSerializer,
    mix_postgame_dlc_with_base: bool = False,
    active_tables: tuple[str, ...] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> EnemyDropPreparation:
    """Extract only the three source DataTables and build the checked assignment plan."""
    root = work_root.expanduser().resolve()
    mappings = mappings_path.expanduser().resolve(strict=True)
    selected_names = (
        _TABLE_NAMES if active_tables is None else _validate_available_tables(tuple(active_tables), "source")
    )
    selected_tables = tuple((table, package) for table, package in ENEMY_DROP_TABLES if table in selected_names)
    if not selected_tables:
        raise EnemyDropGenerationError("No Enemy Drop source table is available.")
    try:
        if progress:
            progress("extracting-enemy-drop-tables", 0, len(selected_tables))
        extracted = extractor.extract_assets(
            game_root,
            (virtual for _table, virtual in selected_tables),
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        if len(extracted) != len(selected_tables):
            raise EnemyDropGenerationError("Asset extraction returned the wrong number of Enemy Drop DataTables.")
        expected_virtuals = {virtual.casefold() for _table, virtual in selected_tables}
        observed_virtuals = {asset.virtual_path.casefold() for asset in extracted}
        if observed_virtuals != expected_virtuals:
            raise EnemyDropGenerationError("Asset extraction returned missing or unexpected Enemy Drop package paths.")
        source_assets = materialize_extracted_assets(extracted, root / "source" / "Content")
        asset_by_virtual = {asset.virtual_path.casefold(): asset for asset in source_assets}
        table_files = {
            table: asset_by_virtual[virtual.casefold()].primary_file
            for table, virtual in selected_tables
        }
        if progress:
            progress("inspecting-enemy-drop-tables", 0, len(selected_tables))
        snapshots = serializer.inspect_tables(work_root=root, mappings_path=mappings, table_files=table_files)
        plan = build_enemy_drop_plan(
            snapshots,
            catalog,
            seed,
            mix_postgame_dlc_with_base=mix_postgame_dlc_with_base,
            active_tables=selected_names,
        )
        if progress:
            progress("inspecting-enemy-drop-tables", len(selected_tables), len(selected_tables))
        return EnemyDropPreparation(
            plan,
            catalog,
            tuple(asset_by_virtual[virtual.casefold()] for _table, virtual in selected_tables),
            snapshots,
        )
    except (AssetExtractionError, EnemyDropPlanError, OSError, ValueError, KeyError) as error:
        if isinstance(error, EnemyDropGenerationError):
            raise
        raise EnemyDropGenerationError(f"Could not prepare Enemy Drop tables: {error}") from error


# Apply enemy drop plan
def apply_enemy_drop_plan(
    preparation: EnemyDropPreparation,
    *,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    serializer: EnemyDropSerializer,
    progress: Callable[[str, int, int], None] | None = None,
) -> EnemyDropGenerationResult:
    """Patch changed eligible rows in the staging Content tree and reopen outputs."""
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    if not _is_within(output_root, root) or output_root == root or output_root in {
        (root / "source" / "Content").resolve(),
    }:
        raise EnemyDropGenerationError("Enemy Drop output must be a separate Content tree inside the generation workspace.")
    output_root.mkdir(parents=True, exist_ok=True)
    active_tables = tuple(
        (table, virtual) for table, virtual in ENEMY_DROP_TABLES
        if table in preparation.plan.active_tables
    )
    active_table_names = preparation.plan.active_tables
    _validate_preparation(preparation, root)
    source_by_table = {
        table: asset for (table, _virtual), asset in zip(active_tables, preparation.source_tables, strict=True)
    }
    before_by_table = {table.table: table for table in preparation.original_snapshots}
    assignments_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    changed_tables: set[str] = set()
    for assignment in preparation.plan.assignments:
        destination = assignment.destination
        source = assignment.source
        if destination.bundle_fingerprint == source.bundle_fingerprint:
            continue
        changed_tables.add(destination.table)
        assignments_by_table[destination.table].append({
            "destinationRowId": destination.row_id,
            "destinationSlotIndex": destination.slot_index,
            "sourceTable": source.table,
            "sourceRowId": source.row_id,
            "sourceSlotIndex": source.slot_index,
            "expectedDestinationFingerprint": destination.bundle_fingerprint,
            "expectedSourceFingerprint": source.bundle_fingerprint,
        })

    output_files: list[Path] = []
    patched_tables: list[str] = []
    added_files: list[Path] = []
    owned_output_headers: list[Path] = []
    after_by_table = dict(before_by_table)
    try:
        patch_requests: dict[str, tuple[Path, Path, tuple[dict[str, Any], ...]]] = {}
        output_by_table: dict[str, Path] = {}
        virtual_by_table = dict(active_tables)
        for table, _virtual in active_tables:
            if table not in changed_tables:
                continue
            source_asset = source_by_table[table]
            source_file = source_asset.primary_file.resolve(strict=True)
            relative = Path(*virtual_by_table[table].split("/")[2:]).with_suffix(source_file.suffix)
            output_file = output_root / relative
            output_file.parent.mkdir(parents=True, exist_ok=True)
            if any(path.exists() for path in _package_files(output_file)):
                raise EnemyDropGenerationError(f"Refusing to overwrite existing staged Enemy Drop table {output_file.name}.")
            output_by_table[table] = output_file
            owned_output_headers.append(output_file)
            patch_requests[table] = (
                source_file,
                output_file,
                tuple(assignments_by_table[table]),
            )

        if patch_requests:
            if progress:
                progress("patching-enemy-drops", 0, len(patch_requests))
            patch_results = serializer.patch_tables(
                work_root=root,
                mappings_path=mappings_path,
                patch_tables=patch_requests,
                source_tables={
                    table: source_by_table[table].primary_file.resolve(strict=True)
                    for table, _virtual in active_tables
                },
            )
            if not isinstance(patch_results, list) or len(patch_results) != len(patch_requests):
                raise EnemyDropGenerationError("UAssetAPI Enemy Drop patch returned the wrong number of table results.")
            result_by_table = {result.get("table"): result for result in patch_results if isinstance(result, dict)}
            if set(result_by_table) != set(patch_requests):
                raise EnemyDropGenerationError("UAssetAPI Enemy Drop patch returned unexpected table results.")
            for table in sorted(patch_requests, key=active_table_names.index):
                result = result_by_table[table]
                expected_input, expected_output, assignments = patch_requests[table]
                if Path(str(result.get("inputFile", ""))).resolve() != expected_input.resolve():
                    raise EnemyDropGenerationError(f"UAssetAPI patched an unexpected {table} source file.")
                if Path(str(result.get("outputFile", ""))).resolve() != expected_output.resolve():
                    raise EnemyDropGenerationError(f"UAssetAPI returned an unexpected {table} output file.")
                output_files_raw = result.get("outputFiles")
                if not isinstance(output_files_raw, list):
                    raise EnemyDropGenerationError(f"UAssetAPI omitted the {table} output package inventory.")
                package_outputs = tuple(Path(str(path)).resolve(strict=True) for path in output_files_raw)
                expected_stem = expected_output.stem.casefold()
                if not package_outputs or any(
                    not _is_within(path, output_root)
                    or path.stem.casefold() != expected_stem
                    or path.suffix.casefold() not in {".uasset", ".uexp", ".ubulk", ".uptnl"}
                    for path in package_outputs
                ):
                    raise EnemyDropGenerationError(f"{table} patcher returned an invalid staged package inventory.")
                added_files.extend(path for path in package_outputs if path not in added_files)
                if expected_output.resolve() not in package_outputs:
                    raise EnemyDropGenerationError(f"{table} patcher omitted its DataTable header.")
                source_asset = source_by_table[table]
                _preserve_additional_sidecars(source_asset, package_outputs, expected_output, added_files)
                if any(path.suffix.casefold() == ".uexp" for path in source_asset.files) and not any(
                    path.suffix.casefold() == ".uexp" for path in package_outputs
                ):
                    raise EnemyDropGenerationError(f"{table} patched output omitted its source .uexp sidecar.")
                added_files.extend(path for path in package_outputs if path not in added_files)
                rows = _parse_rows_from_patch_result(table, result)
                after_by_table[table] = EnemyDropTableSnapshot(table, rows)
                if result.get("rowCount") != len(rows):
                    raise EnemyDropGenerationError(f"{table} patch result reported an inconsistent row count.")
                patched_tables.append(table)
                if progress:
                    progress("patching-enemy-drops", len(patched_tables), len(patch_requests))

        validate_patched_tables(
            preparation.original_snapshots,
            (after_by_table[table] for table in active_table_names),
            preparation.plan,
            preparation.catalog,
        )
        output_files = sorted(set(added_files), key=lambda path: path.as_posix().casefold())
        return EnemyDropGenerationResult(
            preparation.plan,
            tuple(output_files),
            tuple(patched_tables),
            bool(patched_tables),
        )
    except Exception as error:
        cleanup_paths = set(added_files)
        for header in owned_output_headers:
            cleanup_paths.update(path for path in _package_files(header) if path.exists())
        for path in cleanup_paths:
            if _is_within(path, output_root) and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
        if isinstance(error, EnemyDropGenerationError):
            raise
        raise EnemyDropGenerationError(f"Could not apply Enemy Drop plan: {error}") from error


# Parse rows from patch result
def _parse_rows_from_patch_result(table: str, result: dict[str, Any]) -> tuple[EnemyDropRowSnapshot, ...]:
    rows_raw = result.get("rows")
    if not isinstance(rows_raw, list):
        raise EnemyDropGenerationError(f"UAssetAPI omitted reopened {table} DataTable rows.")
    rows: list[EnemyDropRowSnapshot] = []
    for row in rows_raw:
        if not isinstance(row, dict):
            raise EnemyDropGenerationError(f"UAssetAPI returned an invalid reopened {table} row.")
        row_id = _nonempty_string(row.get("rowId"), "rowId")
        slots_raw = row.get("slots")
        if not isinstance(slots_raw, list) or row.get("slotCount") != len(slots_raw):
            raise EnemyDropGenerationError(f"{table}:{row_id} returned an inconsistent reopened slot list.")
        slots = tuple(
            EnemyDropSlotSnapshot(
                table,
                row_id,
                slot.get("slotIndex"),
                _fingerprint(slot.get("weightFingerprint"), "Weight"),
                _fingerprint(slot.get("nonRewardFingerprint"), "non-reward"),
                _fingerprint(slot.get("bundleFingerprint"), "Articles bundle"),
                _reward_labels(slot.get("rewardLabels")),
            )
            for slot in slots_raw
            if isinstance(slot, dict)
        )
        if len(slots) != len(slots_raw) or any(type(slot.slot_index) is not int for slot in slots):
            raise EnemyDropGenerationError(f"{table}:{row_id} contains an invalid reopened slot identity.")
        rows.append(EnemyDropRowSnapshot(table, row_id, slots))
    return tuple(rows)


# Validate preparation
def _validate_preparation(preparation: EnemyDropPreparation, root: Path) -> None:
    active = _validate_available_tables(tuple(preparation.plan.active_tables), "plan")
    snapshot_names = tuple(snapshot.table for snapshot in preparation.original_snapshots)
    if snapshot_names != active:
        raise EnemyDropGenerationError("Enemy Drop preparation snapshots do not match its canonical active table set.")
    source_names = tuple(table for table, _virtual in ENEMY_DROP_TABLES if table in active)
    if len(preparation.source_tables) != len(source_names):
        raise EnemyDropGenerationError("Enemy Drop preparation source package count does not match active tables.")
    slots: dict[tuple[str, str, int], EnemyDropSlotSnapshot] = {}
    for (table, virtual), asset in zip(
        (entry for entry in ENEMY_DROP_TABLES if entry[0] in active), preparation.source_tables, strict=True,
    ):
        if asset.virtual_path.casefold() != virtual.casefold():
            raise EnemyDropGenerationError(f"Enemy Drop source asset does not match the {table} virtual package.")
        _validate_table_package(asset.primary_file.resolve(strict=True), root, f"{table} preparation source")
    for snapshot in preparation.original_snapshots:
        for row in snapshot.rows:
            for slot in row.slots:
                key = (snapshot.table, row.row_id.casefold(), slot.slot_index)
                if key in slots:
                    raise EnemyDropGenerationError("Enemy Drop preparation contains duplicate source slot identities.")
                slots[key] = slot
    destinations: set[tuple[str, str, int]] = set()
    if len(preparation.plan.assignments) != sum(stats.eligible_slots for stats in preparation.plan.table_stats):
        raise EnemyDropGenerationError("Enemy Drop plan assignment count differs from its validated table totals.")
    for assignment in preparation.plan.assignments:
        destination = assignment.destination
        source = assignment.source
        if destination.table not in active or source.table not in active:
            raise EnemyDropGenerationError("Enemy Drop plan references a table outside its active source set.")
        destination_key = (destination.table, destination.row_id.casefold(), destination.slot_index)
        source_key = (source.table, source.row_id.casefold(), source.slot_index)
        if destination_key in destinations:
            raise EnemyDropGenerationError("Enemy Drop plan assigns a destination slot more than once.")
        destinations.add(destination_key)
        if slots.get(destination_key) != destination or slots.get(source_key) != source:
            raise EnemyDropGenerationError("Enemy Drop plan slot identity or fingerprint no longer matches its source snapshot.")


# Validate available tables
def _validate_available_tables(names: tuple[str, ...], label: str, *, require_base: bool = True) -> tuple[str, ...]:
    if not names or len(set(names)) != len(names) or any(name not in _TABLE_NAMES for name in names):
        raise EnemyDropGenerationError(f"Enemy Drop {label} tables are empty, duplicated, or unknown.")
    canonical = tuple(table for table in _TABLE_NAMES if table in names)
    if names != canonical or (require_base and (not canonical or canonical[0] != "BASE")):
        requirement = "BASE plus available optional tables" if require_base else "a canonical subset of destination tables"
        raise EnemyDropGenerationError(f"Enemy Drop {label} tables must use {requirement} in canonical order.")
    return canonical


# Validate table package
def _validate_table_package(path: Path, root: Path, context: str) -> None:
    if path.suffix.casefold() != ".uasset" or not path.is_file() or not _is_within(path, root):
        raise EnemyDropGenerationError(f"{context} must be a .uasset file inside the generation workspace.")
    sidecar = path.with_suffix(".uexp")
    if not sidecar.is_file() or not _is_within(sidecar, root):
        raise EnemyDropGenerationError(f"{context} is missing its required .uexp sidecar.")


# Validate assignments
def _validate_assignments(
    destination_table: str,
    assignments: tuple[dict[str, Any], ...],
    source_tables: tuple[str, ...],
    seen_destinations: set[tuple[str, str, int]],
) -> None:
    if not isinstance(assignments, (tuple, list)):
        raise EnemyDropGenerationError(f"{destination_table} assignments must be a list.")
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            raise EnemyDropGenerationError(f"{destination_table} contains an invalid Enemy Drop assignment.")
        destination_row = assignment.get("destinationRowId")
        source_row = assignment.get("sourceRowId")
        source_table = assignment.get("sourceTable")
        destination_index = assignment.get("destinationSlotIndex")
        source_index = assignment.get("sourceSlotIndex")
        if (
            not isinstance(destination_row, str) or not destination_row.strip()
            or not isinstance(source_row, str) or not source_row.strip()
            or not isinstance(source_table, str) or source_table not in source_tables
            or type(destination_index) is not int or destination_index < 0
            or type(source_index) is not int or source_index < 0
        ):
            raise EnemyDropGenerationError(f"{destination_table} contains an invalid row, source table, or slot index.")
        _fingerprint(assignment.get("expectedDestinationFingerprint"), "destination")
        _fingerprint(assignment.get("expectedSourceFingerprint"), "source")
        key = (destination_table, destination_row.casefold(), destination_index)
        if key in seen_destinations:
            raise EnemyDropGenerationError("Enemy Drop patch request contains a duplicate destination slot.")
        seen_destinations.add(key)


# Preserve additional sidecars
def _preserve_additional_sidecars(
    source_asset: MaterializedAsset,
    generated_files: tuple[Path, ...],
    output_header: Path,
    added_files: list[Path],
) -> None:
    generated_suffixes = {path.suffix.casefold() for path in generated_files}
    for source in source_asset.files:
        suffix = source.suffix.casefold()
        if suffix in {".uasset", ".uexp"} or suffix in generated_suffixes:
            continue
        target = output_header.with_suffix(suffix)
        if target.exists():
            raise EnemyDropGenerationError(f"Refusing to overwrite staged Enemy Drop sidecar {target.name}.")
        temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary)
            if _sha256(temporary) != _sha256(source):
                raise EnemyDropGenerationError(f"Copied Enemy Drop sidecar failed integrity validation: {target.name}.")
            temporary.replace(target)
            added_files.append(target.resolve())
        finally:
            temporary.unlink(missing_ok=True)


# Build package files
def _package_files(header: Path) -> tuple[Path, ...]:
    return tuple(header.with_suffix(extension) for extension in (".uasset", ".uexp", ".ubulk", ".uptnl"))


# Read nonempty string
def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnemyDropGenerationError(f"UAssetAPI returned an invalid {label}.")
    return value


# Calculate the source fingerprint
def _fingerprint(value: Any, label: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise EnemyDropGenerationError(f"UAssetAPI returned an invalid {label} fingerprint.")
    return value


# Return reward labels
def _reward_labels(value: Any) -> tuple[str, ...]:
    """Accept human labels from newer helpers while keeping old test fixtures readable."""
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise EnemyDropGenerationError("UAssetAPI returned invalid Enemy Drop reward labels.")
    return tuple(value)


# Calculate the SHA-256 hash
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
