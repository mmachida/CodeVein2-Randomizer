"""Extract, validate, plan, and patch the confirmed mastery Booster DataTable."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Callable
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
from cv2randomizer.game.booster_catalog import (
    BOOSTER_ROW_STRUCT_PACKAGE,
    BOOSTER_TABLE_PACKAGE,
    BoosterCatalog,
    BoosterMasterySlot,
)
from cv2randomizer.game.content_availability import package_path_from_object_path
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION
from cv2randomizer.randomizer.boosters import BoosterPlan, BoosterPlanError, build_booster_plan


class BoosterGenerationError(RuntimeError):
    """Raised when the mastery Booster table or staged output fails validation."""


@dataclass(frozen=True, slots=True)
class BoosterRowSnapshot:
    row_index: int
    row_name: str
    evaluation_formula: str
    mastery_owner: str
    mastery_rank: str
    blood_code_package: str
    blood_code_asset: str
    booster_story_flag: str
    booster_package: str
    booster_asset: str
    count: int
    notification: bool


@dataclass(frozen=True, slots=True)
class BoosterPreparation:
    plan: BoosterPlan
    catalog: BoosterCatalog
    source_asset: MaterializedAsset
    source_file: Path
    original_rows: tuple[BoosterRowSnapshot, ...]


@dataclass(frozen=True, slots=True)
class BoosterGenerationResult:
    plan: BoosterPlan
    output_files: tuple[Path, ...]
    patched_rows: int
    reopened: bool


class BoosterSerializer:
    """Invoke the pinned UAssetAPI helper for the mastery Booster DataTable."""

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
            raise ValueError(f"Booster serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Inspect table
    def inspect_table(self, *, work_root: Path, mappings_path: Path, input_file: Path) -> tuple[BoosterRowSnapshot, ...]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        package = input_file.expanduser().resolve(strict=True)
        if not package.is_file():
            raise BoosterGenerationError("Extracted Booster source package is not a readable file.")
        result = self._run_json(root, mappings, "inspect-boosters", {
            "packages": [{"virtualPackage": BOOSTER_TABLE_PACKAGE, "inputFile": str(package)}],
        })
        if not isinstance(result, dict) or result.get("virtualPackage") != BOOSTER_TABLE_PACKAGE or result.get("rowCount") != 102:
            raise BoosterGenerationError("UAssetAPI Booster inspection returned an unexpected table or row count.")
        if Path(str(result.get("inputFile", ""))).resolve() != package:
            raise BoosterGenerationError("UAssetAPI inspected an unexpected Booster DataTable file.")
        return _parse_rows(result.get("rows"))

    # Patch table
    def patch_table(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        input_file: Path,
        output_file: Path,
        assignments: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        source = input_file.expanduser().resolve(strict=True)
        output = output_file.expanduser().resolve()
        if not _is_within(output, root) or source == output:
            raise BoosterGenerationError("Booster output must be a separate package file inside the generation workspace.")
        if not assignments:
            raise BoosterGenerationError("Booster patch request contains no changed rows.")
        result = self._run_json(root, mappings, "patch-boosters", {
            "packages": [{
                "virtualPackage": BOOSTER_TABLE_PACKAGE,
                "inputFile": str(source),
                "outputFile": str(output),
                "assignments": list(assignments),
            }],
        })
        if not isinstance(result, dict):
            raise BoosterGenerationError("UAssetAPI returned an invalid Booster patch result.")
        response = result
        if response.get("virtualPackage") != BOOSTER_TABLE_PACKAGE or response.get("patchedRows") != len(assignments):
            raise BoosterGenerationError("UAssetAPI reported unexpected Booster patch counts.")
        if Path(str(response.get("inputFile", ""))).resolve() != source or Path(str(response.get("outputFile", ""))).resolve() != output:
            raise BoosterGenerationError("UAssetAPI returned unexpected Booster package paths.")
        if not isinstance(response.get("outputFiles"), list):
            raise BoosterGenerationError("UAssetAPI omitted Booster staged sidecar inventory.")
        response["parsedRows"] = _parse_rows(response.get("rows"))
        return response

    # Check package paths stay contained
    def _validate_roots(self, work_root: Path, mappings_path: Path) -> tuple[Path, Path]:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file() or not self.helper_project.is_file():
            raise BoosterGenerationError("Booster helper, generation workspace, or CV2.usmap mapping is missing.")
        return root, mappings

    # Run helper and parse JSON
    def _run_json(self, root: Path, mappings: Path, operation: str, document: dict[str, Any]) -> Any:
        manifest = root / f".cv2-boosters-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = uassetapi_command(
            self.helper_project, self.dotnet_executable, operation, str(root), str(mappings),
            self.engine_version, str(manifest),
        )
        try:
            try:
                completed = self.runner(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout_seconds, cwd=root)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise BoosterGenerationError(f"Could not run UAssetAPI Booster helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise BoosterGenerationError(f"UAssetAPI {operation} failed: {detail or 'no diagnostic output'}")
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if not lines:
                raise BoosterGenerationError(f"UAssetAPI {operation} returned no JSON result.")
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError as error:
                raise BoosterGenerationError(f"UAssetAPI {operation} did not return valid JSON.") from error
        finally:
            manifest.unlink(missing_ok=True)


# Prepare booster plan
def prepare_booster_plan(
    catalog: BoosterCatalog,
    seed: str | int,
    *,
    game_root: Path,
    work_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: BoosterSerializer,
    mix_postgame_dlc_with_base: bool,
    available_reward_packages: tuple[str, ...] | frozenset[str] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> BoosterPreparation:
    root = work_root.expanduser().resolve()
    mappings = mappings_path.expanduser().resolve(strict=True)
    try:
        requested_packages = (BOOSTER_TABLE_PACKAGE, BOOSTER_ROW_STRUCT_PACKAGE)
        if progress:
            progress("extracting-booster-table", 0, len(requested_packages))
        extracted = extractor.extract_assets(
            game_root,
            requested_packages,
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        extracted_by_virtual = {asset.virtual_path.casefold(): asset for asset in extracted}
        expected_packages = {package.casefold() for package in requested_packages}
        if len(extracted_by_virtual) != len(requested_packages) or set(extracted_by_virtual) != expected_packages:
            raise BoosterGenerationError(
                "Asset extraction returned missing, duplicate, or unexpected Booster table/schema packages."
            )
        assets = materialize_extracted_assets(extracted, root / "source" / "Content")
        assets_by_virtual = {asset.virtual_path.casefold(): asset for asset in assets}
        if len(assets_by_virtual) != len(requested_packages) or set(assets_by_virtual) != expected_packages:
            raise BoosterGenerationError("Booster table/schema materialization returned duplicate or missing packages.")
        source = assets_by_virtual[BOOSTER_TABLE_PACKAGE.casefold()]
        _validate_fingerprint(catalog.source_package_sha256, source)
        materialized_source = source.primary_file.expanduser().resolve(strict=True)
        rows = serializer.inspect_table(work_root=root, mappings_path=mappings, input_file=materialized_source)
        _validate_catalog_rows(catalog.slots, rows)
        available = None if available_reward_packages is None else frozenset(
            package_path_from_object_path(value).casefold() for value in available_reward_packages
        )
        active_catalog = catalog if available is None else replace(
            catalog,
            slots=tuple(
                slot for slot in catalog.slots
                if package_path_from_object_path(slot.booster_article_path).casefold() in available
            ),
        )
        plan = build_booster_plan(
            active_catalog, seed, mix_postgame_dlc_with_base=mix_postgame_dlc_with_base,
        )
        if progress:
            progress("inspecting-booster-table", 1, 1)
        return BoosterPreparation(plan, catalog, source, materialized_source, rows)
    except (AssetExtractionError, BoosterPlanError, OSError, ValueError, KeyError) as error:
        if isinstance(error, BoosterGenerationError):
            raise
        raise BoosterGenerationError(f"Could not prepare Booster mastery table: {error}") from error


# Apply booster plan
def apply_booster_plan(
    preparation: BoosterPreparation,
    *,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    serializer: BoosterSerializer,
    progress: Callable[[str, int, int], None] | None = None,
) -> BoosterGenerationResult:
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    if not _is_within(output_root, root) or output_root == root or output_root == (root / "source" / "Content").resolve():
        raise BoosterGenerationError("Booster output must be a separate Content tree inside the generation workspace.")
    assignments: list[dict[str, Any]] = []
    for item in preparation.plan.assignments:
        destination = item.destination
        assigned = item.assigned_reward
        if destination.original_reward == assigned:
            continue
        assignments.append({
            "rowIndex": destination.row_index,
            "rowName": destination.row_name,
            "expectedFormula": destination.evaluation_formula_original,
            "masteryOwner": destination.mastery_owner,
            "masteryRank": destination.mastery_rank,
            "expectedBloodCodePackage": destination.blood_code_article_path,
            "expectedBloodCodeAsset": destination.blood_code_article_asset,
            "expectedStoryFlag": destination.booster_story_flag,
            "expectedPackage": destination.booster_article_path,
            "expectedAsset": destination.booster_article_asset,
            "expectedCount": destination.count,
            "expectedNotification": destination.b_notification,
            "assignedStoryFlag": assigned.story_flag,
            "assignedPackage": assigned.article_path,
            "assignedAsset": assigned.article_path.rsplit("/", 1)[-1],
        })
    if not assignments:
        return BoosterGenerationResult(preparation.plan, (), 0, True)
    source = preparation.source_file.resolve(strict=True)
    output = output_root.joinpath(*BOOSTER_TABLE_PACKAGE.split("/")[2:]).with_suffix(source.suffix)
    output.parent.mkdir(parents=True, exist_ok=True)
    if any(path.exists() for path in _package_files(output)):
        raise BoosterGenerationError(f"Refusing to overwrite staged Booster table {output.name}.")
    committed: list[Path] = []
    try:
        if progress:
            progress("patching-booster-table", 0, 1)
        result = serializer.patch_table(
            work_root=root, mappings_path=mappings_path, input_file=source, output_file=output,
            assignments=tuple(assignments),
        )
        raw_outputs = result["outputFiles"]
        output_files = tuple(Path(str(path)).resolve(strict=True) for path in raw_outputs)
        if output.resolve() not in output_files or any(
            not _is_within(path, output_root) or path.stem.casefold() != output.stem.casefold()
            or path.suffix.casefold() not in {".uasset", ".uexp", ".ubulk", ".uptnl"}
            for path in output_files
        ):
            raise BoosterGenerationError("UAssetAPI returned an invalid Booster staged package inventory.")
        if any(sidecar.suffix.casefold() == ".uexp" for sidecar in preparation.source_asset.files) and not any(path.suffix.casefold() == ".uexp" for path in output_files):
            raise BoosterGenerationError("Patched Booster DataTable omitted its source .uexp sidecar.")
        _validate_patched_rows(preparation, result["parsedRows"])
        committed.extend(output_files)
        if progress:
            progress("patching-booster-table", 1, 1)
        return BoosterGenerationResult(preparation.plan, tuple(sorted(committed)), len(assignments), True)
    except Exception as error:
        for path in set(committed) | {path for path in _package_files(output) if path.exists()}:
            if _is_within(path, output_root):
                path.unlink(missing_ok=True)
        if isinstance(error, BoosterGenerationError):
            raise
        raise BoosterGenerationError(f"Could not apply Booster plan: {error}") from error


# Parse rows
def _parse_rows(value: Any) -> tuple[BoosterRowSnapshot, ...]:
    if not isinstance(value, list) or len(value) != 102:
        raise BoosterGenerationError("UAssetAPI must return exactly 102 parsed Booster DataTable rows.")
    rows: list[BoosterRowSnapshot] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise BoosterGenerationError("UAssetAPI returned an invalid Booster DataTable row.")
        integer_fields = ("rowIndex", "count")
        if any(type(raw.get(field)) is not int for field in integer_fields) or type(raw.get("notification")) is not bool:
            raise BoosterGenerationError("UAssetAPI returned invalid Booster row scalar fields.")
        rows.append(BoosterRowSnapshot(
            raw["rowIndex"], _str(raw.get("rowName"), "rowName"), _str(raw.get("evaluationFormula"), "evaluationFormula"),
            _str(raw.get("masteryOwner"), "masteryOwner"), _str(raw.get("masteryRank"), "masteryRank"),
            _str(raw.get("bloodCodePackage"), "bloodCodePackage"), _str(raw.get("bloodCodeAsset"), "bloodCodeAsset"),
            _str(raw.get("boosterStoryFlag"), "boosterStoryFlag"), _str(raw.get("boosterPackage"), "boosterPackage"),
            _str(raw.get("boosterAsset"), "boosterAsset"), raw["count"], raw["notification"],
        ))
    if [row.row_index for row in rows] != list(range(102)) or len({row.row_name.casefold() for row in rows}) != 102:
        raise BoosterGenerationError("UAssetAPI returned duplicate Booster rows or changed DataTable ordering.")
    return tuple(rows)


# Validate catalog rows
def _validate_catalog_rows(expected: tuple[BoosterMasterySlot, ...], observed: tuple[BoosterRowSnapshot, ...]) -> None:
    if len(expected) != len(observed):
        raise BoosterGenerationError("Booster source row count does not match its catalog.")
    for catalog_slot, row in zip(expected, observed, strict=True):
        if (row.row_index, row.row_name, row.evaluation_formula, row.mastery_owner, row.mastery_rank,
            row.blood_code_package, row.blood_code_asset, row.booster_story_flag, row.booster_package,
            row.booster_asset, row.count, row.notification) != (
            catalog_slot.row_index, catalog_slot.row_name, catalog_slot.evaluation_formula_original,
            catalog_slot.mastery_owner, catalog_slot.mastery_rank, catalog_slot.blood_code_article_path,
            catalog_slot.blood_code_article_asset, catalog_slot.booster_story_flag,
            catalog_slot.booster_article_path, catalog_slot.booster_article_asset,
            catalog_slot.count, catalog_slot.b_notification,
        ):
            raise BoosterGenerationError(f"Extracted Booster DataTable row differs from its versioned catalog entry: {catalog_slot.row_name}.")


# Validate patched rows
def _validate_patched_rows(preparation: BoosterPreparation, observed: tuple[BoosterRowSnapshot, ...]) -> None:
    if len(observed) != 102 or len(preparation.original_rows) != 102:
        raise BoosterGenerationError("Reopened Booster DataTable must contain all 102 rows.")
    assignment_by_name = {item.destination.row_name.casefold(): item for item in preparation.plan.assignments}
    original_by_name = {row.row_name.casefold(): row for row in preparation.original_rows}
    if len(assignment_by_name) != 102 or len(original_by_name) != 102:
        raise BoosterGenerationError("Booster plan or source snapshots do not contain 102 unique rows.")
    for row in observed:
        original = original_by_name.get(row.row_name.casefold())
        assignment = assignment_by_name.get(row.row_name.casefold())
        if original is None or assignment is None:
            raise BoosterGenerationError(f"Reopened Booster table has an unknown row {row.row_name}.")
        destination = assignment.destination
        reward = assignment.assigned_reward
        # The C# round-trip guard verifies every nested property outside the two permitted identities.
        if (row.row_index, row.mastery_owner, row.mastery_rank, row.blood_code_package, row.blood_code_asset,
            row.booster_story_flag, row.booster_package, row.booster_asset, row.count, row.notification) != (
            original.row_index, original.mastery_owner, original.mastery_rank, original.blood_code_package,
            original.blood_code_asset, reward.story_flag, reward.article_path,
            reward.article_path.rsplit("/", 1)[-1], original.count, original.notification,
        ):
            raise BoosterGenerationError(f"Reopened Booster row identity or reward bundle differs from plan: {row.row_name}.")
        formula_flag = f"{reward.story_flag} == false"
        if row.evaluation_formula.count(formula_flag) != 1:
            raise BoosterGenerationError(f"Reopened Booster formula does not use the assigned reward StoryFlag: {row.row_name}.")
        expected_formula = original.evaluation_formula.replace(f"{original.booster_story_flag} == false", formula_flag, 1)
        if row.evaluation_formula != expected_formula:
            raise BoosterGenerationError(f"Reopened Booster formula changed fields other than the reward flag: {row.row_name}.")


# Check source file fingerprint
def _validate_fingerprint(expected: dict[str, str], asset: MaterializedAsset) -> None:
    actual = {path.suffix.casefold(): _sha256(path) for path in asset.files if path.suffix.casefold() in {".uasset", ".uexp"}}
    if actual != expected:
        raise BoosterGenerationError("Extracted Booster DataTable source fingerprint differs from the catalog.")


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


# Read str
def _str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoosterGenerationError(f"UAssetAPI returned an invalid {label}.")
    return value


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
