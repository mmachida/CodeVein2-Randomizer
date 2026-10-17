"""Fail-closed world-loot patching through the pinned UAssetAPI helper."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.core.memory_budget import DEFAULT_UASSET_JOB_BYTES, MemoryBudgetScheduler
from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command
from cv2randomizer.game.fname_identity import FNameIdentity, SoftObjectPathIdentity

SUPPORTED_ENGINE_VERSION = "VER_UE5_4"
DOTNET_GC_HEAP_HARD_LIMIT = "0x80000000"  # 2 GiB; prevents .NET from over-expanding on pathological maps.
LOGGER = logging.getLogger(__name__)


class WorldLootSerializationError(RuntimeError):
    """Raised when a world-loot package cannot be safely patched and validated."""


@dataclass(frozen=True, slots=True)
class ArticleReward:
    """A complete ArticleBulkInfo reward tuple."""

    article_data_path: str
    count: int
    b_notification: bool
    soft_object_path_identity: SoftObjectPathIdentity | None = None

    # Validate initialized settings
    def __post_init__(self) -> None:
        path = self.article_data_path
        if type(path) is not str:
            raise ValueError("Reward path must be a string.")
        parsed = PurePosixPath(path)
        if (
            "\\" in path
            or not path.startswith("/Game/")
            or not parsed.is_absolute()
            or parsed.suffix
            or any(part in {"", ".", ".."} for part in parsed.parts[1:])
            or len(parsed.parts) < 3
        ):
            raise ValueError("Reward path must be a /Game/... package path without an extension.")
        if type(self.count) is not int or self.count <= 0:
            raise ValueError("Reward count must be a positive integer.")
        if type(self.b_notification) is not bool:
            raise ValueError("b_notification must be a bool.")
        if self.soft_object_path_identity is not None:
            identity = self.soft_object_path_identity
            if (
                identity.resolved_path != path
                or identity.resolved_asset_name != parsed.name
            ):
                raise ValueError("Soft object path FName identity does not resolve to the Article path.")


@dataclass(frozen=True, slots=True)
class WorldLootPatchResult:
    """Reopened and validated patch result."""

    export_name: str
    export_index: int
    reward_index: int
    unique_save_id: str
    original: ArticleReward
    assigned: ArticleReward
    output_path: Path


@dataclass(frozen=True, slots=True)
class WorldLootTuplePatch:
    """One catalog-verified reward edit within a destination map."""

    export_index: int
    reward_index: int
    export_name: str
    expected_original: ArticleReward
    assigned: ArticleReward
    require_unique_save_id: bool = True


@dataclass(frozen=True, slots=True)
class WorldLootPackagePatch:
    """All requested reward edits for one destination package."""

    input_asset: Path
    output_asset: Path
    patches: tuple[WorldLootTuplePatch, ...]
    blueprint_schema_package: str | None = None


@dataclass(frozen=True, slots=True)
class WorldLootPackagePatchResult:
    """One reopened and validated output package and its edits."""

    input_path: Path
    output_path: Path
    patches: tuple[WorldLootPatchResult, ...]


@dataclass(frozen=True, slots=True)
class HealingUpgradePatch:
    """One physical pickup redirected to a location-specific Blueprint CDO."""

    export_index: int
    export_name: str
    class_package: str
    class_name: str
    cdo_export_index: int
    cdo_export_name: str
    blueprint_input: Path
    blueprint_output: Path
    clone_package_path: str
    expected_original: ArticleReward
    assigned: ArticleReward
    name_map_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HealingUpgradePackagePatch:
    input_asset: Path
    output_asset: Path
    patches: tuple[HealingUpgradePatch, ...]
    package_path: str | None = None
    map_name_map_values: tuple[str, ...] = ()
    discovery_exports: tuple[tuple[int, str], ...] = ()
    world_loot_fields: tuple[tuple[int, int, str, ArticleReward], ...] = ()


@dataclass(frozen=True, slots=True)
class HealingUpgradeCloneResult:
    input_path: Path
    output_path: Path
    clone_package_path: str
    original: ArticleReward
    assigned: ArticleReward
    reopened_and_validated: bool
    raw_payload_preserved: bool
    timings: dict[str, float] | None = None
    prepared_descriptor: dict[str, Any] | None = None
    name_map_count_before: int | None = None
    name_map_count_after: int | None = None
    names_added: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HealingUpgradePackagePatchResult:
    input_path: Path
    output_path: Path
    patch_count: int
    clones: tuple[HealingUpgradeCloneResult, ...]
    reopened_and_validated: bool
    raw_payload_preserved: bool
    timings: dict[str, float] | None = None
    helper_profile: dict[str, float] | None = None
    prepared_descriptor: dict[str, Any] | None = None
    name_map_count_before: int | None = None
    name_map_count_after: int | None = None
    names_added: tuple[str, ...] = ()
    class_inspections: tuple[dict[str, Any], ...] = ()
    world_loot_fields: tuple[dict[str, Any], ...] = ()
    discovery_open_count: int = 0
    preparation_reload_count: int = 0
    serialization_reopen_count: int = 0
    validation_reopen_count: int = 0


class WorldLootSerializer:
    """Run the pinned .NET helper against staged Unreal packages only."""

    # Initialize this object
    def __init__(
        self,
        *,
        helper_project: Path | None = None,
        dotnet_executable: str = "dotnet",
        engine_version: str = "VER_UE5_4",
        timeout_seconds: float = 300,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run_hidden,
    ) -> None:
        self.helper_project = (helper_project or resolve_uassetapi_helper()).resolve()
        self.dotnet_executable = dotnet_executable
        self.engine_version = engine_version
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        if engine_version != SUPPORTED_ENGINE_VERSION:
            raise ValueError(f"World-loot serialization currently supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Patch package reward data
    def patch_packages(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        packages: Iterable[WorldLootPackagePatch],
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[WorldLootPackagePatchResult, ...]:
        """Patch complete maps in small Release-helper batches and validate edits.

        Package inputs are immutable staged originals. Outputs are siblings so
        Blueprint imports resolve; the caller promotes them only after every package
        has passed. The helper forces collection between maps so the bounded heap
        remains stable while process startup is amortized across a small batch.
        """
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise WorldLootSerializationError("work_root must be a directory.")
        if not self.helper_project.is_file():
            raise WorldLootSerializationError(f"UAssetAPI helper project was not found: {self.helper_project}")
        package_list = tuple(packages)
        if not package_list:
            return ()
        if not mappings.is_file():
            raise WorldLootSerializationError(f"UAssetAPI mapping file was not found: {mappings}")

        validated_packages: list[WorldLootPackagePatch] = []
        request_entries: list[dict[str, Any]] = []
        seen_inputs: set[Path] = set()
        seen_outputs: set[Path] = set()
        for package in package_list:
            source = package.input_asset.expanduser().resolve(strict=True)
            output = package.output_asset.expanduser().resolve()
            if not _is_within(source, root) or not _is_within(output, root):
                raise WorldLootSerializationError("Input and output packages must stay inside the supplied staging root.")
            if source == output or source.parent != output.parent:
                raise WorldLootSerializationError("Output must be a new sibling package so Blueprint dependencies remain resolvable.")
            if source.suffix.casefold() not in {".umap", ".uasset"} or output.suffix.casefold() != source.suffix.casefold():
                raise WorldLootSerializationError("Input and output must use the same .umap or .uasset extension.")
            if source in seen_inputs or output in seen_outputs:
                raise WorldLootSerializationError("Batch package inputs and outputs must be unique.")
            if any(path.exists() for path in _package_files(output)):
                raise WorldLootSerializationError(f"Refusing to overwrite an existing staged output package: {output.name}.")
            if not package.patches:
                raise WorldLootSerializationError("Each package patch request must contain at least one reward edit.")
            schema_package = package.blueprint_schema_package
            if schema_package is not None:
                parsed_schema = PurePosixPath(schema_package)
                if (
                    "\\" in schema_package or not schema_package.startswith("/Game/")
                    or not parsed_schema.is_absolute() or parsed_schema.suffix
                    or any(part in {"", ".", ".."} for part in parsed_schema.parts[1:])
                ):
                    raise WorldLootSerializationError("Blueprint schema package must be a canonical /Game/... path.")
            seen_inputs.add(source)
            seen_outputs.add(output)
            validated_packages.append(WorldLootPackagePatch(source, output, package.patches, schema_package))
            seen_slots: set[tuple[int, int]] = set()
            patch_entries: list[dict[str, Any]] = []
            for patch in package.patches:
                if (
                    patch.export_index < 0 or patch.reward_index < 0 or not patch.export_name.strip()
                    or (patch.export_index == 0 and schema_package is None)
                ):
                    raise WorldLootSerializationError(
                        "Patch export identity must use a positive export index; zero is reserved for a named Blueprint CDO."
                    )
                if patch.export_index == 0:
                    expected_cdo = "Default__" + schema_package.rsplit("/", 1)[-1] + "_C"
                    if patch.export_name != expected_cdo:
                        raise WorldLootSerializationError("Named Blueprint CDO must match its registered schema package.")
                slot = (patch.export_index, patch.reward_index)
                if slot in seen_slots:
                    raise WorldLootSerializationError("Duplicate actor/reward patch request in one package.")
                seen_slots.add(slot)
                patch_entries.append({
                    "exportIndex": patch.export_index,
                    "rewardIndex": patch.reward_index,
                    "exportName": patch.export_name,
                    "expected": _reward_to_json(patch.expected_original),
                    "assigned": _reward_to_json(patch.assigned),
                    "requireUniqueSaveId": patch.require_unique_save_id,
                })
            request_entry = {"inputFile": str(source), "outputFile": str(output), "patches": patch_entries}
            if schema_package is not None:
                request_entry["blueprintSchemaPackage"] = schema_package
            request_entries.append(request_entry)

        # Maps are independent, but each helper process can retain a large UE
        # object graph. Four lanes were measured as the best throughput on the
        # current 8-core/32-GiB development host. Admission still uses the same
        # RAM estimates only restrict optional parallel workers; the scheduler
        # always allows one worker so low-memory systems can finish more slowly.
        batch_size = 32
        scheduler = MemoryBudgetScheduler(
            maximum_workers=4,
        )
        memory_status = scheduler.status()
        # Shared Blueprint schemas require serial batches.
        has_blueprint_schema_reads = any(
            package.blueprint_schema_package is not None
            for package in validated_packages
        )
        worker_count = (
            1 if has_blueprint_schema_reads
            else max(1, scheduler.recommended_worker_count())
        )
        LOGGER.info(
            "World Loot helper pool workers=%d batch_size=%d shared_blueprint_schemas=%s estimated_per_helper=%.2f GiB "
            "available_ram=%.2f GiB parallel_worker_reserve=%.2f GiB",
            worker_count,
            batch_size,
            has_blueprint_schema_reads,
            DEFAULT_UASSET_JOB_BYTES / (1024**3),
            memory_status.available_bytes / (1024**3),
            scheduler.reserve_bytes(memory_status.total_bytes) / (1024**3),
        )
        batches = tuple(
            (offset, validated_packages[offset : offset + batch_size])
            for offset in range(0, len(validated_packages), batch_size)
        )
        results: list[WorldLootPackagePatchResult] = []
        try:
            if worker_count == 1:
                completed_count = 0
                for offset, package_batch in batches:
                    batch_results = self._patch_package_batch(
                        root, mappings, len(validated_packages), offset, package_batch, request_entries,
                        scheduler=None,
                    )
                    results.extend(batch_results)
                    completed_count += len(package_batch)
                    if progress:
                        progress(completed_count, len(validated_packages))
            else:
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="cv2-world-loot") as executor:
                    futures = {
                        executor.submit(
                            self._patch_package_batch,
                            root, mappings, len(validated_packages), offset, package_batch, request_entries,
                            scheduler=scheduler,
                        ): (offset, len(package_batch))
                        for offset, package_batch in batches
                    }
                    completed_count = 0
                    for future in as_completed(futures):
                        _, batch_size_for_future = futures[future]
                        results.extend(future.result())
                        completed_count += batch_size_for_future
                        if progress:
                            progress(completed_count, len(validated_packages))
            LOGGER.info(
                "World Loot helper pool complete configured_workers=%d maximum_active=%d batches=%d",
                worker_count,
                max(1, scheduler.maximum_active_jobs),
                len(batches),
            )
        except Exception:
            _remove_all_package_outputs(validated_packages, root)
            raise
        order = {package.input_asset.resolve(): index for index, package in enumerate(validated_packages)}
        results.sort(key=lambda result: order[result.input_path.resolve()])
        return tuple(results)

    # Patch package batch
    def _patch_package_batch(
        self,
        root: Path,
        mappings: Path,
        package_count: int,
        offset: int,
        package_batch: list[WorldLootPackagePatch],
        request_entries: list[dict[str, Any]],
        *,
        scheduler: MemoryBudgetScheduler | None,
    ) -> list[WorldLootPackagePatchResult]:
        """Run one isolated helper batch and validate all its returned packages."""
        entry_batch = [
            {
                **request_entries[index],
                "packageIndex": index + 1,
                "packageCount": package_count,
            }
            for index in range(offset, offset + len(package_batch))
        ]
        manifest = root / f".cv2-world-loot-patch-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps({"packages": entry_batch}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        isolated_mappings: Path | None = None
        try:
            batch_mappings = mappings
            if scheduler is not None:
                isolated_mappings = root / f".cv2-world-loot-mappings-{uuid.uuid4().hex}.usmap"
                try:
                    shutil.copyfile(mappings, isolated_mappings)
                except OSError as error:
                    raise WorldLootSerializationError(
                        f"Could not prepare an isolated mapping copy for parallel world-loot patching: {error}"
                    ) from error
                batch_mappings = isolated_mappings
            command = uassetapi_command(
                self.helper_project, self.dotnet_executable,
                "patch-packages", str(root), str(batch_mappings), self.engine_version, str(manifest),
                no_restore=True,
            )
            helper_env = os.environ.copy()
            helper_env["DOTNET_GCHeapHardLimit"] = DOTNET_GC_HEAP_HARD_LIMIT
            package_started = time.perf_counter()
            try:
                if scheduler is None:
                    completed = self.runner(
                        command, check=False, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=self.timeout_seconds, cwd=root, env=helper_env,
                    )
                else:
                    worker_key = f"world-loot-helper-{threading.get_ident()}"
                    with scheduler.job(worker_key, estimated_bytes=DEFAULT_UASSET_JOB_BYTES):
                        completed = self.runner(
                            command, check=False, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=self.timeout_seconds, cwd=root, env=helper_env,
                        )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise WorldLootSerializationError(f"Could not run UAssetAPI batch helper: {error}") from error
            elapsed = time.perf_counter() - package_started
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-3000:]
                completed_packages = re.findall(r"World Loot package (\d+)/(\d+) ", detail)
                failed_package_number = int(completed_packages[-1][0]) + 1 if completed_packages else offset + 1
                local_failed_index = failed_package_number - offset - 1
                if 0 <= local_failed_index < len(package_batch):
                    failed_package = package_batch[local_failed_index]
                    patch_context = ", ".join(
                        f"export {patch.export_index} {patch.export_name} reward {patch.reward_index}"
                        for patch in failed_package.patches
                    )
                    detail = (
                        f"failed package {failed_package_number}/{package_count} "
                        f"{failed_package.input_asset.name} ({patch_context}); {detail}"
                    )
                raise WorldLootSerializationError(
                    f"UAssetAPI helper rejected the world-loot batch (exit {completed.returncode}): {detail}"
                )

            input_size = sum(
                path.stat().st_size
                for package in package_batch
                for path in _package_files(package.input_asset)
                if path.is_file()
            )
            LOGGER.info(
                "World Loot helper batch packages %d-%d/%d count=%d assignments=%d input=%.1f MiB helper=%.2fs configuration=Release managed_heap_limit=2048 MiB",
                offset + 1, offset + len(package_batch), package_count, len(package_batch),
                sum(len(package.patches) for package in package_batch),
                input_size / (1024 * 1024), elapsed,
            )
            for line in (completed.stderr or "").splitlines():
                if line.strip():
                    LOGGER.info("%s", line.strip())
            try:
                output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
                payload: Any = json.loads(output_lines[-1])
                if not isinstance(payload, list) or len(payload) != len(package_batch):
                    raise ValueError("helper package result count did not match the request")
                by_source = {str(package.input_asset): package for package in package_batch}
                returned: set[Path] = set()
                results: list[WorldLootPackagePatchResult] = []
                for package_result in payload:
                    source = Path(package_result["inputFile"]).resolve()
                    output = Path(package_result["outputFile"]).resolve()
                    request = by_source.get(str(source))
                    if request is None or output != request.output_asset or source in returned:
                        raise ValueError("helper returned an unexpected or duplicate package path")
                    returned.add(source)
                    patches_payload = package_result["patches"]
                    if package_result.get("reopenedAndValidated") is not True or len(patches_payload) != len(request.patches):
                        raise ValueError("helper package did not reopen and validate every patch")
                    if package_result.get("rawPayloadPreserved") is not True:
                        raise ValueError("helper did not confirm raw preservation of non-target exports and registered fields")
                    if not output.is_file() or not output.with_suffix(".uexp").is_file():
                        raise ValueError("helper package output or export sidecar is missing")
                    patch_results: list[WorldLootPatchResult] = []
                    expected_by_slot = {(patch.export_index, patch.reward_index): patch for patch in request.patches}
                    for patch_payload in patches_payload:
                        slot = (patch_payload["exportIndex"], patch_payload["rewardIndex"])
                        expected_slot = slot
                        patch = expected_by_slot.get(slot)
                        if patch is None and request.blueprint_schema_package is not None:
                            expected_slot = next((
                                key for key, value in expected_by_slot.items()
                                if key[0] == 0
                                and key[1] == patch_payload["rewardIndex"]
                                and value.export_name == patch_payload.get("exportName")
                            ), slot)
                            patch = expected_by_slot.get(expected_slot)
                        original = _reward_from_json(patch_payload["original"])
                        assigned = _reward_from_json(patch_payload["assigned"])
                        if (
                            patch is None
                            or patch_payload.get("reopenedAndValidated") is not True
                            or patch_payload.get("exportName") != patch.export_name
                            or not _reward_matches(original, patch.expected_original)
                            or not _reward_matches(assigned, patch.assigned, assigned=True)
                            or Path(patch_payload["outputFile"]).resolve() != output
                            or not isinstance(patch_payload.get("uniqueSaveId"), str)
                            or (patch.require_unique_save_id and not patch_payload["uniqueSaveId"])
                            or (not patch.require_unique_save_id and patch_payload["uniqueSaveId"] != "")
                        ):
                            raise ValueError("helper patch result did not match its catalog-verified request")
                        del expected_by_slot[expected_slot]
                        patch_results.append(WorldLootPatchResult(
                            patch.export_name, patch_payload["exportIndex"], patch.reward_index,
                            patch_payload["uniqueSaveId"],
                            _retain_requested_identity(original, patch.expected_original),
                            _retain_requested_identity(assigned, patch.assigned),
                            output,
                        ))
                    if expected_by_slot:
                        raise ValueError("helper omitted one or more requested reward patches")
                    results.append(WorldLootPackagePatchResult(source, output, tuple(patch_results)))
                if returned != {package.input_asset for package in package_batch}:
                    raise ValueError("helper omitted one or more requested packages")
                return results
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise WorldLootSerializationError(
                    f"UAssetAPI helper returned invalid batch validation output: {error}"
                ) from error
        finally:
            if manifest.is_file() and _is_within(manifest, root):
                manifest.unlink()
            if isolated_mappings is not None and isolated_mappings.is_file() and _is_within(isolated_mappings, root):
                isolated_mappings.unlink()

    # Patch healing upgrades
    def patch_healing_upgrades(
        self,
        *,
        work_root: Path,
        mappings_path: Path,
        packages: Iterable[HealingUpgradePackagePatch],
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[HealingUpgradePackagePatchResult, ...]:
        """Give every per-instance ArticleBulk override a unique Blueprint/CDO package."""
        root = work_root.expanduser().resolve(strict=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        package_list = tuple(packages)
        if not package_list:
            return ()
        if not root.is_dir() or not self.helper_project.is_file() or not mappings.is_file():
            raise WorldLootSerializationError("Healing patch workspace, UAssetAPI helper, or mapping file is unavailable.")

        requests: list[dict[str, Any]] = []
        seen_inputs: set[Path] = set()
        seen_outputs: set[Path] = set()
        seen_clone_paths: set[str] = set()
        seen_clone_outputs: set[Path] = set()
        for package in package_list:
            source = package.input_asset.expanduser().resolve(strict=True)
            output = package.output_asset.expanduser().resolve()
            if not _is_within(source, root) or not _is_within(output, root):
                raise WorldLootSerializationError("Healing map packages must stay inside the generation workspace.")
            if source == output or source.parent != output.parent or source.suffix.casefold() != ".umap" or output.suffix.casefold() != ".umap":
                raise WorldLootSerializationError("Healing map output must be a new sibling .umap package.")
            if source in seen_inputs or output in seen_outputs or any(path.exists() for path in _package_files(output)):
                raise WorldLootSerializationError("Healing map inputs/outputs must be unique and output files must not exist.")
            if not package.patches:
                raise WorldLootSerializationError("Each healing map package requires at least one physical location.")
            seen_inputs.add(source)
            seen_outputs.add(output)
            patch_entries: list[dict[str, Any]] = []
            seen_exports: set[int] = set()
            for patch in package.patches:
                if patch.export_index < 1 or patch.cdo_export_index < 1 or not patch.export_name.strip() or not patch.cdo_export_name.strip():
                    raise WorldLootSerializationError("Healing actor/CDO identities require positive export indices and names.")
                if patch.export_index in seen_exports:
                    raise WorldLootSerializationError("A physical actor may have only one instance override patch per map.")
                seen_exports.add(patch.export_index)
                blueprint = patch.blueprint_input.expanduser().resolve(strict=True)
                clone = patch.blueprint_output.expanduser().resolve()
                if not _is_within(blueprint, root) or blueprint.suffix.casefold() != ".uasset":
                    raise WorldLootSerializationError("Override source Blueprint must be staged inside the workspace.")
                if not _is_within(clone, root) or clone.suffix.casefold() != ".uasset":
                    raise WorldLootSerializationError("Healing Blueprint clone must stay in the workspace as a .uasset package.")
                if blueprint == clone or clone in seen_clone_outputs or any(path.exists() for path in _package_files(clone)):
                    raise WorldLootSerializationError("Healing Blueprint clone output must be unique and must not overwrite a package.")
                virtual_path = patch.clone_package_path
                if (not virtual_path.startswith("/Game/CV2Randomizer/Healing/") or "\\" in virtual_path
                        or any(part in {"", ".", ".."} for part in virtual_path.split("/")[1:])):
                    raise WorldLootSerializationError("Healing clone paths must be canonical under /Game/CV2Randomizer/Healing/.")
                if virtual_path.casefold() in seen_clone_paths:
                    raise WorldLootSerializationError("Healing clone virtual paths must be unique per physical location.")
                if patch.expected_original.soft_object_path_identity is None or patch.assigned.soft_object_path_identity is None:
                    raise WorldLootSerializationError("Healing rewards require exact serialized ArticleDataPath FName identities.")
                if patch.expected_original.count != patch.assigned.count or patch.expected_original.b_notification != patch.assigned.b_notification:
                    raise WorldLootSerializationError("Healing rewards must preserve Count and bNotification.")
                seen_clone_outputs.add(clone)
                seen_clone_paths.add(virtual_path.casefold())
                patch_entries.append({
                    "exportIndex": patch.export_index,
                    "exportName": patch.export_name,
                    "expectedClassPackage": patch.class_package,
                    "expectedClassName": patch.class_name,
                    "cdoExportIndex": patch.cdo_export_index,
                    "cdoExportName": patch.cdo_export_name,
                    "blueprintInputFile": str(blueprint),
                    "blueprintOutputFile": str(clone),
                    "clonePackagePath": virtual_path,
                    "expected": _reward_to_json(patch.expected_original),
                    "assigned": _reward_to_json(patch.assigned),
                    "nameMapValues": list(patch.name_map_values),
                })
            map_name_values = tuple(package.map_name_map_values)
            if (
                len(map_name_values) > 10000
                or any(not isinstance(value, str) or not value for value in map_name_values)
                or len(set(map_name_values)) != len(map_name_values)
            ):
                raise WorldLootSerializationError("Healing map NameMap values must be unique, non-empty, and within the 10000-name limit.")
            requests.append({
                "inputFile": str(source),
                "outputFile": str(output),
                "patches": patch_entries,
                "mapNameMapValues": list(map_name_values),
                "classExports": [
                    {"exportIndex": index, "exportName": name}
                    for index, name in package.discovery_exports
                ],
                "worldLootFields": [
                    {
                        "exportIndex": export_index,
                        "rewardIndex": reward_index,
                        "exportName": export_name,
                        "expected": _reward_to_json(expected),
                    }
                    for export_index, reward_index, export_name, expected in package.world_loot_fields
                ],
            })

        manifest = root / f".cv2-healing-upgrades-{__import__('uuid').uuid4().hex}.json"
        manifest.write_text(json.dumps({"packages": requests}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        succeeded = False
        try:
            command = uassetapi_command(
                self.helper_project, self.dotnet_executable,
                "patch-healing-upgrades", str(root), str(mappings), self.engine_version, str(manifest),
                no_restore=True,
            )
            helper_env = os.environ.copy()
            helper_env["DOTNET_GCHeapHardLimit"] = DOTNET_GC_HEAP_HARD_LIMIT
            helper_started = time.perf_counter()
            try:
                completed = self.runner(
                    command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=self.timeout_seconds, cwd=root, env=helper_env,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise WorldLootSerializationError(f"Could not run the Healing Upgrade UAssetAPI helper: {error}") from error
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise WorldLootSerializationError(f"Healing Upgrade helper failed (exit {completed.returncode}): {detail}")
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            helper_payload = json.loads(lines[-1])
            helper_wall_ms = (time.perf_counter() - helper_started) * 1000
            profiled_output = isinstance(helper_payload, dict)
            if profiled_output:
                rows = helper_payload.get("packages")
                package_total_ms = helper_payload.get("packageTotalMilliseconds")
                internal_startup_ms = helper_payload.get("helperStartupAndMappingsMilliseconds")
                if not isinstance(package_total_ms, (int, float)) or not isinstance(internal_startup_ms, (int, float)):
                    raise ValueError("helper timing profile is missing package or startup durations")
                helper_profile = {
                    "process_wall_ms": helper_wall_ms,
                    "package_total_ms": float(package_total_ms),
                    "outside_packages_ms": max(0.0, helper_wall_ms - float(package_total_ms)),
                    "internal_startup_and_mappings_ms": float(internal_startup_ms),
                }
            else:
                rows = helper_payload
                helper_profile = {"process_wall_ms": helper_wall_ms}
            if not isinstance(rows, list) or len(rows) != len(package_list):
                raise ValueError("helper result package count differs from the request")
            expected_by_input = {package.input_asset.resolve(): package for package in package_list}
            seen_results: set[Path] = set()
            results: list[HealingUpgradePackagePatchResult] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("helper returned a malformed package result")
                input_path = Path(row.get("inputFile", "")).resolve()
                package = expected_by_input.get(input_path)
                output_path = Path(row.get("outputFile", "")).resolve()
                if package is None or output_path != package.output_asset.resolve() or input_path in seen_results:
                    raise ValueError("helper returned an unexpected or duplicate map package")
                seen_results.add(input_path)
                if (row.get("patchCount") != len(package.patches) or row.get("reopenedAndValidated") is not True
                        or row.get("rawPayloadPreserved") is not True):
                    raise ValueError("helper did not validate the map imports and preserved payload")
                if not output_path.is_file() or not output_path.with_suffix(".uexp").is_file():
                    raise ValueError("helper omitted the validated map or its .uexp sidecar")
                class_rows = row.get("classInspections")
                if not isinstance(class_rows, list) or len(class_rows) != len(package.discovery_exports):
                    raise ValueError("helper returned an incomplete per-map class discovery result")
                class_inspections: list[dict[str, Any]] = []
                for (expected_index, expected_name), class_row in zip(package.discovery_exports, class_rows, strict=True):
                    if not isinstance(class_row, dict):
                        raise ValueError("helper returned an invalid per-map class record")
                    if (
                        class_row.get("exportIndex") != expected_index
                        or class_row.get("exportName") != expected_name
                        or not isinstance(class_row.get("className"), str)
                        or not isinstance(class_row.get("packagePath"), str)
                    ):
                        raise ValueError("helper class result did not match the requested map export identity")
                    class_inspections.append(dict(class_row))

                world_field_rows = row.get("worldLootFields")
                if not isinstance(world_field_rows, list) or len(world_field_rows) != len(package.world_loot_fields):
                    raise ValueError("helper returned an incomplete per-map World Loot descriptor result")
                world_loot_fields: list[dict[str, Any]] = []
                for expected_field, field_row in zip(package.world_loot_fields, world_field_rows, strict=True):
                    export_index, reward_index, export_name, expected_reward = expected_field
                    if not isinstance(field_row, dict):
                        raise ValueError("helper returned an invalid per-map World Loot field")
                    if (
                        field_row.get("exportIndex") != export_index
                        or field_row.get("rewardIndex") != reward_index
                        or field_row.get("exportName") != export_name
                        or type(field_row.get("available")) is not bool
                    ):
                        raise ValueError("helper World Loot field did not match the requested map reward")
                    offset = field_row.get("offset")
                    expected_bytes_hex = field_row.get("expectedBytesHex")
                    if field_row["available"]:
                        if (
                            type(offset) is not int or offset < 0
                            or not isinstance(expected_bytes_hex, str) or len(expected_bytes_hex) != 32
                        ):
                            raise ValueError("helper returned an invalid available World Loot field offset")
                    elif offset is not None or not isinstance(field_row.get("unavailableReason"), str):
                        raise ValueError("helper returned an invalid unavailable World Loot field")
                    world_loot_fields.append({
                        "export_index": export_index,
                        "reward_index": reward_index,
                        "export_name": export_name,
                        "offset": offset,
                        "expected_bytes_hex": expected_bytes_hex,
                        "expected_identity": (
                            _identity_to_json(expected_reward.soft_object_path_identity)
                            if expected_reward.soft_object_path_identity is not None else None
                        ),
                        "available": field_row["available"],
                        "unavailable_reason": field_row.get("unavailableReason"),
                    })
                open_counts = {
                    "discovery_open_count": row.get("discoveryOpenCount"),
                    "preparation_reload_count": row.get("preparationReloadCount"),
                    "serialization_reopen_count": row.get("serializationReopenCount"),
                    "validation_reopen_count": row.get("validationReopenCount"),
                }
                if any(type(value) is not int or value < 0 for value in open_counts.values()):
                    raise ValueError("helper returned invalid per-map structural open counts")
                map_descriptor = None
                map_name_map_before = row.get("nameMapCountBefore")
                map_name_map_after = row.get("nameMapCountAfter")
                map_names_added = row.get("namesAdded")
                map_header_size = row.get("headerSize")
                map_uexp_size = row.get("uexpSize")
                map_hashes = row.get("packageFileHashes")
                map_name_indices = row.get("nameMapIndices")
                if profiled_output:
                    if (
                        type(map_name_map_before) is not int or map_name_map_before < 0
                        or type(map_name_map_after) is not int or map_name_map_after < map_name_map_before
                        or not isinstance(map_names_added, list)
                        or any(not isinstance(name, str) for name in map_names_added)
                        or map_name_map_after - map_name_map_before != len(map_names_added)
                        or type(map_header_size) is not int or map_header_size <= 0
                        or type(map_uexp_size) is not int or map_uexp_size <= 0
                        or not isinstance(map_hashes, dict)
                        or any(not isinstance(key, str) or not key.startswith(".") or not _is_sha256(value)
                               for key, value in map_hashes.items())
                        or not isinstance(map_name_indices, dict)
                        or any(not isinstance(key, str) or type(value) is not int or value < 0
                               for key, value in map_name_indices.items())
                    ):
                        raise ValueError("helper returned an invalid prepared Healing map descriptor")
                    if package.package_path is not None:
                        map_descriptor = {
                            "version": 3,
                            "package_path": package.package_path,
                            "header_sha256": map_hashes.get(".umap"),
                            "uexp_sha256": map_hashes.get(".uexp"),
                            "header_size": map_header_size,
                            "uexp_size": map_uexp_size,
                            "package_file_hashes": map_hashes,
                            "name_map_indices": map_name_indices,
                            "name_map_count_before": map_name_map_before,
                            "name_map_count_after": map_name_map_after,
                            "names_added_count": len(map_names_added),
                            "fields": world_loot_fields,
                            "prepared_asset": None,
                            "preparation_reason": None,
                            "unavailable_reason": None,
                        }
                expected_patches = {patch.clone_package_path: patch for patch in package.patches}
                clone_rows = row.get("clones")
                if not isinstance(clone_rows, list) or len(clone_rows) != len(expected_patches):
                    raise ValueError("helper clone count differs from the requested physical locations")
                clones: list[HealingUpgradeCloneResult] = []
                seen_clone_results: set[str] = set()
                for clone_row in clone_rows:
                    if not isinstance(clone_row, dict):
                        raise ValueError("helper returned a malformed Blueprint clone result")
                    virtual_path = clone_row.get("clonePackagePath")
                    patch = expected_patches.get(virtual_path)
                    clone_input = Path(clone_row.get("inputFile", "")).resolve()
                    clone_output = Path(clone_row.get("outputFile", "")).resolve()
                    if (patch is None or virtual_path in seen_clone_results
                            or clone_input != patch.blueprint_input.resolve()
                            or clone_output != patch.blueprint_output.resolve()):
                        raise ValueError("helper returned an unexpected or duplicate Blueprint clone")
                    seen_clone_results.add(virtual_path)
                    original = _reward_from_json(clone_row["original"])
                    assigned = _reward_from_json(clone_row["assigned"])
                    if (clone_row.get("reopenedAndValidated") is not True or clone_row.get("rawPayloadPreserved") is not True
                            or not _reward_matches(original, patch.expected_original)
                            or not _reward_matches(assigned, patch.assigned, assigned=True)
                            or not clone_output.is_file() or not clone_output.with_suffix(".uexp").is_file()):
                        raise ValueError("helper did not confirm the patched CDO reward and preserved Blueprint payload")
                    clone_timing = clone_row.get("timing")
                    if clone_timing is not None and (
                        not isinstance(clone_timing, dict)
                        or any(not isinstance(value, (int, float)) for value in clone_timing.values())
                    ):
                        raise ValueError("helper returned invalid Healing clone timings")
                    helper_name_indices = clone_row.get("nameMapIndices")
                    package_hashes = clone_row.get("packageFileHashes")
                    name_map_before = clone_row.get("nameMapCountBefore")
                    name_map_after = clone_row.get("nameMapCountAfter")
                    names_added = clone_row.get("namesAdded")
                    descriptor = None
                    if profiled_output:
                        if (
                            not isinstance(helper_name_indices, dict)
                            or any(not isinstance(key, str) or type(value) is not int or value < 0
                                   for key, value in helper_name_indices.items())
                            or not isinstance(package_hashes, dict)
                            or any(not isinstance(key, str) or not key.startswith(".") or not _is_sha256(value)
                                   for key, value in package_hashes.items())
                        ):
                            raise ValueError("helper omitted Healing clone NameMap or file-integrity descriptors")
                        field_offset = clone_row.get("fieldOffset")
                        expected_bytes_hex = clone_row.get("expectedBytesHex")
                        header_size = clone_row.get("headerSize")
                        uexp_size = clone_row.get("uexpSize")
                        if (
                            type(field_offset) is not int or field_offset < 0
                            or not isinstance(expected_bytes_hex, str) or len(expected_bytes_hex) != 32
                            or type(header_size) is not int or header_size <= 0
                            or type(uexp_size) is not int or uexp_size <= 0
                            or field_offset + 16 > uexp_size
                        ):
                            raise ValueError("helper returned an invalid Healing clone ArticleDataPath descriptor")
                        if (
                            type(name_map_before) is not int or name_map_before < 0
                            or type(name_map_after) is not int or name_map_after < name_map_before
                            or not isinstance(names_added, list) or any(not isinstance(name, str) for name in names_added)
                        ):
                            raise ValueError("helper returned invalid Healing clone NameMap preparation details")
                        descriptor = {
                            "version": 3,
                            "package_path": virtual_path,
                            "header_sha256": package_hashes.get(".uasset"),
                            "uexp_sha256": package_hashes.get(".uexp"),
                            "header_size": header_size,
                            "uexp_size": uexp_size,
                            "package_file_hashes": package_hashes,
                            "name_map_indices": helper_name_indices,
                            "name_map_count_before": name_map_before,
                            "name_map_count_after": name_map_after,
                            "names_added_count": name_map_after - name_map_before,
                            "fields": [{
                                "export_index": patch.cdo_export_index,
                                "reward_index": 0,
                                "export_name": patch.cdo_export_name,
                                "offset": field_offset,
                                "expected_bytes_hex": expected_bytes_hex,
                                "expected_identity": _identity_to_json(patch.expected_original.soft_object_path_identity),
                                "available": True,
                                "unavailable_reason": None,
                            }],
                            "prepared_asset": None,
                            "preparation_reason": None,
                            "unavailable_reason": None,
                        }
                    elif not isinstance(names_added, list):
                        names_added = []
                    clones.append(HealingUpgradeCloneResult(
                        clone_input, clone_output, virtual_path,
                        _retain_requested_identity(original, patch.expected_original),
                        _retain_requested_identity(assigned, patch.assigned), True, True,
                        {str(key): float(value) for key, value in clone_timing.items()} if isinstance(clone_timing, dict) else None,
                        descriptor, name_map_before, name_map_after, tuple(names_added),
                    ))
                if seen_clone_results != set(expected_patches):
                    raise ValueError("helper omitted a requested physical-location Blueprint clone")
                results.append(HealingUpgradePackagePatchResult(
                    input_path, output_path, len(package.patches), tuple(clones), True, True,
                    {str(key): float(value) for key, value in row.get("timing", {}).items()}
                    if isinstance(row.get("timing"), dict) else None,
                    helper_profile,
                    map_descriptor, map_name_map_before, map_name_map_after,
                    tuple(map_names_added) if isinstance(map_names_added, list) else (),
                    tuple(class_inspections), tuple(world_loot_fields),
                    open_counts["discovery_open_count"],
                    open_counts["preparation_reload_count"],
                    open_counts["serialization_reopen_count"],
                    open_counts["validation_reopen_count"],
                ))
                if progress:
                    progress(len(results), len(package_list))
            if seen_results != set(expected_by_input):
                raise ValueError("helper omitted a requested Healing Upgrade map package")
            succeeded = True
            return tuple(results)
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorldLootSerializationError(f"UAssetAPI returned invalid Healing Upgrade validation output: {error}") from error
        finally:
            manifest.unlink(missing_ok=True)
            if not succeeded:
                for package in package_list:
                    _remove_package_outputs(package.output_asset.resolve(), root)
                    for patch in package.patches:
                        _remove_package_outputs(patch.blueprint_output.resolve(), root)

    # Patch reward
    def patch_reward(
        self,
        *,
        work_root: Path,
        input_asset: Path,
        output_asset: Path,
        mappings_path: Path,
        export_index: int,
        reward_index: int,
        expected_export_name: str,
        expected_original: ArticleReward,
        assigned: ArticleReward,
    ) -> WorldLootPatchResult:
        """Patch one reward tuple in a staged package and require helper re-open validation."""
        root = work_root.expanduser().resolve(strict=True)
        source = input_asset.expanduser().resolve(strict=True)
        output = output_asset.expanduser().resolve()
        mappings = mappings_path.expanduser().resolve(strict=True)
        project = self.helper_project

        if not root.is_dir():
            raise WorldLootSerializationError("work_root must be a directory.")
        if not _is_within(source, root) or not _is_within(output, root):
            raise WorldLootSerializationError("Input and output packages must stay inside the supplied staging root.")
        if source == output or source.parent != output.parent:
            raise WorldLootSerializationError("Output must be a new sibling package so Blueprint dependencies remain resolvable.")
        if source.suffix.casefold() not in {".umap", ".uasset"} or output.suffix.casefold() != source.suffix.casefold():
            raise WorldLootSerializationError("Input and output must use the same .umap or .uasset extension.")
        if not expected_export_name.strip():
            raise WorldLootSerializationError("expected_export_name is required.")
        if type(export_index) is not int or export_index < 1:
            raise WorldLootSerializationError("Catalog export_index must be a positive Unreal 1-based index.")
        if type(reward_index) is not int or reward_index < 0:
            raise WorldLootSerializationError("reward_index must be zero-based and non-negative.")
        if not project.is_file():
            raise WorldLootSerializationError(f"UAssetAPI helper project was not found: {project}")
        if not mappings.is_file():
            raise WorldLootSerializationError(f"UAssetAPI mapping file was not found: {mappings}")
        if any(path.exists() for path in _package_files(output)):
            raise WorldLootSerializationError("Refusing to overwrite an existing staged output package.")

        command = uassetapi_command(
            project, self.dotnet_executable, "patch", str(root), str(source), str(mappings),
            self.engine_version, str(output), str(export_index), str(reward_index), expected_export_name,
            expected_original.article_data_path, str(expected_original.count),
            str(expected_original.b_notification).lower(), assigned.article_data_path,
            str(assigned.count), str(assigned.b_notification).lower(), no_restore=True,
        )
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
            _remove_package_outputs(output, root)
            raise WorldLootSerializationError(f"Could not run UAssetAPI helper: {error}") from error

        if completed.returncode != 0:
            _remove_package_outputs(output, root)
            detail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise WorldLootSerializationError(
                f"UAssetAPI helper rejected the world-loot patch (exit {completed.returncode}): {detail}"
            )
        try:
            output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
            payload: Any = json.loads(output_lines[-1])
            original = _reward_from_json(payload["original"])
            actual_assigned = _reward_from_json(payload["assigned"])
            result_path = Path(payload["outputFile"]).resolve()
            if (
                payload.get("reopenedAndValidated") is not True
                or payload.get("rawPayloadPreserved") is not True
                or payload.get("exportName") != expected_export_name
                or payload.get("exportIndex") != export_index
                or payload.get("rewardIndex") != reward_index
                or not _reward_matches(original, expected_original)
                or not _reward_matches(actual_assigned, assigned, assigned=True)
                or result_path != output
                or not output.is_file()
                or not output.with_suffix(".uexp").is_file()
            ):
                raise ValueError("helper result did not match the requested verified patch")
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            _remove_package_outputs(output, root)
            raise WorldLootSerializationError(f"UAssetAPI helper returned invalid validation output: {error}") from error

        return WorldLootPatchResult(
            export_name=payload["exportName"],
            export_index=payload["exportIndex"],
            reward_index=payload["rewardIndex"],
            unique_save_id=payload["uniqueSaveId"],
            original=_retain_requested_identity(original, expected_original),
            assigned=_retain_requested_identity(actual_assigned, assigned),
            output_path=output,
        )



# Return reward to JSON
def _reward_to_json(value: ArticleReward) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": value.article_data_path,
        "count": value.count,
        "notification": value.b_notification,
    }
    if value.soft_object_path_identity is not None:
        payload["softObjectPathIdentity"] = _soft_object_identity_to_json(value.soft_object_path_identity)
    return payload


# Remove all package outputs
def _remove_all_package_outputs(packages: Iterable[WorldLootPackagePatch], root: Path) -> None:
    for package in packages:
        output = package.output_asset.resolve()
        if _is_within(output, root):
            _remove_package_outputs(output, root)

# Return reward from JSON
def _reward_from_json(value: dict[str, Any]) -> ArticleReward:
    raw_identity = value.get("softObjectPathIdentity")
    identity = _soft_object_identity_from_json(raw_identity) if raw_identity is not None else None
    return ArticleReward(
        article_data_path=value["path"],
        count=value["count"],
        b_notification=value["notification"],
        soft_object_path_identity=identity,
    )


# Find soft object identity to JSON
def _soft_object_identity_to_json(value: SoftObjectPathIdentity) -> dict[str, Any]:
    return {
        "packageName": {"value": value.package_name.value, "number": value.package_name.number},
        "assetName": {"value": value.asset_name.value, "number": value.asset_name.number},
    }


# Resolve identity to JSON
def _identity_to_json(value: SoftObjectPathIdentity) -> dict[str, Any]:
    return {
        "package_name": {
            "value": value.package_name.value,
            "number": value.package_name.number,
        },
        "asset_name": {
            "value": value.asset_name.value,
            "number": value.asset_name.number,
        },
    }


# Find soft object identity from JSON
def _soft_object_identity_from_json(value: dict[str, Any]) -> SoftObjectPathIdentity:
    package = value["packageName"]
    asset = value["assetName"]
    return SoftObjectPathIdentity(
        FNameIdentity(package["value"], package["number"]),
        FNameIdentity(asset["value"], asset["number"]),
    )


# Return reward matches
def _reward_matches(actual: ArticleReward, expected: ArticleReward, *, assigned: bool = False) -> bool:
    if (
        actual.article_data_path != expected.article_data_path
        or actual.count != expected.count
        or actual.b_notification != expected.b_notification
    ):
        return False
    expected_identity = expected.soft_object_path_identity
    if expected_identity is None and assigned and actual.soft_object_path_identity is not None:
        expected_identity = SoftObjectPathIdentity.from_literal_path(expected.article_data_path)
    return expected_identity is None or actual.soft_object_path_identity == expected_identity


# Retain requested identity
def _retain_requested_identity(actual: ArticleReward, requested: ArticleReward) -> ArticleReward:
    """Keep exact FNames in results only when the request supplied them explicitly."""
    if requested.soft_object_path_identity is None:
        return ArticleReward(actual.article_data_path, actual.count, actual.b_notification)
    return actual


# Build package files
def _package_files(header: Path) -> tuple[Path, ...]:
    return (header, *(header.with_suffix(extension) for extension in (".uexp", ".ubulk", ".uptnl")))


# Remove package outputs
def _remove_package_outputs(header: Path, root: Path) -> None:
    for path in _package_files(header):
        if path.is_symlink() or not path.is_file() or not _is_within(path, root):
            continue
        path.unlink()


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


# Check SHA-256
def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )
