"""Extract, validate, randomize, and patch Booster Extractor recipe DataTables."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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
from cv2randomizer.game.booster_reward_pool import BoosterRewardCandidate
from cv2randomizer.game.extractor_catalog import (
    ExtractorCatalog,
    ExtractorCatalogError,
    ExtractorRecipeSlot,
    ExtractorRecipeTable,
)
from cv2randomizer.game.content_availability import package_path_from_object_path
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION
from cv2randomizer.game.version_compatibility import is_game_data_layout_mismatch
from cv2randomizer.randomizer.extractor import ExtractorPlan, ExtractorPlanError, build_extractor_plan


class ExtractorGenerationError(RuntimeError):
    """Raised when recipe source data or generated Extractor output fails validation."""


@dataclass(frozen=True, slots=True)
class ExtractorRowSnapshot:
    row_index: int
    row_name: str
    article_path: str
    snapshot_sha256: str
    immutable_fingerprint_sha256: str
    structural_fingerprint_sha256: str
    raw_material_quantities: tuple[int, ...]
    raw_material_paths: tuple[str, ...]
    haze_cost: int


@dataclass(frozen=True, slots=True)
class ExtractorTableSnapshot:
    virtual_package: str
    input_file: Path
    row_count: int
    rows: tuple[ExtractorRowSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ExtractorPreparation:
    plan: ExtractorPlan | None
    catalog: ExtractorCatalog
    source_assets: tuple[MaterializedAsset, ...]
    source_tables: tuple[ExtractorTableSnapshot, ...]
    incompatible_tables: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractorGenerationResult:
    plan: ExtractorPlan | None
    output_files: tuple[Path, ...]
    patched_rows: int
    table_packages: tuple[str, ...]
    reopened: bool
    one_material_each: bool = False


class ExtractorSerializer:
    """Invoke the pinned UAssetAPI helper for only the two recipe tables."""

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
            raise ValueError(f"Extractor serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Inspect tables
    def inspect_tables(self, *, work_root: Path, mappings_path: Path,
                       package_files: Mapping[str, Path]) -> tuple[ExtractorTableSnapshot, ...]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        request = []
        for virtual_package, path in sorted(package_files.items(), key=lambda item: item[0].casefold()):
            request.append({"virtualPackage": virtual_package, "inputFile": str(path.expanduser().resolve(strict=True))})
        if not request or len(request) > 2:
            raise ExtractorGenerationError("Extractor inspection must request the base table and optionally the DLC table.")
        result = self._run_json(root, mappings, "inspect-extractor", {"packages": request})
        if not isinstance(result, list) or len(result) != len(request):
            raise ExtractorGenerationError("UAssetAPI returned an unexpected Extractor table inventory.")
        parsed = tuple(_parse_table(raw) for raw in result)
        expected = {item["virtualPackage"].casefold(): Path(item["inputFile"]).resolve() for item in request}
        if {table.virtual_package.casefold() for table in parsed} != set(expected):
            raise ExtractorGenerationError("UAssetAPI inspected an unexpected Extractor table package.")
        for table in parsed:
            if table.input_file.resolve() != expected[table.virtual_package.casefold()]:
                raise ExtractorGenerationError("UAssetAPI inspected an unexpected Extractor source file.")
        return parsed

    # Patch tables
    def patch_tables(self, *, work_root: Path, mappings_path: Path,
                     packages: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        result = self._run_json(root, mappings, "patch-extractor", {"packages": list(packages)})
        if not isinstance(result, list) or len(result) != len(packages) or any(not isinstance(item, dict) for item in result):
            raise ExtractorGenerationError("UAssetAPI returned an invalid Extractor patch inventory.")
        return tuple(result)

    # Check package paths stay contained
    def _validate_roots(self, work_root: Path, mappings_path: Path) -> tuple[Path, Path]:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise ExtractorGenerationError("Extractor helper, workspace, or CV2.usmap mapping is missing.")
        return root, mappings

    # Run helper and parse JSON
    def _run_json(self, root: Path, mappings: Path, operation: str, document: dict[str, Any]) -> Any:
        manifest = root / f".cv2-extractor-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project, self.dotnet_executable, operation,
            str(root), str(mappings), self.engine_version, str(manifest),
        )
        try:
            try:
                completed = self.runner(command, check=False, capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=self.timeout_seconds, cwd=root)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ExtractorGenerationError(f"Could not run UAssetAPI Extractor helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise ExtractorGenerationError(f"UAssetAPI {operation} failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise ExtractorGenerationError(f"UAssetAPI {operation} returned no JSON result.")
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError as error:
                raise ExtractorGenerationError(f"UAssetAPI {operation} returned invalid JSON.") from error
        finally:
            manifest.unlink(missing_ok=True)


# Prepare extractor plan
def prepare_extractor_plan(
    catalog: ExtractorCatalog,
    candidates: tuple[BoosterRewardCandidate, ...] = (),
    seed: str | int = "",
    *,
    game_root: Path,
    work_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: ExtractorSerializer,
    mix_postgame_dlc_with_base: bool,
    randomize_recipes: bool = True,
    available_reward_packages: tuple[str, ...] | frozenset[str] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> ExtractorPreparation:
    root = work_root.expanduser().resolve()
    mappings = mappings_path.expanduser().resolve(strict=True)
    tables = catalog.tables
    requested = tuple(table.virtual_package for table in tables)
    if not requested:
        raise ExtractorGenerationError("No available Extractor recipe table was selected.")
    try:
        if progress:
            progress("extracting-extractor-recipe-tables", 0, len(requested))
        extracted = extractor.extract_assets(
            game_root, requested, game_version=game_version, data_revision=data_revision, progress=progress,
        )
        by_virtual = {asset.virtual_path.casefold(): asset for asset in extracted}
        if len(by_virtual) != len(requested) or set(by_virtual) != {item.casefold() for item in requested}:
            raise ExtractorGenerationError("Asset extraction returned a missing or unexpected recipe table.")
        materialized = materialize_extracted_assets(
            extracted, root / "source" / "extractor" / "Content",
        )
        assets_by_virtual = {asset.virtual_path.casefold(): asset for asset in materialized}
        files_by_virtual = {package: assets_by_virtual[package.casefold()].primary_file for package in requested}
        inspect_by_package: dict[str, ExtractorTableSnapshot] = {}
        batch_error: ExtractorGenerationError | None = None
        try:
            batch = serializer.inspect_tables(
                work_root=root, mappings_path=mappings, package_files=files_by_virtual,
            )
            inspect_by_package = {item.virtual_package.casefold(): item for item in batch}
            if set(inspect_by_package) != {package.casefold() for package in requested}:
                raise ExtractorGenerationError("UAssetAPI recipe table set differs from the catalog.")
        except ExtractorGenerationError as error:
            if not is_game_data_layout_mismatch(error):
                raise
            batch_error = error
        compatible_tables: list[ExtractorRecipeTable] = []
        compatible_snapshots: list[ExtractorTableSnapshot] = []
        incompatible_tables: list[str] = []
        for table in tables:
            asset = assets_by_virtual[table.virtual_package.casefold()]
            try:
                _validate_fingerprint(table.source_package_sha256, asset, table.table_name)
                observed = inspect_by_package.get(table.virtual_package.casefold())
                if observed is None and batch_error is not None:
                    isolated = serializer.inspect_tables(
                        work_root=root,
                        mappings_path=mappings,
                        package_files={table.virtual_package: files_by_virtual[table.virtual_package]},
                    )
                    if len(isolated) == 1 and isolated[0].virtual_package.casefold() == table.virtual_package.casefold():
                        observed = isolated[0]
                if observed is None:
                    raise ExtractorGenerationError(f"UAssetAPI omitted the expected recipe table {table.table_name}.")
                _validate_rows(table, observed)
            except (ExtractorGenerationError, ExtractorCatalogError) as error:
                if not is_game_data_layout_mismatch(error):
                    raise
                incompatible_tables.append(table.table_name)
                continue
            compatible_tables.append(table)
            compatible_snapshots.append(observed)
        if not compatible_tables:
            raise ExtractorGenerationError("No Extractor recipe table matches this game's layout.")
        available = None if available_reward_packages is None else frozenset(
            package_path_from_object_path(value).casefold() for value in available_reward_packages
        )
        active_catalog = ExtractorCatalog(
            catalog.revision,
            tuple(
                replace(
                    table,
                    rows=tuple(
                        row for row in table.rows
                        if available is None
                        or package_path_from_object_path(row.original_article_path).casefold() in available
                    ),
                )
                for table in compatible_tables
            ),
        )
        rejected_recipe_prefixes = tuple(
            f"extractor-recipe:{table.virtual_package.casefold()}#"
            for table in tables
            if table.table_name in incompatible_tables
        )
        compatible_candidates = tuple(
            candidate for candidate in candidates
            if not any(
                origin.casefold().startswith(rejected_prefix)
                for origin in candidate.origins
                for rejected_prefix in rejected_recipe_prefixes
            )
        )
        plan = build_extractor_plan(
            active_catalog, compatible_candidates, seed,
            mix_postgame_dlc_with_base=mix_postgame_dlc_with_base,
        ) if randomize_recipes else None
        if progress:
            progress("inspecting-extractor-recipe-tables", len(requested), len(requested))
        return ExtractorPreparation(
            plan,
            active_catalog,
            tuple(assets_by_virtual[item.virtual_package.casefold()] for item in compatible_tables),
            tuple(compatible_snapshots),
            tuple(incompatible_tables),
        )
    except (AssetExtractionError, ExtractorCatalogError, ExtractorPlanError, OSError, ValueError, KeyError) as error:
        if isinstance(error, ExtractorGenerationError):
            raise
        raise ExtractorGenerationError(f"Could not prepare Booster Extractor tables: {error}") from error


# Apply extractor plan
def apply_extractor_plan(
    preparation: ExtractorPreparation,
    *,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    serializer: ExtractorSerializer,
    one_material_each: bool = False,
    progress: Callable[[str, int, int], None] | None = None,
) -> ExtractorGenerationResult:
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    if not _is_within(output_root, root) or output_root in {root, (root / "source" / "extractor" / "Content").resolve()}:
        raise ExtractorGenerationError("Extractor output must be a separate Content tree inside the workspace.")
    source_by_virtual = {asset.virtual_path.casefold(): asset for asset in preparation.source_assets}
    rows_by_virtual = {table.virtual_package.casefold(): table for table in preparation.source_tables}
    assignments_by_virtual: dict[str, list[dict[str, Any]]] = {}
    for assignment in (preparation.plan.assignments if preparation.plan is not None else ()):
        destination = assignment.destination
        if destination.original_article_path.casefold() == assignment.assigned_reward.article_path.casefold():
            continue
        assignments_by_virtual.setdefault(destination.virtual_package, []).append({
            "rowIndex": destination.row_index,
            "rowName": destination.row_name,
            "expectedArticlePath": destination.original_article_path,
            "expectedImmutableFingerprint": destination.immutable_fingerprint_sha256,
            "assignedArticlePath": assignment.assigned_reward.article_path,
        })
    requests: list[dict[str, Any]] = []
    output_by_virtual: dict[str, Path] = {}
    owned_headers: list[Path] = []
    requested_virtuals = set(assignments_by_virtual)
    if one_material_each:
        requested_virtuals.update(
            table.virtual_package for table in preparation.source_tables
            if any(any(quantity != 1 for quantity in row.raw_material_quantities) for row in table.rows)
        )
    for virtual in sorted(requested_virtuals, key=str.casefold):
        assignments = assignments_by_virtual.get(virtual, [])
        source_asset = source_by_virtual.get(virtual.casefold())
        source_snapshot = rows_by_virtual.get(virtual.casefold())
        if source_asset is None or source_snapshot is None:
            raise ExtractorGenerationError(f"No inspected Extractor source exists for {virtual}.")
        source = source_asset.primary_file.resolve(strict=True)
        output = output_root.joinpath(*virtual.split("/")[2:]).with_suffix(source.suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        if any(path.exists() for path in _package_files(output)):
            raise ExtractorGenerationError(f"Refusing to overwrite staged Extractor package {output.name}.")
        output_by_virtual[virtual] = output
        owned_headers.append(output)
        table = preparation.catalog.table(virtual)
        if table is None:
            raise ExtractorGenerationError(f"Extractor table {virtual} is absent from the versioned catalog.")
        requests.append({
            "virtualPackage": virtual,
            "inputFile": str(source),
            "outputFile": str(output),
            "expectedRowCount": table.expected_row_count,
            "oneMaterialEach": one_material_each,
            "expectedRows": [
                {
                    "rowIndex": row.row_index,
                    "rowName": row.row_name,
                    "expectedArticlePath": row.article_path,
                    "expectedImmutableFingerprint": row.immutable_fingerprint_sha256,
                }
                for row in source_snapshot.rows
            ],
            "assignments": assignments,
        })
    committed: list[Path] = []
    patched_rows = 0
    try:
        if requests:
            if progress:
                progress("patching-extractor-recipe-tables", 0, len(requests))
            raw_results = serializer.patch_tables(work_root=root, mappings_path=mappings_path, packages=tuple(requests))
            by_virtual = {item.get("virtualPackage"): item for item in raw_results}
            if set(by_virtual) != set(output_by_virtual):
                raise ExtractorGenerationError("UAssetAPI patched unexpected Extractor recipe tables.")
            for index, (virtual, output) in enumerate(output_by_virtual.items(), start=1):
                result = by_virtual[virtual]
                source = source_by_virtual[virtual.casefold()].primary_file.resolve(strict=True)
                if (Path(str(result.get("inputFile", ""))).resolve() != source
                        or Path(str(result.get("outputFile", ""))).resolve() != output
                        or result.get("rowCount") != preparation.catalog.table(virtual).expected_row_count):
                    raise ExtractorGenerationError(f"UAssetAPI returned invalid staged paths or row count for {virtual}.")
                output_paths = result.get("outputFiles")
                if not isinstance(output_paths, list):
                    raise ExtractorGenerationError(f"UAssetAPI omitted sidecars for {virtual}.")
                files = tuple(Path(str(path)).resolve(strict=True) for path in output_paths)
                if output.resolve() not in files or any(
                    not _is_within(path, output_root) or path.stem.casefold() != output.stem.casefold()
                    or path.suffix.casefold() not in {".uasset", ".uexp", ".ubulk", ".uptnl"} for path in files
                ):
                    raise ExtractorGenerationError(f"UAssetAPI returned an invalid output inventory for {virtual}.")
                if any(source_file.suffix.casefold() == ".uexp" for source_file in source_by_virtual[virtual.casefold()].files) and not any(
                    path.suffix.casefold() == ".uexp" for path in files
                ):
                    raise ExtractorGenerationError(f"Patched Extractor table {virtual} is missing its .uexp sidecar.")
                changed_rows = _validate_patched_table(
                    preparation, virtual, result.get("rows"), one_material_each=one_material_each,
                )
                if type(result.get("patchedRows")) is not int or result["patchedRows"] != changed_rows:
                    raise ExtractorGenerationError(f"UAssetAPI returned an invalid patched-row count for {virtual}.")
                patched_rows += changed_rows
                committed.extend(files)
                if progress:
                    progress("patching-extractor-recipe-tables", index, len(requests))
        if len(committed) != len(set(committed)):
            raise ExtractorGenerationError("Extractor patcher returned duplicate output files.")
        return ExtractorGenerationResult(
            preparation.plan,
            tuple(sorted(committed)),
            patched_rows,
            tuple(sorted(output_by_virtual, key=str.casefold)),
            True,
            one_material_each,
        )
    except Exception as error:
        cleanup = set(committed)
        for header in owned_headers:
            cleanup.update(path for path in _package_files(header) if path.exists())
        for path in cleanup:
            if _is_within(path, output_root):
                path.unlink(missing_ok=True)
        if isinstance(error, ExtractorGenerationError):
            raise
        raise ExtractorGenerationError(f"Could not apply Booster Extractor plan: {error}") from error


# Parse table
def _parse_table(value: Any) -> ExtractorTableSnapshot:
    if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
        raise ExtractorGenerationError("UAssetAPI returned an invalid Extractor table.")
    virtual = _string(value.get("virtualPackage"), "virtualPackage")
    path = Path(_string(value.get("inputFile"), "inputFile")).resolve()
    count = value.get("rowCount")
    if type(count) is not int or count != len(value["rows"]):
        raise ExtractorGenerationError("UAssetAPI returned an invalid Extractor row count.")
    rows: list[ExtractorRowSnapshot] = []
    for raw in value["rows"]:
        if not isinstance(raw, dict) or type(raw.get("rowIndex")) is not int:
            raise ExtractorGenerationError("UAssetAPI returned an invalid Extractor row entry.")
        if type(raw.get("hazeCost")) is not int:
            raise ExtractorGenerationError("UAssetAPI returned an invalid Extractor Haze cost.")
        rows.append(ExtractorRowSnapshot(raw["rowIndex"], _string(raw.get("rowName"), "rowName"),
            _string(raw.get("articlePath"), "articlePath"), _sha(raw.get("snapshotSha256"), "snapshotSha256"),
            _sha(raw.get("immutableFingerprintSha256"), "immutableFingerprintSha256"),
            _sha(raw.get("structuralFingerprintSha256"), "structuralFingerprintSha256"),
            _int_tuple(raw.get("rawMaterialQuantities"), "rawMaterialQuantities"),
            _string_tuple(raw.get("rawMaterialPaths"), "rawMaterialPaths"), raw["hazeCost"]))
        if len(rows[-1].raw_material_paths) != len(rows[-1].raw_material_quantities):
            raise ExtractorGenerationError("UAssetAPI returned mismatched Booster ingredient paths and quantities.")
    if [row.row_index for row in rows] != list(range(count)) or len({row.row_name.casefold() for row in rows}) != count:
        raise ExtractorGenerationError("UAssetAPI returned duplicate Extractor rows or changed row order.")
    return ExtractorTableSnapshot(virtual, path, count, tuple(rows))


# Validate rows
def _validate_rows(table: ExtractorRecipeTable, observed: ExtractorTableSnapshot) -> None:
    if observed.virtual_package.casefold() != table.virtual_package.casefold() or observed.row_count != table.expected_row_count:
        raise ExtractorGenerationError(f"Extracted table shape differs from the catalog: {table.table_name}.")
    for expected, row in zip(table.rows, observed.rows, strict=True):
        if (expected.row_index, expected.row_name, expected.original_article_path, expected.immutable_fingerprint_sha256) != (
            row.row_index, row.row_name, row.article_path, row.immutable_fingerprint_sha256
        ):
            raise ExtractorGenerationError(f"Extracted row differs from the versioned catalog: {table.table_name}:{expected.row_name}.")


# Validate patched table
def _validate_patched_table(
    preparation: ExtractorPreparation,
    virtual: str,
    raw_rows: Any,
    *,
    one_material_each: bool = False,
) -> int:
    table = preparation.catalog.table(virtual)
    if table is None or not isinstance(raw_rows, list) or len(raw_rows) != table.expected_row_count:
        raise ExtractorGenerationError(f"Reopened Extractor table {virtual} has an invalid row inventory.")
    original = next(
        (item for item in preparation.source_tables if item.virtual_package.casefold() == virtual.casefold()), None,
    )
    if original is None:
        raise ExtractorGenerationError(f"Reopened Extractor table {virtual} has no inspected source.")
    reopened = _parse_table({
        "virtualPackage": virtual, "inputFile": str(original.input_file),
        "rowCount": table.expected_row_count, "rows": raw_rows,
    })
    assigned = {
        item.destination.row_name.casefold(): item.assigned_reward.article_path
        for item in (preparation.plan.assignments if preparation.plan is not None else ())
        if item.destination.virtual_package.casefold() == virtual.casefold()
    }
    for source_row, row in zip(original.rows, reopened.rows, strict=True):
        name = row.row_name.casefold()
        if row.row_name != source_row.row_name:
            raise ExtractorGenerationError("Reopened Extractor table changed row identities or ordering.")
        if one_material_each:
            if row.structural_fingerprint_sha256 != source_row.structural_fingerprint_sha256:
                raise ExtractorGenerationError(f"Reopened Extractor row changed fields outside its reward and extraction costs: {name}.")
            if row.raw_material_quantities != (1,) * len(source_row.raw_material_quantities):
                raise ExtractorGenerationError(f"Reopened Extractor row does not have a 1-per-material cost: {name}.")
            if row.haze_cost != source_row.haze_cost:
                raise ExtractorGenerationError(f"Reopened Extractor row changed its Haze cost: {name}.")
        elif row.immutable_fingerprint_sha256 != source_row.immutable_fingerprint_sha256:
            raise ExtractorGenerationError(f"Reopened Extractor row changed protected fields: {name}.")
        if row.article_path != assigned.get(name, source_row.article_path):
            raise ExtractorGenerationError(f"Reopened Extractor reward differs from the generated plan: {name}.")
    return sum(source.snapshot_sha256 != row.snapshot_sha256 for source, row in zip(original.rows, reopened.rows, strict=True))


# Check source file fingerprint
def _validate_fingerprint(expected: dict[str, str], asset: MaterializedAsset, table_name: str) -> None:
    actual = {path.suffix.casefold(): _sha256(path) for path in asset.files if path.suffix.casefold() in {".uasset", ".uexp"}}
    if actual != expected:
        raise ExtractorGenerationError(f"Extracted {table_name} source hashes differ from the current catalog.")


# Build package files
def _package_files(header: Path) -> tuple[Path, ...]:
    return tuple(header.with_suffix(extension) for extension in (".uasset", ".uexp", ".ubulk", ".uptnl"))


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


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtractorGenerationError(f"UAssetAPI returned invalid {label}.")
    return value


# Read SHA
def _sha(value: Any, label: str) -> str:
    result = _string(value, label).casefold()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ExtractorGenerationError(f"UAssetAPI returned invalid SHA-256 for {label}.")
    return result


# Parse int tuple
def _int_tuple(value: Any, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        raise ExtractorGenerationError(f"UAssetAPI returned invalid {label}.")
    return tuple(value)


# Parse string tuple
def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ExtractorGenerationError(f"UAssetAPI returned invalid {label}.")
    return tuple(value)
