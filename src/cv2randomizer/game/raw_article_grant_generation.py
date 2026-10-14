"""Patch explicitly researched direct Article grants inside the World Loot flow."""

from __future__ import annotations

import json
import hashlib
import subprocess
import uuid
import time
from collections.abc import Callable
from collections import defaultdict
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
from cv2randomizer.game.blood_code_generation import BloodCodeGrantSnapshot, BloodCodeSerializer
from cv2randomizer.game.catalogs import (
    SUPPORTED_DIRECT_ARTICLE_GRANT_DESTINATIONS,
    QuestArticleGrantLayout,
    RawArticleGrantLayout,
    WorldLootLocation,
)
from cv2randomizer.game.world_loot_serialization import SUPPORTED_ENGINE_VERSION
from cv2randomizer.randomizer.world_loot import WorldLootAssignment, WorldLootRandomizationPlan


SUPPORTED_DIRECT_GRANT_DESTINATIONS = SUPPORTED_DIRECT_ARTICLE_GRANT_DESTINATIONS


class RawArticleGrantGenerationError(RuntimeError):
    """Raised when a researched direct reward slot does not match its strict layout."""


@dataclass(frozen=True, slots=True)
class RawArticleGrantSnapshot:
    virtual_package: str
    input_file: Path
    export_index: int
    export_name: str
    serial_size: int
    path_name_index: int
    path_number: int
    object_name_index: int
    object_number: int
    path_name_map_value: str
    object_name_map_value: str
    article_path: str


@dataclass(frozen=True, slots=True)
class WorldLootDirectGrantResult:
    destination_id: str
    display_name: str
    original_article_path: str
    assigned_article_path: str
    changed: bool
    output_files: tuple[Path, ...]
    reopened_and_validated: bool


class RawArticleGrantSerializer:
    """Invoke the shared UAssetAPI helper for explicitly allowlisted RawExport destinations."""

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
            raise ValueError(f"Raw Article grant serialization supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Inspect prepared game data
    def inspect(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        slot: WorldLootLocation,
        input_file: Path,
    ) -> RawArticleGrantSnapshot:
        root, mappings = self._validate_roots(work_root, mappings_path)
        source = input_file.resolve(strict=True)
        if not _is_within(source, root) or not source.is_file():
            raise RawArticleGrantGenerationError("Direct reward source must be inside the generation workspace.")
        raw = self._run_json(root, mappings, "inspect-raw-article-grant", {
            "virtualPackage": slot.package_path,
            "inputFile": str(source),
            "slot": _slot_document(slot),
        })
        return _parse_snapshot(raw, slot, source)

    # Patch the prepared asset package
    def patch(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        slot: WorldLootLocation,
        input_file: Path,
        output_file: Path,
        assigned_article_path: str,
    ) -> tuple[RawArticleGrantSnapshot, tuple[Path, ...]]:
        root, mappings = self._validate_roots(work_root, mappings_path)
        source = input_file.resolve(strict=True)
        output = output_file.resolve()
        if not _is_within(source, root) or not _is_within(output, root) or source == output:
            raise RawArticleGrantGenerationError("Direct reward patch source and output must be separate workspace paths.")
        if output.exists() or any(path.exists() for path in _package_files(output)):
            raise RawArticleGrantGenerationError("Refusing to overwrite an existing direct reward output package.")
        raw = self._run_json(root, mappings, "patch-raw-article-grant", {
            "virtualPackage": slot.package_path,
            "inputFile": str(source),
            "outputFile": str(output),
            "assignedArticlePath": assigned_article_path,
            "slot": _slot_document(slot),
        })
        snapshot = _parse_snapshot(raw, slot, output)
        if raw.get("reopenedAndValidated") is not True or snapshot.article_path != assigned_article_path:
            raise RawArticleGrantGenerationError("UAssetAPI did not confirm the literal direct reward after reopen.")
        raw_files = raw.get("outputFiles")
        if not isinstance(raw_files, list) or not raw_files:
            raise RawArticleGrantGenerationError("UAssetAPI omitted direct reward output sidecars.")
        files = tuple(Path(value).resolve(strict=True) for value in raw_files if isinstance(value, str))
        if len(files) != len(raw_files) or output not in files or any(
            not _is_within(path, root)
            or path.stem.casefold() != output.stem.casefold()
            or path.suffix.casefold() not in {".uasset", ".uexp", ".ubulk", ".uptnl"}
            for path in files
        ):
            raise RawArticleGrantGenerationError("UAssetAPI returned an invalid direct reward output inventory.")
        if source.with_suffix(".uexp").is_file() and not any(path.suffix.casefold() == ".uexp" for path in files):
            raise RawArticleGrantGenerationError("Patched direct reward package omitted its required .uexp sidecar.")
        return snapshot, tuple(sorted(files, key=lambda path: path.suffix.casefold()))

    # Run helper and parse JSON
    def _run_json(self, root: Path, mappings: Path, operation: str, document: dict[str, Any]) -> dict[str, Any]:
        if not self.helper_project.is_file():
            raise RawArticleGrantGenerationError(f"UAssetAPI helper project does not exist: {self.helper_project}.")
        manifest = root / f".cv2-direct-reward-{operation}-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        try:
            try:
                result = self.runner(
                    uassetapi_command(
                        self.helper_project, self.dotnet_executable, operation,
                        str(root), str(mappings), self.engine_version, str(manifest),
                    ),
                    check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=self.timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise RawArticleGrantGenerationError(f"Could not run the UAssetAPI direct reward helper: {error}.") from error
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-5000:]
                raise RawArticleGrantGenerationError(
                    f"UAssetAPI {operation} failed (exit {result.returncode}): {detail}"
                )
            try:
                raw = json.loads(result.stdout)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise RawArticleGrantGenerationError(f"UAssetAPI {operation} returned invalid JSON.") from error
            if not isinstance(raw, dict):
                raise RawArticleGrantGenerationError(f"UAssetAPI {operation} returned an invalid result object.")
            return raw
        finally:
            manifest.unlink(missing_ok=True)

    # Check package paths stay contained
    @staticmethod
    def _validate_roots(work_root: Path, mappings_path: Path) -> tuple[Path, Path]:
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir() or not mappings.is_file():
            raise RawArticleGrantGenerationError("Workspace root or CV2.usmap mapping is invalid.")
        return root, mappings


# Apply world loot direct grants
def apply_world_loot_direct_grants(
    plan: WorldLootRandomizationPlan,
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: RawArticleGrantSerializer,
    quest_serializer: BloodCodeSerializer | None = None,
    validated_quest_destination_ids: frozenset[str] | set[str] | tuple[str, ...] = (),
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[WorldLootDirectGrantResult, ...]:
    """Serialize researched RawExport and structured quest Article destinations in World Loot."""
    operation_started = time.perf_counter()
    plan_started = time.perf_counter()
    matches = tuple(
        assignment for assignment in plan.assignments
        if assignment.destination.destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}
    )
    supported_destinations = SUPPORTED_DIRECT_GRANT_DESTINATIONS | frozenset(validated_quest_destination_ids)
    seen: set[str] = set()
    for assignment in matches:
        destination_id = assignment.destination.destination_id
        if destination_id not in supported_destinations:
            raise RawArticleGrantGenerationError(f"Unsupported direct Article destination in World Loot plan: {destination_id}.")
        if destination_id in validated_quest_destination_ids:
            layouts = assignment.destination.quest_grant_layouts
            if (
                assignment.destination.destination_kind != "QUEST_ARTICLE_GRANT"
                or not layouts
                or any(not layout.source_uasset_sha256 or not layout.source_uexp_sha256 for layout in layouts)
            ):
                raise RawArticleGrantGenerationError(
                    f"Validated FieldEnemy destination lacks its fingerprint-bound quest layout: {destination_id}."
                )
        if destination_id in seen:
            raise RawArticleGrantGenerationError(f"World Loot plan contains duplicate direct-grant destination: {destination_id}.")
        seen.add(destination_id)
    plan_seconds = time.perf_counter() - plan_started
    results_by_id: dict[str, WorldLootDirectGrantResult] = {}
    quest_assignments_by_package: dict[str, list[WorldLootAssignment]] = {}
    raw_helper_seconds = 0.0
    raw_helper_invocations = 0
    for assignment in matches:
        destination_id = str(assignment.destination.destination_id)
        if len(assignment.destination.rewards) != len(assignment.assigned_rewards):
            raise RawArticleGrantGenerationError("Direct Article assignment changed the reward bundle arity.")
        if assignment.destination.destination_kind == "QUEST_ARTICLE_GRANT":
            layouts = assignment.destination.quest_grant_layouts
            if (
                len(layouts) != len(assignment.destination.rewards)
                or (bool(assignment.destination.quest_article_layouts)
                    and tuple(layout.reward_index for layout in layouts) != tuple(range(len(layouts))))
            ):
                raise RawArticleGrantGenerationError("Structured quest Article assignment has an incomplete reward layout bundle.")
        elif len(assignment.destination.rewards) != 1:
            raise RawArticleGrantGenerationError("Raw direct Article grants must remain singleton reward assignments.")
        allow_repeat_gain = _allows_repeat_hot_spring_gain(assignment.destination)
        if not _assignment_changes_article_paths(assignment) and not allow_repeat_gain:
            if assignment.destination.display_name is None:
                raise RawArticleGrantGenerationError("Unchanged direct Article destination has no display name.")
            results_by_id[destination_id] = WorldLootDirectGrantResult(
                destination_id,
                assignment.destination.display_name,
                assignment.destination.rewards[0].article_path,
                assignment.assigned_rewards[0].article_path,
                False,
                (),
                False,
            )
            continue
        if assignment.destination.destination_kind == "QUEST_ARTICLE_GRANT":
            if quest_serializer is None:
                raise RawArticleGrantGenerationError("Structured quest Article destination has no UAssetAPI GainItem serializer.")
            quest_assignments_by_package.setdefault(assignment.destination.package_path, []).append(assignment)
            continue
        raw_started = time.perf_counter()
        results_by_id[destination_id] = _apply_one_world_loot_direct_grant(
            assignment, game_root=game_root, work_root=work_root, output_content_root=output_content_root,
            mappings_path=mappings_path, game_version=game_version, data_revision=data_revision,
            extractor=extractor, serializer=serializer, progress=progress,
        )
        raw_helper_seconds += time.perf_counter() - raw_started
        raw_helper_invocations += 1
    if quest_assignments_by_package:
        all_quest_assignments = tuple(
            assignment
            for package_assignments in quest_assignments_by_package.values()
            for assignment in package_assignments
        )
        for result in _apply_world_loot_quest_article_grants(
            all_quest_assignments, game_root=game_root, work_root=work_root,
            output_content_root=output_content_root, mappings_path=mappings_path,
            game_version=game_version, data_revision=data_revision,
            extractor=extractor, serializer=quest_serializer,
            supported_destination_ids=supported_destinations, progress=progress,
        ):
            results_by_id[result.destination_id] = result
    changed_assignments = tuple(
        item for item in matches
        if _assignment_changes_article_paths(item) or _allows_repeat_hot_spring_gain(item.destination)
    )
    timing_path = work_root.expanduser().resolve() / "logs" / "direct_world_loot_timing.json"
    timing_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        timing_data = json.loads(timing_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        timing_data = {}
    if not isinstance(timing_data, dict):
        timing_data = {}
    timing_data.update({
        "direct_reward_assignment_count": len(matches),
        "direct_reward_changed_assignment_count": len(changed_assignments),
        "direct_reward_unique_changed_packages": len({item.destination.package_path.casefold() for item in changed_assignments}),
        "direct_reward_plan_creation_seconds": plan_seconds,
        "direct_reward_raw_helper_invocations": raw_helper_invocations,
        "direct_reward_raw_helper_seconds": raw_helper_seconds,
        "direct_reward_helper_invocations": int(timing_data.get("direct_reward_helper_invocations", 0)) + raw_helper_invocations,
        "direct_reward_total_seconds": time.perf_counter() - operation_started,
    })
    timing_path.write_text(json.dumps(timing_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return tuple(results_by_id[str(assignment.destination.destination_id)] for assignment in matches)


# Apply one world loot quest article
def _apply_one_world_loot_quest_article_grant(
    assignment: WorldLootAssignment,
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: BloodCodeSerializer,
    supported_destination_ids: frozenset[str] | None = None,
    progress: Callable[[str, int, int], None] | None,
) -> WorldLootDirectGrantResult:
    return _apply_world_loot_quest_article_grants(
        (assignment,), game_root=game_root, work_root=work_root,
            output_content_root=output_content_root, mappings_path=mappings_path,
            game_version=game_version, data_revision=data_revision,
            extractor=extractor, serializer=serializer,
            supported_destination_ids=(
                SUPPORTED_DIRECT_GRANT_DESTINATIONS | (supported_destination_ids or frozenset())
            ),
            progress=progress,
    )[0]


# Apply world loot quest article grants
def _apply_world_loot_quest_article_grants(
    assignments: tuple[WorldLootAssignment, ...],
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: BloodCodeSerializer,
    supported_destination_ids: frozenset[str],
    progress: Callable[[str, int, int], None] | None,
) -> tuple[WorldLootDirectGrantResult, ...]:
    if not assignments:
        return ()
    grouped: dict[str, list[WorldLootAssignment]] = defaultdict(list)
    for assignment in assignments:
        destination = assignment.destination
        if (
            destination.destination_id not in supported_destination_ids
            or destination.destination_kind != "QUEST_ARTICLE_GRANT"
        ):
            raise RawArticleGrantGenerationError("Structured quest Article package batch contains an unsupported destination.")
        layouts = destination.quest_grant_layouts
        if (
            not layouts or destination.display_name is None
            or len(destination.rewards) != len(layouts)
            or len(assignment.assigned_rewards) != len(layouts)
            or (bool(destination.quest_article_layouts)
                and tuple(layout.reward_index for layout in layouts) != tuple(range(len(layouts))))
        ):
            raise RawArticleGrantGenerationError("Structured quest Article assignment has invalid destination metadata or payload arity.")
        for assigned_reward in assignment.assigned_rewards:
            _canonical_article_identity(assigned_reward.article_path)
        grouped[destination.package_path.casefold()].append(assignment)

    changed_by_package = {
        key: package_assignments
        for key, package_assignments in grouped.items()
        if any(
            _assignment_changes_article_paths(item) or _allows_repeat_hot_spring_gain(item.destination)
            for item in package_assignments
        )
    }
    results_by_id: dict[str, WorldLootDirectGrantResult] = {}
    for package_assignments in grouped.values():
        if package_assignments[0].destination.package_path.casefold() in changed_by_package:
            continue
        for assignment in package_assignments:
            destination = assignment.destination
            results_by_id[str(destination.destination_id)] = WorldLootDirectGrantResult(
                str(destination.destination_id), str(destination.display_name),
                destination.rewards[0].article_path, assignment.assigned_rewards[0].article_path,
                False, (), False,
            )
    if not changed_by_package:
        return tuple(results_by_id[str(item.destination.destination_id)] for item in assignments)

    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    source_root = (root / "source" / "Content").resolve()
    if not _is_within(output_root, root) or output_root in {root, source_root}:
        raise RawArticleGrantGenerationError("Structured quest Article output must be a separate Content tree inside the workspace.")

    try:
        package_names = tuple(sorted(
            {item.destination.package_path for items in changed_by_package.values() for item in items},
            key=str.casefold,
        ))
        if progress:
            progress("extracting-direct-quest-article", 0, len(package_names))
        started = time.perf_counter()
        extracted = extractor.extract_assets(
            game_root, package_names, game_version=game_version,
            data_revision=data_revision, progress=progress,
        )
        extraction_seconds = time.perf_counter() - started
        extracted_by_key = {asset.virtual_path.casefold(): asset for asset in extracted}
        if set(extracted_by_key) != {value.casefold() for value in package_names}:
            raise RawArticleGrantGenerationError("Asset extractor returned a missing or unexpected structured quest Article package.")
        fingerprint_started = time.perf_counter()
        for key, package_assignments in changed_by_package.items():
            asset = extracted_by_key[package_assignments[0].destination.package_path.casefold()]
            layouts = tuple(
                layout for item in package_assignments for layout in item.destination.quest_grant_layouts
            )
            for layout in layouts:
                _validate_quest_source_fingerprint(asset.primary_file, layout, package_assignments[0].destination.package_path)
        source_fingerprint_seconds = time.perf_counter() - fingerprint_started

        started = time.perf_counter()
        materialized = materialize_extracted_assets(extracted, source_root, verify_hashes=False)
        materialize_seconds = time.perf_counter() - started
        materialized_files = tuple(path for asset in materialized for path in asset.files)
        materialized_by_key = {asset.virtual_path.casefold(): asset for asset in materialized}
        staging_root = root / "direct-quest-article-patches" / uuid.uuid4().hex / "Content"
        patch_requests: dict[str, tuple[Path, Path, tuple[dict[str, Any], ...]]] = {}
        destinations_by_package: dict[str, list[WorldLootAssignment]] = {}
        changed_ids: set[str] = set()
        for key, package_assignments in changed_by_package.items():
            package_path = package_assignments[0].destination.package_path
            source_asset = materialized_by_key[package_path.casefold()]
            final_header = output_root.joinpath(*package_path.split("/")[2:]).with_suffix(".uasset")
            existing_files = tuple(path for path in _package_files(final_header) if path.is_file())
            if existing_files and final_header not in existing_files:
                raise RawArticleGrantGenerationError("Existing structured quest output is missing its package header.")
            patch_source = final_header if final_header.is_file() else source_asset.primary_file
            patch_output = staging_root.joinpath(*package_path.split("/")[2:]).with_suffix(".uasset")
            patch_output.parent.mkdir(parents=True, exist_ok=True)
            package_documents: list[dict[str, Any]] = []
            package_assignments_sorted = sorted(
                package_assignments,
                key=lambda item: (
                    item.destination.export_index,
                    item.destination.quest_grant_layouts[0].bulk_index,
                    item.destination.quest_grant_layouts[0].reward_index,
                ),
            )
            for assignment in package_assignments_sorted:
                destination = assignment.destination
                for reward_index, (layout, assigned_reward) in enumerate(zip(
                    destination.quest_grant_layouts, assignment.assigned_rewards, strict=True,
                )):
                    if destination.quest_article_layouts and layout.reward_index != reward_index:
                        raise RawArticleGrantGenerationError("Structured quest reward layouts are not in serialized index order.")
                    assigned_path = assigned_reward.article_path
                    assigned_asset = _canonical_article_identity(assigned_path)
                    target_exports = (
                        (destination.export_index, layout.export_name),
                        *((mirror.export_index, mirror.export_name) for mirror in layout.mirrored_exports),
                    )
                    for export_index, export_name in target_exports:
                        patch_document = {
                            "exportIndex": export_index,
                            "exportName": export_name,
                            "bulkIndex": layout.bulk_index,
                            "rewardIndex": layout.reward_index,
                            "patchArticlePath": _assignment_changes_article_paths(assignment),
                            "expectedPackage": layout.expected_article_path,
                            "expectedAsset": layout.expected_article_path.rsplit("/", 1)[-1],
                            "expectedCount": layout.count,
                            "expectedNotification": layout.notification,
                            "assignedPackage": assigned_path,
                            "assignedAsset": assigned_asset,
                            "expectedSubPathString": layout.expected_sub_path_string,
                            "expectedPackageNameValue": layout.expected_package_name_value,
                            "expectedPackageNameNumber": layout.expected_package_name_number,
                            "expectedAssetNameValue": layout.expected_asset_name_value,
                            "expectedAssetNameNumber": layout.expected_asset_name_number,
                            "expectedArticlesCount": layout.expected_articles_count,
                            "preservedExportIndex": layout.preserved_export_index,
                            "preservedExportName": layout.preserved_export_name,
                            "requireStrictExportSnapshots": layout.require_strict_export_snapshots,
                        }
                        if _allows_repeat_hot_spring_gain(destination):
                            patch_document["allowMultipleGain"] = True
                        package_documents.append(patch_document)
                if _assignment_changes_article_paths(assignment) or _allows_repeat_hot_spring_gain(destination):
                    changed_ids.add(str(destination.destination_id))
            physical_keys = {(item["exportIndex"], item["bulkIndex"], item["rewardIndex"]) for item in package_documents}
            if len(physical_keys) != len(package_documents):
                raise RawArticleGrantGenerationError("Structured quest Article assignments overlap physical export targets.")
            if existing_files:
                source_suffixes = {path.suffix.casefold() for path in source_asset.files}
                existing_suffixes = {path.suffix.casefold() for path in existing_files}
                if existing_suffixes != source_suffixes:
                    raise RawArticleGrantGenerationError("Existing staged quest package has an unexpected sidecar inventory.")
            patch_requests[package_path] = (patch_source, patch_output, tuple(package_documents))
            destinations_by_package[package_path.casefold()] = package_assignments

        started = time.perf_counter()
        raw_results = serializer.patch_world_loot_quest_packages(
            work_root=root, mappings_path=mappings_path, patch_packages=patch_requests,
        )
        helper_seconds = time.perf_counter() - started
        if len(raw_results) != len(patch_requests) or any(not isinstance(item, dict) for item in raw_results):
            raise RawArticleGrantGenerationError("UAssetAPI returned an unexpected structured quest Article patch result.")
        request_by_key = {package.casefold(): request for package, request in patch_requests.items()}
        returned_by_package: dict[str, dict[str, Any]] = {}
        for raw_result in raw_results:
            package_path = raw_result.get("virtualPackage")
            if not isinstance(package_path, str) or package_path.casefold() not in request_by_key:
                raise RawArticleGrantGenerationError("UAssetAPI returned an unexpected quest Article package identity.")
            if package_path.casefold() in returned_by_package:
                raise RawArticleGrantGenerationError("UAssetAPI returned a duplicate quest Article package result.")
            source, expected_output, documents = request_by_key[package_path.casefold()]
            if (
                Path(str(raw_result.get("inputFile", ""))).resolve() != source.resolve()
                or Path(str(raw_result.get("outputFile", ""))).resolve() != expected_output.resolve()
                or raw_result.get("patchedSlots") != len(documents)
            ):
                raise RawArticleGrantGenerationError("UAssetAPI returned an invalid quest Article patch identity or count.")
            returned_by_package[package_path.casefold()] = raw_result

        output_inventory_by_package: dict[str, tuple[Path, ...]] = {}
        for key, raw_result in returned_by_package.items():
            package_path = destinations_by_package[key][0].destination.package_path
            expected_output = patch_requests[package_path][1].resolve()
            raw_files = raw_result.get("outputFiles")
            if not isinstance(raw_files, list) or not raw_files:
                raise RawArticleGrantGenerationError("UAssetAPI omitted structured quest Article sidecars.")
            output_files = tuple(Path(str(value)).resolve(strict=True) for value in raw_files if isinstance(value, str))
            if len(output_files) != len(raw_files) or expected_output not in output_files or any(
                not _is_within(path, staging_root)
                or path.stem.casefold() != expected_output.stem.casefold()
                or path.suffix.casefold() not in {".uasset", ".uexp", ".ubulk", ".uptnl"}
                for path in output_files
            ):
                raise RawArticleGrantGenerationError("UAssetAPI returned an invalid structured quest Article output inventory.")
            source = request_by_key[key][0]
            if source.with_suffix(".uexp").is_file() and not any(path.suffix.casefold() == ".uexp" for path in output_files):
                raise RawArticleGrantGenerationError("Patched quest Article package omitted its required .uexp sidecar.")
            output_inventory_by_package[key] = output_files

        committed_by_package: dict[str, tuple[Path, ...]] = {}
        commit_started = time.perf_counter()
        for key, output_files in output_inventory_by_package.items():
            assignments_for_package = destinations_by_package[key]
            package_path = assignments_for_package[0].destination.package_path
            final_header = output_root.joinpath(*package_path.split("/")[2:]).with_suffix(".uasset")
            committed_by_package[key] = _commit_quest_article_package(
                output_files, final_header, output_root,
            )
        commit_seconds = time.perf_counter() - commit_started

        if progress:
            progress("patching-direct-quest-article", len(patch_requests), len(patch_requests))
        for package_assignments in grouped.values():
            key = package_assignments[0].destination.package_path.casefold()
            package_outputs = committed_by_package.get(key, ())
            reopened = bool(package_outputs)
            for assignment in package_assignments:
                destination = assignment.destination
                results_by_id[str(destination.destination_id)] = WorldLootDirectGrantResult(
                    str(destination.destination_id), str(destination.display_name),
                    destination.rewards[0].article_path, assignment.assigned_rewards[0].article_path,
                    str(destination.destination_id) in changed_ids, package_outputs, reopened,
                )
        timing_document: dict[str, float | int] = {
            "direct_reward_unique_packages": len(changed_by_package),
            "direct_reward_quest_helper_invocations": 1,
            "direct_reward_extract_seconds": extraction_seconds,
            "direct_reward_source_fingerprint_seconds": source_fingerprint_seconds,
            "direct_reward_materialize_seconds": materialize_seconds,
            "direct_reward_patch_helper_seconds": helper_seconds,
            "direct_reward_structural_package_loads": len(changed_by_package) * 2,
            "direct_reward_serializations": len(changed_by_package),
            "direct_reward_reopen_validations": len(changed_by_package),
            "direct_reward_source_copy_files": len(materialized_files),
            "direct_reward_source_copy_bytes": sum(path.stat().st_size for path in materialized_files if path.is_file()),
            "direct_reward_source_hash_file_reads": 0,
            "direct_reward_output_commit_seconds": commit_seconds,
        }
        helper_timing_fields = (
            ("packageLoads", "direct_reward_structural_package_loads"),
            ("sourceLoadSeconds", "direct_reward_helper_source_load_seconds"),
            ("sourceInspectionSeconds", "direct_reward_helper_source_inspection_seconds"),
            ("mutationSeconds", "direct_reward_helper_mutation_seconds"),
            ("serializationSeconds", "direct_reward_helper_serialization_seconds"),
            ("reopenValidationSeconds", "direct_reward_helper_reopen_validation_seconds"),
            ("outputMoveSeconds", "direct_reward_helper_output_move_seconds"),
        )
        for raw_key, timing_key in helper_timing_fields:
            values = [result.get(raw_key) for result in returned_by_package.values()]
            if values and all(type(value) in {int, float} for value in values):
                timing_document[timing_key] = sum(float(value) for value in values)
        # Detailed helper timings are stored in the generation workspace for diagnosis.
        (root / "logs").mkdir(parents=True, exist_ok=True)
        (root / "logs" / "direct_world_loot_timing.json").write_text(
            json.dumps(timing_document, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        return tuple(results_by_id[str(item.destination.destination_id)] for item in assignments)
    except Exception as error:
        if isinstance(error, RawArticleGrantGenerationError):
            raise
        raise RawArticleGrantGenerationError(f"Could not patch structured direct World Loot quest rewards: {error}") from error


# Resolve canonical article identity
def _canonical_article_identity(article_path: str) -> str:
    package, _, asset = article_path.rpartition("/")
    if not package or not asset or not article_path.startswith("/Game/Blueprint/Article/"):
        raise RawArticleGrantGenerationError("Assigned World Loot reward is not a canonical Article package path.")
    return asset


# Resolve assignment changes article paths
def _assignment_changes_article_paths(assignment: WorldLootAssignment) -> bool:
    return tuple(item.article_path for item in assignment.destination.rewards) != tuple(
        item.article_path for item in assignment.assigned_rewards
    )


# Check repeat hot spring gain
def _allows_repeat_hot_spring_gain(slot: WorldLootLocation) -> bool:
    """Hot Springs must grant randomized items that the player already owns."""
    destination_id = slot.destination_id or ""
    package_path = slot.package_path.casefold()
    return (
        slot.destination_kind == "QUEST_ARTICLE_GRANT"
        and destination_id.casefold().startswith("hot_spring_")
        and "/hotspring/" in package_path
    )


# Find quest article target coordinates
def _quest_article_target_coordinates(slot: WorldLootLocation) -> tuple[tuple[int, str, int, int], ...]:
    layouts = slot.quest_grant_layouts
    if not layouts:
        raise RawArticleGrantGenerationError("Structured quest Article slot has no validated layout.")
    targets = tuple(
        target
        for layout in layouts
        for target in (
            (slot.export_index, layout.export_name, layout.bulk_index, layout.reward_index),
            *((mirror.export_index, mirror.export_name, layout.bulk_index, layout.reward_index)
              for mirror in layout.mirrored_exports),
        )
    )
    if len({(item[0], item[2], item[3]) for item in targets}) != len(targets):
        raise RawArticleGrantGenerationError("Structured quest Article mirrors contain duplicate physical targets.")
    return targets


# Inspect quest article targets
def _inspect_quest_article_targets(
    serializer: BloodCodeSerializer,
    root: Path,
    mappings_path: Path,
    slot: WorldLootLocation,
    input_file: Path,
) -> tuple[tuple[int, str, BloodCodeGrantSnapshot], ...]:
    snapshots = serializer.inspect_world_loot_quest_packages(
        work_root=root, mappings_path=mappings_path,
        package_files={slot.package_path: input_file},
    )
    if not slot.quest_grant_layouts:
        raise RawArticleGrantGenerationError("Structured quest Article slot has no validated layout.")
    matches: list[tuple[int, str, BloodCodeGrantSnapshot]] = []
    for export_index, export_name, bulk_index, reward_index in _quest_article_target_coordinates(slot):
        found = tuple(
            snapshot for snapshot in snapshots
            if (snapshot.virtual_package.casefold(), snapshot.export_index, snapshot.bulk_index, snapshot.reward_index)
            == (slot.package_path.casefold(), export_index, bulk_index, reward_index)
        )
        if len(found) != 1 or found[0].export_name != export_name:
            raise RawArticleGrantGenerationError("Expected structured quest Article target was not uniquely found by export and row identity.")
        matches.append((export_index, export_name, found[0]))
    return tuple(matches)


# Validate quest source fingerprint
def _validate_quest_source_fingerprint(
    primary_file: Path,
    layout: QuestArticleGrantLayout,
    package_path: str,
) -> None:
    expected_uasset = layout.source_uasset_sha256
    expected_uexp = layout.source_uexp_sha256
    if expected_uasset is None and expected_uexp is None:
        return
    if not expected_uasset or not expected_uexp:
        raise RawArticleGrantGenerationError("Structured quest Article source fingerprint is incomplete.")
    uexp_file = primary_file.with_suffix(".uexp")
    manifest_path = primary_file.parent / ".cv2randomizer-asset.json"
    if not primary_file.is_file() or not uexp_file.is_file():
        raise RawArticleGrantGenerationError(
            f"Fingerprint-bound quest package omitted its .uasset/.uexp source sidecars: {package_path}."
        )
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = {row.get("name"): row for row in manifest.get("files", []) if isinstance(row, dict)}
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError) as error:
            raise RawArticleGrantGenerationError(f"Cached quest package manifest is unreadable: {package_path}.") from error
        header_entry = entries.get(primary_file.name)
        export_entry = entries.get(uexp_file.name)
        valid = (
            isinstance(header_entry, dict) and header_entry.get("sha256") == expected_uasset
            and isinstance(export_entry, dict) and export_entry.get("sha256") == expected_uexp
            and header_entry.get("size") == primary_file.stat().st_size
            and export_entry.get("size") == uexp_file.stat().st_size
        )
    else:
        # Keep injected/direct fixture callers safe; the normal cache-backed
        # Generate path uses the already validated immutable package manifest.
        valid = (
            hashlib.sha256(primary_file.read_bytes()).hexdigest() == expected_uasset
            and hashlib.sha256(uexp_file.read_bytes()).hexdigest() == expected_uexp
        )
    if not valid:
        raise RawArticleGrantGenerationError(
            f"Structured quest Article source fingerprint mismatch: {package_path}."
        )


# Validate quest article snapshot
def _validate_quest_article_snapshot(
    slot: WorldLootLocation,
    layout: QuestArticleGrantLayout,
    snapshot: BloodCodeGrantSnapshot,
    *,
    assigned: bool,
    assigned_path: str | None = None,
    target_export_index: int | None = None,
    target_export_name: str | None = None,
) -> None:
    expected_path = assigned_path if assigned else layout.expected_article_path
    expected_name = expected_path.rsplit("/", 1)[-1]
    expected_package_value = expected_path if assigned else layout.expected_package_name_value
    expected_asset_value = expected_name if assigned else layout.expected_asset_name_value
    expected_package_number = 0 if assigned else layout.expected_package_name_number
    expected_asset_number = 0 if assigned else layout.expected_asset_name_number
    if (
        snapshot.virtual_package.casefold() != slot.package_path.casefold()
        or snapshot.export_index != (slot.export_index if target_export_index is None else target_export_index)
        or snapshot.export_name != (layout.export_name if target_export_name is None else target_export_name)
        or snapshot.bulk_index != layout.bulk_index
        or snapshot.reward_index != layout.reward_index
        or snapshot.reward_package != expected_path
        or snapshot.reward_asset != expected_name
        or snapshot.package_name_value != expected_package_value
        or snapshot.package_name_number != expected_package_number
        or snapshot.asset_name_value != expected_asset_value
        or snapshot.asset_name_number != expected_asset_number
        or snapshot.sub_path_string != layout.expected_sub_path_string
        or (layout.expected_articles_count is not None and snapshot.articles_count != layout.expected_articles_count)
        or snapshot.count != layout.count
        or snapshot.notification is not layout.notification
    ):
        mode = "assigned" if assigned else "vanilla"
        raise RawArticleGrantGenerationError(f"Reopened structured quest Article package differs from its {mode} source identity.")


# List commit quest article package
def _commit_quest_article_package(
    staged_files: tuple[Path, ...],
    final_header: Path,
    output_root: Path,
) -> tuple[Path, ...]:
    staged_by_suffix = {path.suffix.casefold(): path for path in staged_files}
    if len(staged_by_suffix) != len(staged_files) or ".uasset" not in staged_by_suffix:
        raise RawArticleGrantGenerationError("Staged structured quest package has duplicate or missing file types.")
    targets = {suffix: final_header.with_suffix(suffix) for suffix in staged_by_suffix}
    if any(not _is_within(path, output_root) for path in targets.values()):
        raise RawArticleGrantGenerationError("Structured quest package commit target escaped the output Content tree.")
    existing = {path.suffix.casefold(): path for path in _package_files(final_header) if path.is_file()}
    if existing and set(existing) != set(targets):
        raise RawArticleGrantGenerationError("Refusing to replace a staged quest package with a different sidecar inventory.")

    token = uuid.uuid4().hex
    backups: dict[str, Path] = {}
    committed: list[Path] = []
    try:
        for suffix, staged in staged_by_suffix.items():
            target = targets[suffix]
            target.parent.mkdir(parents=True, exist_ok=True)
            if staged.stat().st_size <= 0:
                raise RawArticleGrantGenerationError(f"Patched quest sidecar is empty: {staged.name}.")
        for suffix, target in targets.items():
            if target.is_file():
                backup = target.with_name(f"{target.name}.cv2-backup-{token}")
                target.replace(backup)
                backups[suffix] = backup
            staged_by_suffix[suffix].replace(target)
            committed.append(target)
        for backup in backups.values():
            backup.unlink(missing_ok=True)
        return tuple(sorted(targets.values(), key=lambda path: path.suffix.casefold()))
    except Exception:
        for target in reversed(committed):
            target.unlink(missing_ok=True)
        for suffix, backup in backups.items():
            if backup.is_file():
                backup.replace(targets[suffix])
        raise
    finally:
        for path in backups.values():
            path.unlink(missing_ok=True)


# Apply one world loot direct grant
def _apply_one_world_loot_direct_grant(
    assignment: WorldLootAssignment,
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: RawArticleGrantSerializer,
    progress: Callable[[str, int, int], None] | None,
) -> WorldLootDirectGrantResult:
    slot = assignment.destination
    destination_id = slot.destination_id
    if destination_id not in SUPPORTED_DIRECT_GRANT_DESTINATIONS:
        raise RawArticleGrantGenerationError(f"Unsupported direct Article destination: {destination_id}.")
    if len(assignment.assigned_rewards) != 1 or slot.raw_article_layout is None or slot.display_name is None:
        raise RawArticleGrantGenerationError("Direct Article grant assignment has an invalid World Loot payload.")
    original_path = slot.rewards[0].article_path
    assigned_path = assignment.assigned_rewards[0].article_path
    if assigned_path == original_path:
        return WorldLootDirectGrantResult(
            str(destination_id), slot.display_name, original_path, assigned_path, False, (), False,
        )
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    if not _is_within(output_root, root) or output_root == (root / "source" / "Content").resolve():
        raise RawArticleGrantGenerationError("Direct reward output must be a separate Content tree inside the workspace.")
    try:
        if progress:
            progress("extracting-direct-article-grant", 0, len(assignment.assigned_rewards))
        extracted = extractor.extract_assets(
            game_root, (slot.package_path,), game_version=game_version,
            data_revision=data_revision, progress=progress,
        )
        if len(extracted) != 1 or extracted[0].virtual_path.casefold() != slot.package_path.casefold():
            raise RawArticleGrantGenerationError("Asset extractor returned a missing or unexpected direct reward package.")
        materialized = materialize_extracted_assets(extracted, root / "source" / "Content", verify_hashes=False)
        if len(materialized) != 1:
            raise RawArticleGrantGenerationError("Direct reward materialization returned an unexpected package count.")
        source_asset = materialized[0]
        expected_header = root / "source" / "Content" / Path(*slot.source_file.split("/")).with_suffix(".uasset")
        if source_asset.primary_file.resolve() != expected_header.resolve():
            raise RawArticleGrantGenerationError("Extracted direct reward package does not match its catalogued virtual path.")
        output = output_root.joinpath(*slot.package_path.split("/")[2:]).with_suffix(".uasset")
        output.parent.mkdir(parents=True, exist_ok=True)
        patched_snapshot, output_files = serializer.patch(
            work_root=root,
            mappings_path=mappings_path,
            slot=slot,
            input_file=source_asset.primary_file,
            output_file=output,
            assigned_article_path=assigned_path,
        )
        if patched_snapshot.article_path != assigned_path or patched_snapshot.path_number != 0 or patched_snapshot.object_number != 0:
            raise RawArticleGrantGenerationError("Reopened direct reward package failed its literal Article identity check.")
        if progress:
            progress("patching-direct-article-grant", 1, 1)
        return WorldLootDirectGrantResult(
            str(destination_id), slot.display_name, original_path, assigned_path, True, output_files, True,
        )
    except (AssetExtractionError, OSError, ValueError) as error:
        if isinstance(error, RawArticleGrantGenerationError):
            raise
        raise RawArticleGrantGenerationError(f"Could not patch direct World Loot reward: {error}") from error
    except Exception as error:
        if isinstance(error, RawArticleGrantGenerationError):
            raise
        raise RawArticleGrantGenerationError(f"Could not patch direct World Loot reward: {error}") from error


# Validate vanilla snapshot
def _validate_vanilla_snapshot(slot: WorldLootLocation, snapshot: RawArticleGrantSnapshot) -> None:
    layout = slot.raw_article_layout
    if layout is None:
        raise RawArticleGrantGenerationError("Direct reward slot has no validated raw serialization layout.")
    if (
        snapshot.virtual_package != slot.package_path
        or snapshot.export_index != slot.export_index
        or snapshot.export_name != layout.export_name
        or snapshot.serial_size != layout.serial_size
        or snapshot.path_name_index != layout.expected_path_name_index
        or snapshot.path_number != layout.expected_vanilla_number
        or snapshot.object_name_index != layout.expected_object_name_index
        or snapshot.object_number != layout.expected_vanilla_number
        or snapshot.path_name_map_value != layout.expected_path_name_map_value
        or snapshot.object_name_map_value != layout.expected_object_name_map_value
        or snapshot.article_path != layout.expected_vanilla_article_path
    ):
        raise RawArticleGrantGenerationError("Extracted direct reward CDO differs from its fingerprint-bound catalog identity.")


# Build slot document
def _slot_document(slot: WorldLootLocation) -> dict[str, Any]:
    layout = slot.raw_article_layout
    if layout is None:
        raise RawArticleGrantGenerationError("Direct reward slot has no raw Article layout metadata.")
    return {
        "destinationId": slot.destination_id,
        "packagePath": slot.package_path,
        "exportName": layout.export_name,
        "exportIndex": slot.export_index,
        "serialSize": layout.serial_size,
        "pathNameIndexOffset": layout.path_name_index_offset,
        "pathNumberOffset": layout.path_number_offset,
        "objectNameIndexOffset": layout.object_name_index_offset,
        "objectNumberOffset": layout.object_number_offset,
        "expectedPathNameIndex": layout.expected_path_name_index,
        "expectedObjectNameIndex": layout.expected_object_name_index,
        "expectedPathNameMapValue": layout.expected_path_name_map_value,
        "expectedObjectNameMapValue": layout.expected_object_name_map_value,
        "expectedVanillaArticlePath": layout.expected_vanilla_article_path,
        "expectedVanillaNumber": layout.expected_vanilla_number,
        "sourceUassetSha256": layout.source_uasset_sha256,
        "sourceUexpSha256": layout.source_uexp_sha256,
    }


# Parse snapshot
def _parse_snapshot(raw: Any, slot: WorldLootLocation, expected_file: Path) -> RawArticleGrantSnapshot:
    layout = slot.raw_article_layout
    if not isinstance(raw, dict) or layout is None or raw.get("virtualPackage") != slot.package_path:
        raise RawArticleGrantGenerationError("UAssetAPI returned an unexpected direct reward package result.")
    integer_fields = (
        "exportIndex", "serialSize", "pathNameIndex", "pathNumber", "objectNameIndex", "objectNumber",
    )
    if any(type(raw.get(field)) is not int for field in integer_fields):
        raise RawArticleGrantGenerationError("UAssetAPI returned invalid direct reward field offsets or sizes.")
    input_file = Path(str(raw.get("inputFile", ""))).resolve()
    if input_file != expected_file.resolve():
        raise RawArticleGrantGenerationError("UAssetAPI inspected or patched an unexpected direct reward file.")
    return RawArticleGrantSnapshot(
        slot.package_path,
        input_file,
        raw["exportIndex"],
        _string(raw.get("exportName"), "export name"),
        raw["serialSize"],
        raw["pathNameIndex"],
        raw["pathNumber"],
        raw["objectNameIndex"],
        raw["objectNumber"],
        _string(raw.get("pathNameMapValue"), "path NameMap value"),
        _string(raw.get("objectNameMapValue"), "object NameMap value"),
        _string(raw.get("articlePath"), "Article path"),
    )


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RawArticleGrantGenerationError(f"UAssetAPI returned an invalid {label}.")
    return value


# Build package files
def _package_files(header: Path) -> tuple[Path, ...]:
    return tuple(header.with_suffix(extension) for extension in (".uasset", ".uexp", ".ubulk", ".uptnl"))


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
