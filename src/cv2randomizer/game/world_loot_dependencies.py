"""Dependency staging for parsing generated World Loot map actors."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from .asset_extraction import (
    AssetExtractionError,
    ExtractedAsset,
    IoStoreAssetExtractor,
    MaterializedAsset,
    materialize_extracted_assets,
)
from .catalogs import (
    SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS,
    WorldLootLocation,
    is_valid_scripted_reward_only_location,
    is_world_map_package,
)
from cv2randomizer.core.subprocesses import run_hidden
from cv2randomizer.core.memory_budget import MemoryBudgetScheduler
from cv2randomizer.core.resources import resolve_uassetapi_helper, uassetapi_command
from .world_loot_serialization import DOTNET_GC_HEAP_HARD_LIMIT, SUPPORTED_ENGINE_VERSION
from .world_loot_serialization import (
    ArticleReward,
    HealingUpgradePackagePatch,
    HealingUpgradePatch,
    WorldLootSerializer,
)
from .version_compatibility import is_game_data_layout_mismatch

LOGGER = logging.getLogger(__name__)
_PREPARED_CACHE_RENAME_RETRIES = 7
_PREPARED_CACHE_RENAME_RETRY_SECONDS = 0.05


# Allow audited flag pickups
def _can_rebind_profile_scripted_location(
    location: WorldLootLocation,
    target_export_index: int | None = None,
) -> bool:
    if location.scripted_reward_mode is None:
        return True
    if (
        location.scripted_reward_mode != SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS
        or location.actor_class != "BP_PickupItem_C"
    ):
        return False
    candidate = (
        replace(location, export_index=target_export_index)
        if target_export_index is not None else location
    )
    return bool(
        is_valid_scripted_reward_only_location(candidate)
    )


# Publish prepared cache atomically
def _publish_prepared_cache_entry(temporary: Path, destination: Path) -> None:
    """Retry brief Windows file locks without replacing existing cache data."""
    for attempt in range(_PREPARED_CACHE_RENAME_RETRIES + 1):
        if destination.exists() or destination.is_symlink():
            raise WorldLootDependencyError(
                "Prepared World Loot cache destination appeared during publication."
            )
        try:
            temporary.replace(destination)
            return
        except PermissionError:
            if destination.exists() or destination.is_symlink():
                raise WorldLootDependencyError(
                    "Prepared World Loot cache destination appeared during publication."
                )
            if attempt >= _PREPARED_CACHE_RENAME_RETRIES:
                raise
            time.sleep(min(
                _PREPARED_CACHE_RENAME_RETRY_SECONDS * (2 ** attempt),
                0.5,
            ))
    raise WorldLootDependencyError("Prepared World Loot cache publication did not complete.")

_BatchItem = TypeVar("_BatchItem")
_BatchResult = TypeVar("_BatchResult")
_HEALING_UPGRADE_LOCATION_TYPES = frozenset({
    "HEALING_UPGRADE_GOLDEN_SEED",
    "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
})
_MAP_INSPECTION_BATCH_SIZE = 8
_BLUEPRINT_PARENT_BATCH_SIZE = 16
_WORLD_LOOT_NAMEMAP_BATCH_SIZE = 24
_BASELINE_WORLD_LOOT_VERSIONS = frozenset({"2.0.3", "2.0.3.0"})


# Decide whether actor validation applies
def _actor_schema_check_required(
    game_version: str,
    display_version: str | None,
    compatibility_profile: str | None,
) -> bool:
    """Use the legacy actor check only outside the 2.0.3 baseline."""
    if compatibility_profile is not None:
        return True
    return not any(
        isinstance(candidate, str) and candidate.strip() in _BASELINE_WORLD_LOOT_VERSIONS
        for candidate in (game_version, display_version)
    )


@dataclass(slots=True)
class PackagePreparationContext:
    """Discovery/preparation metadata retained for one staged map package."""

    package_path: str
    source_asset: MaterializedAsset
    locations: tuple[WorldLootLocation, ...]
    required_name_values: tuple[str, ...]
    healing_locations: tuple[WorldLootLocation, ...]
    class_records: list[dict[str, Any]] | None = None
    binary_descriptor: dict[str, Any] | None = None


# Group healing pickups by map
def _healing_instance_locations_by_map(
    by_package: dict[str, list[WorldLootLocation]],
) -> dict[str, tuple[WorldLootLocation, ...]]:
    """Keep Extract-time Healing preparation scoped to the two Healing pickup types."""
    grouped = {
        package.casefold(): tuple(
            location for location in locations
            if (
                location.destination_kind == "INSTANCE_ARTICLE_BULK_OVERRIDE"
                and location.location_type in _HEALING_UPGRADE_LOCATION_TYPES
            )
        )
        for package, locations in by_package.items()
    }
    return {key: values for key, values in grouped.items() if values}


# Run batch with failure isolation
def _run_batch_with_failure_isolation(
    items: Sequence[_BatchItem],
    run_batch: Callable[[tuple[_BatchItem, ...]], Sequence[_BatchResult]],
    on_failure: Callable[[_BatchItem, Exception], None],
    on_complete: Callable[[int], None],
) -> tuple[_BatchResult, ...]:
    """Run a batch efficiently, bisecting failures until unsafe items are isolated."""
    batch = tuple(items)
    if not batch:
        return ()
    try:
        result = tuple(run_batch(batch))
    except Exception as error:
        if len(batch) == 1:
            on_failure(batch[0], error)
            on_complete(1)
            return ()
        midpoint = len(batch) // 2
        return (
            *_run_batch_with_failure_isolation(batch[:midpoint], run_batch, on_failure, on_complete),
            *_run_batch_with_failure_isolation(batch[midpoint:], run_batch, on_failure, on_complete),
        )
    on_complete(len(batch))
    return result


# Return size bounded batches
def _size_bounded_batches(
    items: Sequence[_BatchItem],
    *,
    maximum_items: int,
) -> tuple[tuple[_BatchItem, ...], ...]:
    """Group independent sequential helper jobs while keeping memory bounded."""
    if maximum_items <= 0:
        raise ValueError("Batch size must be positive.")
    return tuple(
        tuple(items[offset:offset + maximum_items])
        for offset in range(0, len(items), maximum_items)
    )
    on_complete(len(batch))
    return result


class WorldLootDependencyError(RuntimeError):
    """Raised when a target map's generated Blueprint hierarchy cannot be staged."""


@dataclass(frozen=True, slots=True)
class WorldLootDependencyStage:
    """Materialized generated maps and the Blueprint packages required to parse them."""

    content_root: Path
    maps: tuple[MaterializedAsset, ...]
    blueprint_packages: tuple[MaterializedAsset, ...]
    class_packages: tuple[str, ...]
    dependency_index: dict[str, Any] | None = None


class WorldLootDependencyResolver:
    """Extract and stage map actor classes in a standard Unreal Content layout."""

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
        self._helper_build_lock = threading.Lock()
        self._helper_ready = False
        if engine_version != SUPPORTED_ENGINE_VERSION:
            raise ValueError(f"World-loot dependency inspection supports only {SUPPORTED_ENGINE_VERSION}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")

    # Stage for locations
    def stage_for_locations(
        self,
        *,
        game_root: Path,
        locations: Iterable[WorldLootLocation],
        work_root: Path,
        mappings_path: Path,
        game_version: str,
        data_revision: str,
        display_version: str | None = None,
        compatibility_profile: str | None = None,
        extractor: IoStoreAssetExtractor | None = None,
        dependency_index: dict[str, Any] | None = None,
        candidate_name_values: Iterable[str] | None = None,
        prepared_cache_root: Path | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        memory_scheduler: MemoryBudgetScheduler | None = None,
        diagnostics_callback: Callable[[str, str], None] | None = None,
        helper_metrics_callback: Callable[[str, object], None] | None = None,
        profile_callback: Callable[..., None] | None = None,
    ) -> WorldLootDependencyStage:
        """Extract destination maps and recursively resolve generated Blueprint parents."""
        root = work_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        mappings = mappings_path.expanduser().resolve(strict=True)
        scheduler = memory_scheduler or MemoryBudgetScheduler()
        candidate_names = None if candidate_name_values is None else tuple(sorted(set(candidate_name_values)))
        if not mappings.is_file():
            raise WorldLootDependencyError("World-loot mappings file does not exist.")
        if not self.helper_project.is_file():
            raise WorldLootDependencyError(f"UAssetAPI helper project was not found: {self.helper_project}")
        by_package: dict[str, list[WorldLootLocation]] = {}
        asset_source = extractor or self.extractor
        package_inventory = getattr(asset_source, "inventory", None)
        if package_inventory is None:
            package_inventory = getattr(asset_source, "_observed_package_inventory", None)
        if package_inventory is None:
            inventory_reader = getattr(asset_source, "inspect_package_inventory", None)
            if callable(inventory_reader):
                try:
                    package_inventory = inventory_reader(game_root)
                except (AssetExtractionError, OSError, RuntimeError, ValueError) as error:
                    LOGGER.info("World Loot schema checks will use extraction results; inventory lookup failed: %s", error)
        for location in locations:
            if not is_world_map_package(location.package_path):
                raise WorldLootDependencyError(f"Unsupported destination map package: {location.package_path}")
            if not location.package_path.rsplit("/", 1)[-1] or "." in location.package_path.rsplit("/", 1)[-1]:
                raise WorldLootDependencyError(f"Invalid destination map package path: {location.package_path}")
            by_package.setdefault(location.package_path, []).append(location)
        if not by_package:
            return WorldLootDependencyStage(root / "source" / "Content", (), (), ())

        try:
            extracted_maps = asset_source.extract_assets(
                game_root,
                sorted(by_package, key=str.casefold),
                game_version=game_version,
                data_revision=data_revision,
                progress=progress,
            )
            if progress:
                progress("materializing-maps", len(extracted_maps), len(extracted_maps))
            content_root = root / "source" / "Content"
            maps = materialize_extracted_assets(
                extracted_maps,
                content_root,
                profile_callback=profile_callback,
                profile_kind="world_map_sources",
            )
            if dependency_index is not None:
                indexed_classes: set[str] = set()
                index_maps = dependency_index.get("maps")
                if (
                    dependency_index.get("schema_version") != 1
                    or dependency_index.get("game_version") != game_version
                    or dependency_index.get("catalog_revision") != data_revision
                    or not isinstance(index_maps, dict)
                ):
                    raise WorldLootDependencyError("Cached World Loot dependency index does not match the active cache identity.")
                indexed_blueprints = dependency_index.get("blueprint_packages")
                location_descriptors = dependency_index.get("world_loot_location_descriptors")
                if not isinstance(indexed_blueprints, list) or any(
                    not isinstance(package, str) for package in indexed_blueprints
                ):
                    raise WorldLootDependencyError("Cached World Loot Blueprint dependency list is invalid.")
                if not isinstance(location_descriptors, dict):
                    raise WorldLootDependencyError("Cached World Loot prepared location descriptors are missing.")
                raw_incompatible_maps = dependency_index.get("incompatible_world_loot_maps", [])
                raw_incompatible_locations = dependency_index.get("incompatible_world_loot_locations", [])
                if not isinstance(raw_incompatible_maps, list) or not isinstance(raw_incompatible_locations, list):
                    raise WorldLootDependencyError("Cached World Loot compatibility records are invalid.")
                incompatible_maps: set[str] = set()
                for row in raw_incompatible_maps:
                    if not isinstance(row, dict) or not isinstance(row.get("package_path"), str):
                        raise WorldLootDependencyError("Cached World Loot map compatibility record is invalid.")
                    incompatible_maps.add(row["package_path"].casefold())
                incompatible_locations: set[tuple[str, int]] = set()
                for row in raw_incompatible_locations:
                    if (
                        not isinstance(row, dict)
                        or not isinstance(row.get("package_path"), str)
                        or not isinstance(row.get("export_index"), int)
                        or isinstance(row.get("export_index"), bool)
                        or row["export_index"] < 1
                    ):
                        raise WorldLootDependencyError("Cached World Loot location compatibility record is invalid.")
                    incompatible_locations.add((row["package_path"].casefold(), row["export_index"]))
                normalized_index_maps = {
                    str(package).casefold(): value for package, value in index_maps.items()
                }
                for package, package_locations in by_package.items():
                    if package.casefold() in incompatible_maps:
                        continue
                    package_index = normalized_index_maps.get(package.casefold())
                    if not isinstance(package_index, dict):
                        raise WorldLootDependencyError(f"Cached World Loot dependency index omitted {package}.")
                    for location in package_locations:
                        if (package.casefold(), location.export_index) in incompatible_locations:
                            continue
                        descriptor_key = f"{location.package_path.casefold()}#{location.export_index}"
                        _validate_cached_location_descriptor(
                            location, location_descriptors.get(descriptor_key),
                        )
                        class_info = package_index.get(str(location.export_index))
                        if (
                            not isinstance(class_info, dict)
                            or class_info.get("export_name") != location.export_name
                            or not isinstance(class_info.get("class_name"), str)
                        ):
                            raise WorldLootDependencyError(
                                f"Cached World Loot dependency index did not match {location.export_name}."
                            )
                        class_package = class_info.get("class_package")
                        if class_package is not None:
                            if not isinstance(class_package, str):
                                raise WorldLootDependencyError("Cached World Loot Blueprint package path is invalid.")
                            indexed_classes.add(_validate_blueprint_path(class_package))
                blueprint_paths = tuple(sorted(
                    {_validate_blueprint_path(package) for package in indexed_blueprints},
                    key=str.casefold,
                ))
                blueprints: tuple[MaterializedAsset, ...] = ()
                if blueprint_paths:
                    extracted_blueprints = asset_source.extract_assets(
                        game_root,
                        blueprint_paths,
                        game_version=game_version,
                        data_revision=data_revision,
                        progress=progress,
                    )
                    blueprints = materialize_extracted_assets(
                        extracted_blueprints,
                        content_root,
                        profile_callback=profile_callback,
                        profile_kind="blueprint_dependencies",
                    )
                    if progress:
                        progress("staging-blueprint-dependencies", len(blueprints), len(blueprint_paths))
                return WorldLootDependencyStage(
                    content_root=content_root,
                    maps=maps,
                    blueprint_packages=blueprints,
                    class_packages=tuple(sorted((
                        {_validate_blueprint_path(package) for package in indexed_blueprints}
                        | indexed_classes
                    ), key=str.casefold)),
                )
            # A package context owns discovery, preparation, and the compact
            # metadata produced while its one source UAsset remains open.
            # Keep each catalog export distinct: identical Blueprint classes
            # can resolve to different imported packages in different maps.
            map_by_header = {asset.primary_file.resolve(): asset for asset in maps}
            map_by_virtual_fold = {asset.virtual_path.casefold(): asset for asset in maps}
            package_sizes_all = tuple(_package_size(asset.primary_file) for asset in maps)
            worker_limit = max(1, scheduler.recommended_worker_count([
                scheduler.estimate_job_bytes(f"world-map:{asset.virtual_path.casefold()}", size)
                for asset, size in zip(maps, package_sizes_all, strict=True)
            ]))
            self._ensure_helper_ready(scheduler, diagnostics_callback)
            worker_mappings = self._prepare_worker_mappings(root, mappings, worker_limit)
            export_rebindings: list[dict[str, Any]] = []
            profile_scan_reasons: dict[tuple[str, int], set[str]] = {}
            if compatibility_profile == "1.0.9":
                by_package, export_rebindings, profile_scan_reasons = self._resolve_profile_world_loot_exports(
                    root, mappings, maps, by_package,
                    memory_scheduler=scheduler,
                    worker_mappings=worker_mappings,
                    worker_limit=worker_limit,
                    diagnostics_callback=diagnostics_callback,
                    profile_callback=profile_callback,
                )
            package_contexts = {
                package.casefold(): PackagePreparationContext(
                    package_path=package,
                    source_asset=map_by_virtual_fold[package.casefold()],
                    locations=tuple(sorted(package_locations, key=lambda item: (item.export_index, item.export_name))),
                    required_name_values=candidate_names or (),
                    healing_locations=tuple(
                        location for location in package_locations
                        if location.destination_kind == "INSTANCE_ARTICLE_BULK_OVERRIDE"
                        and location.location_type in _HEALING_UPGRADE_LOCATION_TYPES
                    ),
                )
                for package, package_locations in sorted(by_package.items(), key=lambda item: item[0].casefold())
            }
            locations_by_export = {
                (package.casefold(), location.export_index): location
                for package, map_locations in by_package.items()
                for location in map_locations
            }
            direct_packages: set[str] = set()
            direct_package_references = 0
            indexed_maps: dict[str, dict[str, dict[str, Any]]] = {}
            incompatible_map_reasons: dict[str, set[str]] = {}
            incompatible_location_reasons: dict[tuple[str, int], set[str]] = profile_scan_reasons

            # Record map classes
            def record_map_classes(expected_map: MaterializedAsset, classes: object) -> None:
                nonlocal direct_package_references
                if not isinstance(classes, list):
                    raise WorldLootDependencyError(f"UAssetAPI omitted class results for {expected_map.virtual_path}.")
                context = package_contexts.get(expected_map.virtual_path.casefold())
                if context is None:
                    raise WorldLootDependencyError(f"UAssetAPI returned an unexpected map during class indexing: {expected_map.virtual_path}.")
                package_locations = context.locations
                package_index: dict[str, dict[str, Any]] = {}
                package_direct_packages: set[str] = set()
                package_direct_references = 0
                returned_indices: set[int] = set()
                for class_info in classes:
                    if not isinstance(class_info, dict):
                        raise WorldLootDependencyError("UAssetAPI returned an invalid World Loot class record.")
                    export_index = class_info.get("exportIndex")
                    if not isinstance(export_index, int) or isinstance(export_index, bool):
                        incompatible_map_reasons.setdefault(expected_map.virtual_path.casefold(), set()).add(
                            f"invalid export index {export_index!r} in class results"
                        )
                        continue
                    location = locations_by_export.get((expected_map.virtual_path.casefold(), export_index))
                    if location is None:
                        incompatible_map_reasons.setdefault(expected_map.virtual_path.casefold(), set()).add(
                            f"unexpected export #{export_index} ({class_info.get('exportName')!r})"
                        )
                        continue
                    if export_index in returned_indices:
                        incompatible_map_reasons.setdefault(expected_map.virtual_path.casefold(), set()).add(
                            f"duplicate class result for export #{export_index}"
                        )
                        continue
                    returned_indices.add(export_index)
                    actual_export_name = class_info.get("exportName")
                    if actual_export_name != location.export_name:
                        incompatible_location_reasons.setdefault(
                            (expected_map.virtual_path.casefold(), location.export_index), set(),
                        ).add(
                            f"expected {location.export_name!r}, found {actual_export_name!r}"
                        )
                        continue
                    class_name = class_info.get("className")
                    package_path = class_info.get("packagePath")
                    if class_name == "BlueprintGeneratedClass":
                        if not isinstance(package_path, str) or not package_path.startswith("/Game/"):
                            incompatible_location_reasons.setdefault(
                                (expected_map.virtual_path.casefold(), location.export_index), set(),
                            ).add("generated Blueprint class import did not resolve to a /Game package")
                            continue
                        try:
                            package_path = _validate_blueprint_path(package_path)
                        except ValueError as error:
                            incompatible_location_reasons.setdefault(
                                (expected_map.virtual_path.casefold(), location.export_index), set(),
                            ).add(f"generated Blueprint package path is invalid: {error}")
                            continue
                        package_direct_references += 1
                        package_direct_packages.add(package_path)
                    elif isinstance(package_path, str) and package_path.startswith("/Game/"):
                        incompatible_location_reasons.setdefault(
                            (expected_map.virtual_path.casefold(), location.export_index), set(),
                        ).add(
                            f"target class changed from generated Blueprint: {class_name} at {package_path}"
                        )
                        continue
                    elif not isinstance(class_name, str) or not isinstance(package_path, str):
                        incompatible_location_reasons.setdefault(
                            (expected_map.virtual_path.casefold(), location.export_index), set(),
                        ).add("UAssetAPI returned an incomplete World Loot class record")
                        continue
                    else:
                        package_path = None
                    package_index[str(location.export_index)] = {
                        "export_name": location.export_name,
                        "class_name": class_name,
                        "class_package": package_path,
                    }
                for location in package_locations:
                    if location.export_index not in returned_indices:
                        incompatible_location_reasons.setdefault(
                            (expected_map.virtual_path.casefold(), location.export_index), set(),
                        ).add(f"catalogued export {location.export_name!r} was not returned")
                if expected_map.virtual_path.casefold() in incompatible_map_reasons:
                    return
                indexed_maps[expected_map.virtual_path] = package_index
                context.class_records = classes
                direct_package_references += package_direct_references
                direct_packages.update(package_direct_packages)

            healing_location_groups = _healing_instance_locations_by_map(by_package)
            healing_package_keys = set(healing_location_groups)
            non_healing_by_package = {
                package: package_locations
                for package, package_locations in by_package.items()
                if package.casefold() not in healing_package_keys
            }
            combined_class_results: dict[str, list[dict[str, Any]]] = {}
            single_pass_started = time.perf_counter()
            namemap_started = time.perf_counter()
            binary_package_descriptors = (
                self._prepare_binary_map_descriptors(
                    root,
                    mappings,
                    maps,
                    non_healing_by_package,
                    candidate_names,
                    memory_scheduler=scheduler,
                    worker_mappings=worker_mappings,
                    prepared_cache_root=prepared_cache_root,
                    progress=progress,
                    diagnostics_callback=diagnostics_callback,
                    profile_callback=profile_callback,
                    class_results=combined_class_results,
                )
                if candidate_names is not None else {}
            )
            for package_key, descriptor in binary_package_descriptors.items():
                context = package_contexts.get(package_key)
                if context is not None:
                    context.binary_descriptor = descriptor

            # Healing CDO source packages are explicit catalog metadata, so
            # stage those once before the map helper. Parent closure is still
            # resolved together with the other discovered Blueprint classes.
            known: dict[str, MaterializedAsset] = {}
            healing_prepared_maps: dict[str, dict[str, Any]] = {}
            healing_prepared_clones: dict[str, dict[str, Any]] = {}
            healing_preparation_failures: dict[str, str] = {}
            if prepared_cache_root is not None and candidate_names is not None and healing_location_groups:
                healing_sources = sorted({
                    location.instance_override_source.cdo_package
                    for package_locations in healing_location_groups.values()
                    for location in package_locations
                    if location.instance_override_source is not None
                }, key=str.casefold)
                if healing_sources:
                    extracted_sources = asset_source.extract_assets(
                        game_root, healing_sources, game_version=game_version,
                        data_revision=data_revision, progress=progress,
                    )
                    for asset in materialize_extracted_assets(extracted_sources, content_root):
                        known[asset.virtual_path.casefold()] = asset
                (
                    healing_prepared_maps,
                    healing_prepared_clones,
                    healing_preparation_failures,
                    healing_classes,
                ) = self._prepare_healing_templates(
                    root,
                    mappings,
                    maps,
                    known,
                    by_package,
                    candidate_names,
                    prepared_cache_root=prepared_cache_root,
                    memory_scheduler=scheduler,
                    worker_mappings=worker_mappings,
                    progress=progress,
                    diagnostics_callback=diagnostics_callback,
                    profile_callback=profile_callback,
                )
                combined_class_results.update(healing_classes)
                for package_key, descriptor in healing_prepared_maps.items():
                    context = package_contexts.get(package_key)
                    if context is not None:
                        context.binary_descriptor = descriptor
            for package_key, classes in sorted(combined_class_results.items()):
                package_asset = map_by_virtual_fold.get(package_key)
                context = package_contexts.get(package_key)
                if package_asset is None or context is None:
                    raise WorldLootDependencyError(f"Combined map preparation returned an unexpected class map: {package_key}.")
                if len(classes) == len({location.export_index for location in context.locations}):
                    record_map_classes(package_asset, classes)

            inspection_by_package = {
                context.package_path: context.locations
                for context in package_contexts.values()
                if context.class_records is None
            }
            inspection_manifest = {
                "maps": [
                    {
                        "inputFile": str(package_contexts[package.casefold()].source_asset.primary_file.resolve()),
                        "exportIndices": sorted({location.export_index for location in locations_for_map}),
                    }
                    for package, locations_for_map in sorted(inspection_by_package.items(), key=lambda item: item[0].casefold())
                ]
            }
            inspection_requests = inspection_manifest["maps"]
            map_inspection_started = time.perf_counter()
            package_sizes = tuple(_package_size(Path(request["inputFile"])) for request in inspection_requests)
            inspection_batches = _size_bounded_batches(
                tuple(zip(inspection_requests, package_sizes, strict=True)),
                maximum_items=_MAP_INSPECTION_BATCH_SIZE,
            )
            if diagnostics_callback is not None:
                diagnostics_callback(
                    "inspecting-map-classes",
                    f"{len(inspection_requests)} maps in {len(inspection_batches)} helper batches; "
                    f"adaptive worker limit={worker_limit}",
                )

            # Inspect batch
            def inspect_batch(
                batch_index: int,
                attempt_path: str,
                batch: tuple[tuple[dict[str, Any], int], ...],
            ) -> list[dict[str, Any]]:
                requests = [request for request, _size in batch]
                map_assets = []
                for request in requests:
                    map_asset = map_by_header.get(Path(request["inputFile"]).resolve())
                    if map_asset is None:
                        package_name = Path(request["inputFile"]).stem
                        raise WorldLootDependencyError(
                            f"Map request is outside the dependency staging set: {package_name}."
                        )
                    map_assets.append(map_asset)
                batch_key = (
                    f"world-map-batch:{batch_index}:{attempt_path}:"
                    f"{map_assets[0].virtual_path.casefold()}"
                )
                largest_size = max(size for _request, size in batch)
                estimate = max(
                    scheduler.estimate_job_bytes(
                        f"world-map:{asset.virtual_path.casefold()}", size,
                    )
                    for asset, (_request, size) in zip(map_assets, batch, strict=True)
                )
                worker_mapping = _mapping_for_current_worker(worker_mappings)
                if diagnostics_callback is not None:
                    diagnostics_callback(
                        "inspecting-map-classes",
                        f"batch {batch_index}/{len(inspection_batches)} "
                        f"({completed_inspections}/{len(inspection_requests)} maps; {len(requests)} maps)",
                    )
                if profile_callback is not None:
                    profile_callback(
                        "helper_launch",
                        helper_operation="inspect-map-batch",
                        package_count=len(requests),
                        package_count_total=len(inspection_requests),
                        batch_index=batch_index,
                        batch_count=len(inspection_batches),
                        worker_limit=worker_limit,
                        maximum_package_bytes=largest_size,
                    )
                helper_started = time.perf_counter()
                with scheduler.job(batch_key, package_bytes=largest_size, estimated_bytes=estimate):
                    batch_result = self._run_helper_json(
                        root,
                        worker_mapping,
                        "inspect-map-batch",
                        {"maps": requests},
                        memory_scheduler=scheduler,
                        job_key=batch_key,
                        job_reserved=True,
                    )
                if profile_callback is not None:
                    profile_callback(
                        "helper_batch_wall_time",
                        helper_operation="inspect-map-batch",
                        package_count=len(requests),
                        batch_index=batch_index,
                        batch_count=len(inspection_batches),
                        elapsed_seconds=time.perf_counter() - helper_started,
                    )
                if not isinstance(batch_result, list) or len(batch_result) != len(batch) or any(
                    not isinstance(result, dict) for result in batch_result
                ):
                    raise WorldLootDependencyError(
                        f"UAssetAPI map inspection result count did not match batch {batch_index}."
                    )
                for map_asset, map_result in zip(map_assets, batch_result, strict=True):
                    memory_result = map_result.get("memory")
                    if isinstance(memory_result, dict):
                        map_job_key = f"world-map:{map_asset.virtual_path.casefold()}"
                        scheduler.record_peak(
                            map_job_key, _positive_int(memory_result.get("peakWorkingSetBytes")) or 0,
                        )
                        if helper_metrics_callback is not None:
                            map_locations = inspection_by_package.get(map_asset.virtual_path, ())
                            helper_metrics_callback(map_job_key, {
                                **memory_result,
                                "package": map_asset.virtual_path,
                                "operation": "inspect-map-batch",
                                "reason": "World Loot map class inspection",
                                "systems": sorted({
                                    location.location_type or location.actor_category
                                    for location in map_locations
                                }, key=str.casefold),
                                "loadMilliseconds": map_result.get("loadMilliseconds"),
                                "analysisMilliseconds": map_result.get("analysisMilliseconds"),
                            })
                return batch_result

            # Isolate incompatible source maps
            def inspect_with_isolation(
                batch_index: int,
                attempt_path: str,
                batch: tuple[tuple[dict[str, Any], int], ...],
            ) -> tuple[dict[str, Any], ...]:
                try:
                    return tuple(inspect_batch(batch_index, attempt_path, batch))
                except Exception as error:
                    if not is_game_data_layout_mismatch(error):
                        raise
                    if len(batch) == 1:
                        request = batch[0][0]
                        asset = map_by_header.get(Path(str(request.get("inputFile", ""))).resolve())
                        package = (
                            str(request.get("packagePath", ""))
                            if request.get("packagePath") is not None
                            else asset.virtual_path if asset is not None else ""
                        )
                        package_key = package.casefold()
                        if package_key not in package_contexts:
                            raise WorldLootDependencyError(
                                "UAssetAPI rejected a map outside the prepared World Loot source set."
                            ) from error
                        reason = (
                            "map export structure is incompatible with this game version: "
                            f"{type(error).__name__}: {error}"
                        )[:500]
                        incompatible_map_reasons.setdefault(package_key, set()).add(reason)
                        LOGGER.warning("Skipping incompatible World Loot map %s: %s", package, error)
                        return ()
                    middle = len(batch) // 2
                    return (
                        *inspect_with_isolation(batch_index, f"{attempt_path}.0", batch[:middle]),
                        *inspect_with_isolation(batch_index, f"{attempt_path}.1", batch[middle:]),
                    )

            completed_inspections = 0
            with ThreadPoolExecutor(max_workers=worker_limit, thread_name_prefix="cv2-map-inspect") as pool:
                pending = {
                    pool.submit(inspect_with_isolation, index, str(index), batch)
                    for index, batch in enumerate(inspection_batches, start=1)
                }
                while pending:
                    completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        map_results = future.result()
                        for map_result in map_results:
                            completed_inspections += 1
                            map_path = Path(str(map_result.get("inputFile", ""))).resolve()
                            expected_map = map_by_header.get(map_path)
                            if expected_map is None:
                                raise WorldLootDependencyError("UAssetAPI returned an unexpected map path.")
                            record_map_classes(expected_map, map_result.get("classes"))
                            if progress:
                                progress("inspecting-map-classes", completed_inspections, len(inspection_requests))
            all_indexed_maps: dict[str, dict[str, dict[str, Any]]] = {}
            for package, package_locations in by_package.items():
                if package.casefold() in incompatible_map_reasons:
                    continue
                inspected_map = indexed_maps.get(package)
                if inspected_map is None:
                    raise WorldLootDependencyError(f"UAssetAPI omitted a World Loot map during class indexing: {package}.")
                package_index: dict[str, dict[str, Any]] = {}
                for location in package_locations:
                    class_info = inspected_map.get(str(location.export_index))
                    if class_info is None:
                        continue
                    package_index[str(location.export_index)] = {
                        "export_name": location.export_name,
                        "class_name": class_info["class_name"],
                        "class_package": class_info["class_package"],
                    }
                if package_index:
                    all_indexed_maps[package] = package_index
            for package_key, reasons in sorted(incompatible_map_reasons.items()):
                LOGGER.warning(
                    "Skipping World Loot map %s because its export index data is inconsistent: %s",
                    package_contexts[package_key].package_path,
                    "; ".join(sorted(reasons, key=str.casefold)),
                )
            for (package_key, export_index), reasons in sorted(incompatible_location_reasons.items()):
                LOGGER.warning(
                    "Skipping World Loot export %s#%s because it does not match the catalog: %s",
                    package_contexts[package_key].package_path,
                    export_index,
                    "; ".join(sorted(reasons, key=str.casefold)),
                )
            indexed_maps_by_key = {
                package.casefold(): rows for package, rows in indexed_maps.items()
            }
            unavailable_blueprint_packages: set[str] = set()
            if package_inventory is not None:
                for package, package_locations in by_package.items():
                    rows = indexed_maps_by_key.get(package.casefold(), {})
                    for location in package_locations:
                        class_info = rows.get(str(location.export_index))
                        blueprint_package = (
                            class_info.get("class_package") if isinstance(class_info, dict) else None
                        )
                        if not isinstance(blueprint_package, str) or package_inventory.contains(blueprint_package):
                            continue
                        unavailable_blueprint_packages.add(blueprint_package.casefold())
                        incompatible_location_reasons.setdefault(
                            (package.casefold(), location.export_index), set(),
                        ).add(f"required Blueprint schema package is absent from this game version: {blueprint_package}")
                direct_packages = {
                    package for package in direct_packages
                    if package.casefold() not in unavailable_blueprint_packages
                }
            if profile_callback is not None:
                profile_callback(
                    "stage",
                    stage="single_pass_map_discovery_and_preparation",
                    elapsed_seconds=time.perf_counter() - single_pass_started,
                    unique_maps=len(package_contexts),
                    prepared_discovery_maps=len(combined_class_results),
                    inspection_only_maps=len(inspection_requests),
                    maps_with_descriptors=sum(context.binary_descriptor is not None for context in package_contexts.values()),
                    structural_preparation_reloads=0,
                )
            if profile_callback is not None:
                profile_callback(
                    "stage",
                    stage="world_map_structural_inspection",
                    elapsed_seconds=time.perf_counter() - map_inspection_started,
                    packages=len(inspection_requests),
                    target_exports=len(locations_by_export),
                )

            parent_resolution_started = time.perf_counter()
            pending = set(direct_packages) | {
                asset.virtual_path for asset in known.values()
            }
            processed_parent_packages: set[str] = set()
            parent_packages_by_child: dict[str, str] = {}
            parent_dependency_edges = 0
            discovered_dependency_nodes = {package.casefold() for package in direct_packages}
            memoized_parent_edges = direct_package_references - len(direct_packages)
            rounds = 0
            while pending:
                rounds += 1
                if rounds > 64 or len(known) + len(pending) > 4096:
                    raise WorldLootDependencyError("Blueprint hierarchy exceeded safety limits.")
                package_batch = sorted(
                    (package for package in pending if package.casefold() not in processed_parent_packages),
                    key=str.casefold,
                )
                if package_inventory is not None:
                    absent = [package for package in package_batch if not package_inventory.contains(package)]
                    for package in absent:
                        unavailable_blueprint_packages.add(package.casefold())
                        processed_parent_packages.add(package.casefold())
                    package_batch = [
                        package for package in package_batch
                        if package.casefold() not in unavailable_blueprint_packages
                    ]
                if not package_batch:
                    break
                missing = [package for package in package_batch if package.casefold() not in known]
                if missing:
                    extracted = asset_source.extract_assets(
                        game_root,
                        missing,
                        game_version=game_version,
                        data_revision=data_revision,
                        progress=progress,
                    )
                    materialized = materialize_extracted_assets(extracted, content_root)
                    for asset in materialized:
                        known[asset.virtual_path.casefold()] = asset
                current_assets = tuple(known[package.casefold()] for package in package_batch)
                if progress:
                    progress("resolving-blueprint-parents", len(known), 0)
                parent_results = self._run_blueprint_parents(
                    root,
                    mappings,
                    current_assets,
                    memory_scheduler=scheduler,
                    diagnostics_callback=diagnostics_callback,
                    helper_metrics_callback=helper_metrics_callback,
                    worker_mappings=worker_mappings,
                    profile_callback=profile_callback,
                )
                if not isinstance(parent_results, list) or len(parent_results) != len(current_assets):
                    raise WorldLootDependencyError("UAssetAPI Blueprint-parent result count did not match extracted classes.")
                for asset, parent_info in zip(current_assets, parent_results, strict=True):
                    processed_parent_packages.add(asset.virtual_path.casefold())
                    parent_package = parent_info.get("parentPackage")
                    if parent_package is None:
                        continue
                    if not isinstance(parent_package, str):
                        raise WorldLootDependencyError("Blueprint parent package has an invalid type.")
                    parent_package = _validate_blueprint_path(parent_package)
                    parent_dependency_edges += 1
                    parent_key = parent_package.casefold()
                    parent_packages_by_child[asset.virtual_path.casefold()] = parent_key
                    if parent_key in discovered_dependency_nodes:
                        memoized_parent_edges += 1
                    else:
                        discovered_dependency_nodes.add(parent_key)
                    if parent_package.casefold() not in processed_parent_packages:
                        pending.add(parent_package)
            changed = True
            while changed:
                changed = False
                for child_package, parent_package in parent_packages_by_child.items():
                    if parent_package in unavailable_blueprint_packages and child_package not in unavailable_blueprint_packages:
                        unavailable_blueprint_packages.add(child_package)
                        changed = True
            for package, package_locations in by_package.items():
                rows = indexed_maps_by_key.get(package.casefold(), {})
                for location in package_locations:
                    class_info = rows.get(str(location.export_index))
                    blueprint_package = (
                        class_info.get("class_package") if isinstance(class_info, dict) else None
                    )
                    if isinstance(blueprint_package, str) and blueprint_package.casefold() in unavailable_blueprint_packages:
                        incompatible_location_reasons.setdefault(
                            (package.casefold(), location.export_index), set(),
                        ).add("a required Blueprint schema package or parent is absent from this game version")
            if profile_callback is not None:
                profile_callback(
                    "blueprint_dependency_graph",
                    nodes=len(discovered_dependency_nodes),
                    direct_package_references=direct_package_references,
                    direct_unique_packages=len(direct_packages),
                    parent_edges=parent_dependency_edges,
                    unique_parent_targets=len(discovered_dependency_nodes),
                    closure_computations=len(processed_parent_packages),
                    memoized_duplicate_edges=memoized_parent_edges,
                    closure_rounds=rounds,
                )
                profile_callback(
                    "stage",
                    stage="blueprint_dependency_resolution",
                    elapsed_seconds=time.perf_counter() - parent_resolution_started,
                    packages=len(known),
                    recursive_rounds=rounds,
                )
            actor_schema_check_required = _actor_schema_check_required(
                game_version,
                display_version,
                compatibility_profile,
            )
            if actor_schema_check_required:
                schema_check_packages = {
                    package: locations
                    for package, locations in by_package.items()
                    if package.casefold() not in incompatible_map_reasons
                }
                schema_failures = self._validate_legacy_actor_schemas(
                    root,
                    mappings,
                    maps,
                    schema_check_packages,
                    memory_scheduler=scheduler,
                    worker_mappings=worker_mappings,
                    worker_limit=worker_limit,
                    already_incompatible=incompatible_location_reasons,
                    progress=progress,
                    diagnostics_callback=diagnostics_callback,
                )
                for key, reason in schema_failures.items():
                    incompatible_location_reasons.setdefault(key, set()).add(reason)
            return WorldLootDependencyStage(
                content_root=content_root,
                maps=maps,
                blueprint_packages=tuple(known[key] for key in sorted(known)),
                class_packages=tuple(sorted((asset.virtual_path for asset in known.values()), key=str.casefold)),
                dependency_index={
                    "schema_version": 1,
                    "game_version": game_version,
                    "catalog_revision": data_revision,
                    "maps": all_indexed_maps,
                    "incompatible_world_loot_maps": [
                        {
                            "package_path": package_contexts[package_key].package_path,
                            "reasons": sorted(reasons, key=str.casefold),
                        }
                        for package_key, reasons in sorted(incompatible_map_reasons.items())
                    ],
                    "incompatible_world_loot_locations": [
                        {
                            "package_path": package_contexts[package_key].package_path,
                            "export_index": export_index,
                            "reasons": sorted(reasons, key=str.casefold),
                        }
                        for (package_key, export_index), reasons in sorted(incompatible_location_reasons.items())
                    ],
                    "blueprint_cdo_defaults": {},
                    "blueprint_packages": sorted(
                        (asset.virtual_path for asset in known.values()), key=str.casefold,
                    ),
                    "compatibility_profile": compatibility_profile,
                    "actor_schema_compatibility_checked": actor_schema_check_required,
                    "world_loot_export_rebindings": export_rebindings,
                    "binary_patch_version": 3 if candidate_name_values is not None else None,
                    "world_loot_binary_patch_packages": binary_package_descriptors,
                    "healing_prepared_maps": healing_prepared_maps,
                    "healing_prepared_clones": healing_prepared_clones,
                    "healing_preparation_failures": healing_preparation_failures,
                },
            )
        except (AssetExtractionError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorldLootDependencyError(f"Could not stage world-loot Blueprint dependencies: {error}") from error


    # Verify older-build pickup schemas
    def _validate_legacy_actor_schemas(
        self,
        work_root: Path,
        mappings: Path,
        maps: tuple[MaterializedAsset, ...],
        by_package: dict[str, list[WorldLootLocation]],
        *,
        memory_scheduler: MemoryBudgetScheduler,
        worker_mappings: tuple[Path, ...],
        worker_limit: int,
        already_incompatible: dict[tuple[str, int], set[str]],
        progress: Callable[[str, int, int], None] | None,
        diagnostics_callback: Callable[[str, str], None] | None,
    ) -> dict[tuple[str, int], str]:
        """Preflight old-build actor parsing so incompatible pickups can be skipped."""
        map_by_virtual = {asset.virtual_path.casefold(): asset for asset in maps}
        map_by_header = {asset.primary_file.resolve(): asset for asset in maps}
        requests: list[dict[str, Any]] = []
        for package, locations in sorted(by_package.items(), key=lambda item: item[0].casefold()):
            asset = map_by_virtual.get(package.casefold())
            if asset is None:
                continue
            actor_validations: list[dict[str, Any]] = []
            for location in locations:
                if (
                    location.destination_kind not in {"ARTICLE_BULK", "RENEWABLE_INGREDIENT"}
                    or (package.casefold(), location.export_index) in already_incompatible
                ):
                    continue
                expected_rewards = []
                for reward in location.rewards:
                    identity = reward.soft_object_path_identity
                    expected_rewards.append({
                        "path": reward.article_path,
                        "count": reward.count,
                        "notification": reward.notification,
                        "softObjectPathIdentity": (
                            {
                                "packageName": {
                                    "value": identity.package_name.value,
                                    "number": identity.package_name.number,
                                },
                                "assetName": {
                                    "value": identity.asset_name.value,
                                    "number": identity.asset_name.number,
                                },
                            }
                            if identity is not None else None
                        ),
                    })
                actor_validations.append({
                    "exportIndex": location.export_index,
                    "exportName": location.export_name,
                    "expectedRewards": expected_rewards,
                    "requireUniqueSaveId": location.destination_kind == "ARTICLE_BULK",
                    "expectedSaveId": location.save_id,
                })
            if actor_validations:
                requests.append({
                    "inputFile": str(asset.primary_file.resolve()),
                    "packagePath": asset.virtual_path,
                    "exportIndices": sorted({row["exportIndex"] for row in actor_validations}),
                    "actorValidations": actor_validations,
                })
        if not requests:
            return {}

        batches = _size_bounded_batches(requests, maximum_items=_MAP_INSPECTION_BATCH_SIZE)
        if diagnostics_callback is not None:
            diagnostics_callback(
                "checking-map-actor-schemas",
                f"Checking catalogued pickups in {len(requests)} maps for this older game build.",
            )
        failures: dict[tuple[str, int], str] = {}

        # Validate one helper batch
        def validate_batch(
            batch_number: int,
            attempt_path: str,
            batch: tuple[dict[str, Any], ...],
        ) -> list[dict[str, Any]]:
            package_sizes = tuple(_package_size(Path(request["inputFile"])) for request in batch)
            largest_size = max(package_sizes)
            estimated_memory = max(
                memory_scheduler.estimate_job_bytes(
                    f"world-map-schema:{Path(request['inputFile']).stem.casefold()}", size,
                )
                for request, size in zip(batch, package_sizes, strict=True)
            )
            job_key = f"world-map-schema-batch:{batch_number}:{attempt_path}"
            with memory_scheduler.job(
                job_key, package_bytes=largest_size, estimated_bytes=estimated_memory,
            ):
                result = self._run_helper_json(
                    work_root,
                    _mapping_for_current_worker(worker_mappings),
                    "inspect-map-batch",
                    {"maps": list(batch)},
                    memory_scheduler=memory_scheduler,
                    job_key=job_key,
                    job_reserved=True,
                    diagnostics_callback=diagnostics_callback,
                )
            if not isinstance(result, list) or len(result) != len(batch) or any(
                not isinstance(row, dict) for row in result
            ):
                raise WorldLootDependencyError("UAssetAPI actor-schema result count did not match its batch.")
            return result

        # Isolate incompatible actor-schema maps
        def validate_with_isolation(
            batch_number: int,
            attempt_path: str,
            batch: tuple[dict[str, Any], ...],
        ) -> tuple[tuple[dict[str, Any], dict[str, Any] | None, Exception | None], ...]:
            try:
                results = validate_batch(batch_number, attempt_path, batch)
                return tuple((request, result, None) for request, result in zip(batch, results, strict=True))
            except Exception as error:
                if not is_game_data_layout_mismatch(error):
                    raise
                if len(batch) == 1:
                    return ((batch[0], None, error),)
                middle = len(batch) // 2
                return (
                    *validate_with_isolation(batch_number, f"{attempt_path}.0", batch[:middle]),
                    *validate_with_isolation(batch_number, f"{attempt_path}.1", batch[middle:]),
                )

        completed_maps = 0
        future_batches: dict[
            Future[tuple[tuple[dict[str, Any], dict[str, Any] | None, Exception | None], ...]],
            tuple[dict[str, Any], ...],
        ] = {}
        with ThreadPoolExecutor(
            max_workers=min(worker_limit, len(batches)), thread_name_prefix="cv2-map-schema",
        ) as pool:
            pending = {
                pool.submit(validate_with_isolation, index, str(index), batch): batch
                for index, batch in enumerate(batches, start=1)
            }
            future_batches.update(pending)
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    batch = future_batches.pop(future)
                    results = future.result()
                    for request, result, error in results:
                        asset = map_by_header.get(Path(request["inputFile"]).resolve())
                        if error is not None:
                            reason = (
                                "actor schema is incompatible with this game version: "
                                f"{type(error).__name__}: {error}"
                            )[:500]
                            if asset is not None:
                                for actor in request["actorValidations"]:
                                    failures[(asset.virtual_path.casefold(), actor["exportIndex"])] = reason
                            completed_maps += 1
                            if progress:
                                progress("checking-map-actor-schemas", completed_maps, len(requests))
                            continue
                        assert result is not None
                        if asset is None or result.get("inputFile") != request["inputFile"]:
                            reason = "UAssetAPI returned an unexpected map during actor-schema validation."
                            for actor in request["actorValidations"]:
                                failures[(str(request["packagePath"]).casefold(), actor["exportIndex"])] = reason
                            continue
                        raw_validations = result.get("validations")
                        validations = {
                            row.get("exportIndex"): row
                            for row in raw_validations if isinstance(row, dict)
                        } if isinstance(raw_validations, list) else {}
                        map_error = result.get("inspectionError")
                        for actor in request["actorValidations"]:
                            validation = validations.get(actor["exportIndex"])
                            if (
                                not isinstance(validation, dict)
                                or validation.get("exportName") != actor["exportName"]
                                or validation.get("compatible") is not True
                            ):
                                detail = (
                                    validation.get("reason") if isinstance(validation, dict) else None
                                ) or map_error or "UAssetAPI did not validate this actor export."
                                failures[(asset.virtual_path.casefold(), actor["exportIndex"])] = (
                                    f"actor schema compatibility check failed: {str(detail)[:500]}"
                                )
                        completed_maps += 1
                        if progress:
                            progress("checking-map-actor-schemas", completed_maps, len(requests))
        LOGGER.info(
            "Older-build World Loot actor schema check completed maps=%d incompatible_locations=%d",
            len(requests), len(failures),
        )
        return failures

    # Resolve catalogued exports by exact object name
    def _resolve_profile_world_loot_exports(
        self,
        work_root: Path,
        mappings: Path,
        maps: tuple[MaterializedAsset, ...],
        by_package: dict[str, list[WorldLootLocation]],
        *,
        memory_scheduler: MemoryBudgetScheduler,
        worker_mappings: tuple[Path, ...],
        worker_limit: int,
        diagnostics_callback: Callable[[str, str], None] | None,
        profile_callback: Callable[..., None] | None,
    ) -> tuple[
        dict[str, list[WorldLootLocation]],
        list[dict[str, Any]],
        dict[tuple[str, int], set[str]],
    ]:
        """Find exact catalogued actors at their 1.0.9 export positions."""
        map_by_virtual = {asset.virtual_path.casefold(): asset for asset in maps}
        requests: list[dict[str, Any]] = []
        for package, locations in sorted(by_package.items(), key=lambda item: item[0].casefold()):
            asset = map_by_virtual.get(package.casefold())
            if asset is None:
                continue
            names = sorted({location.export_name for location in locations}, key=str.casefold)
            if names:
                requests.append({
                    "inputFile": str(asset.primary_file.resolve()),
                    "exportNames": names,
                })
        if not requests:
            return by_package, [], {}

        request_sizes = tuple(_package_size(Path(request["inputFile"])) for request in requests)
        batches = _size_bounded_batches(
            tuple(zip(requests, request_sizes, strict=True)),
            maximum_items=_MAP_INSPECTION_BATCH_SIZE,
        )
        if diagnostics_callback is not None:
            diagnostics_callback(
                "matching-map-exports",
                f"Scanning exact object names in {len(requests)} maps for the 1.0.9 profile.",
            )
        results_by_file: dict[Path, dict[str, Any]] = {}
        completed_maps = 0
        completed_lock = threading.Lock()

        # Search a helper batch
        def search_batch(
            batch_number: int,
            batch: tuple[tuple[dict[str, Any], int], ...],
        ) -> list[dict[str, Any]]:
            nonlocal completed_maps
            manifest_maps = [request for request, _size in batch]
            largest_size = max(size for _request, size in batch)
            estimate = max(
                memory_scheduler.estimate_job_bytes(
                    f"world-map-search:{Path(request['inputFile']).stem.casefold()}", size,
                )
                for request, size in batch
            )
            mapping = _mapping_for_current_worker(worker_mappings)
            if profile_callback is not None:
                profile_callback(
                    "helper_launch",
                    helper_operation="find-map-exports",
                    package_count=len(batch),
                    package_count_total=len(requests),
                    batch_index=batch_number,
                    batch_count=len(batches),
                    worker_limit=worker_limit,
                    maximum_package_bytes=largest_size,
                )
            started = time.perf_counter()
            try:
                with memory_scheduler.job(
                    f"world-map-search-batch:{batch_number}",
                    package_bytes=largest_size,
                    estimated_bytes=estimate,
                ):
                    found = self._run_helper_json(
                        work_root,
                        mapping,
                        "find-map-exports",
                        {"maps": manifest_maps},
                        memory_scheduler=memory_scheduler,
                        job_key=f"world-map-search-batch:{batch_number}",
                        job_reserved=True,
                    )
            finally:
                if profile_callback is not None:
                    profile_callback(
                        "helper_batch_wall_time",
                        helper_operation="find-map-exports",
                        package_count=len(batch),
                        batch_index=batch_number,
                        elapsed_seconds=time.perf_counter() - started,
                    )
            if not isinstance(found, list) or len(found) != len(batch) or any(
                not isinstance(result, dict) for result in found
            ):
                raise WorldLootDependencyError(
                    f"UAssetAPI exact-name search returned an invalid result batch {batch_number}."
                )
            with completed_lock:
                completed_maps += len(batch)
                completed = completed_maps
            if diagnostics_callback is not None:
                diagnostics_callback(
                    "matching-map-exports", f"Scanned {completed}/{len(requests)} maps.",
                )
            return found

        # Isolate malformed maps from healthy maps
        def search_with_isolation(
            batch_number: int,
            batch: tuple[tuple[dict[str, Any], int], ...],
        ) -> tuple[dict[str, Any], ...]:
            return _run_batch_with_failure_isolation(
                batch,
                lambda values: search_batch(batch_number, values),
                lambda _item, error: LOGGER.warning(
                    "Could not scan a map for 1.0.9 World Loot export names: %s", error,
                ),
                lambda count: None,
            )

        with ThreadPoolExecutor(max_workers=worker_limit, thread_name_prefix="cv2-map-name-search") as pool:
            futures = [
                pool.submit(search_with_isolation, batch_number, batch)
                for batch_number, batch in enumerate(batches, start=1)
            ]
            for future in futures:
                for result in future.result():
                    input_path = Path(str(result.get("inputFile", ""))).resolve()
                    if input_path in results_by_file:
                        raise WorldLootDependencyError("UAssetAPI exact-name search repeated a map.")
                    results_by_file[input_path] = result

        locations_by_map: dict[str, list[WorldLootLocation]] = {
            package.casefold(): list(locations) for package, locations in by_package.items()
        }
        package_by_input = {
            asset.primary_file.resolve(): package.casefold()
            for package, locations in by_package.items()
            if (asset := map_by_virtual.get(package.casefold())) is not None
            and any(location.export_name for location in locations)
        }
        candidate_indices: dict[tuple[str, int], int] = {}
        candidate_details: dict[tuple[str, int], dict[str, Any]] = {}
        scan_reasons: dict[tuple[str, int], set[str]] = {}

        # Record a reason for a rejected profile candidate
        def reject(location: WorldLootLocation, reason: str) -> None:
            scan_reasons.setdefault(
                (location.package_path.casefold(), location.export_index), set(),
            ).add(reason)

        for input_path, package_key in package_by_input.items():
            map_result = results_by_file.get(input_path)
            if map_result is None:
                continue
            rows = locations_by_map[package_key]
            package_path = rows[0].package_path
            matches = map_result.get("matches")
            if not isinstance(matches, list):
                continue
            by_name: dict[str, list[dict[str, Any]]] = {}
            for match in matches:
                if not isinstance(match, dict):
                    continue
                name = match.get("exportName")
                if isinstance(name, str):
                    by_name.setdefault(name.casefold(), []).append(match)
            for location in rows:
                if not _can_rebind_profile_scripted_location(location):
                    continue
                found = by_name.get(location.export_name.casefold(), [])
                if len(found) != 1 or found[0].get("exportName") != location.export_name:
                    if len(found) > 1:
                        reject(location, "exact object name occurs more than once in the 1.0.9 map")
                    elif found:
                        reject(location, "exact object name has a case-only mismatch in the 1.0.9 map")
                    continue
                match = found[0]
                target_index = match.get("exportIndex")
                class_name = match.get("className")
                class_package = match.get("packagePath")
                name_prefix = location.export_name.split("_UAID_", 1)[0]
                if (
                    not isinstance(target_index, int) or isinstance(target_index, bool) or target_index < 1
                    or class_name != "BlueprintGeneratedClass"
                    or not isinstance(class_package, str) or not class_package.startswith("/Game/")
                    or name_prefix.casefold() != location.actor_class.casefold()
                ):
                    reject(location, "exact export did not retain its catalogued actor Blueprint class")
                    continue
                expected_class_package = location.structural_class_package
                if expected_class_package is not None and class_package.casefold() != expected_class_package.casefold():
                    reject(location, "exact export references a different structural pickup class")
                    continue
                if not _can_rebind_profile_scripted_location(location, target_index):
                    reject(location, "scripted reward failed its resolved-index validation")
                    continue
                key = (location.package_path.casefold(), location.export_index)
                candidate_indices[key] = target_index
                candidate_details[key] = {
                    "package_path": package_path,
                    "catalog_export_index": location.export_index,
                    "resolved_export_index": target_index,
                    "export_name": location.export_name,
                    "class_package": class_package,
                }

        # Remove candidates that collide with another catalog slot
        active_candidates = set(candidate_indices)
        original_locations = {
            (location.package_path.casefold(), location.export_index): location
            for locations in locations_by_map.values()
            for location in locations
        }
        while True:
            final_indices = {
                key: candidate_indices[key] if key in active_candidates else key[1]
                for key in original_locations
            }
            keys_by_target: dict[tuple[str, int], list[tuple[str, int]]] = {}
            for key, target_index in final_indices.items():
                keys_by_target.setdefault((key[0], target_index), []).append(key)
            conflicts = {
                key
                for source_keys in keys_by_target.values() if len(source_keys) > 1
                for key in source_keys if key in active_candidates
            }
            if not conflicts:
                break
            for key in conflicts:
                reject(original_locations[key], "resolved export index collides with another catalogued map slot")
                active_candidates.remove(key)

        export_rebindings = [
            candidate_details[key]
            for key in sorted(active_candidates)
            if candidate_indices[key] != key[1]
        ]
        index_by_source = {key: candidate_indices[key] for key in active_candidates}
        remapped: dict[str, list[WorldLootLocation]] = {}
        for package, source_rows in by_package.items():
            package_key = package.casefold()
            rows = locations_by_map[package_key]
            updated_rows: list[WorldLootLocation] = []
            for location in rows:
                key = (location.package_path.casefold(), location.export_index)
                target_index = index_by_source.get(key, location.export_index)
                mirror_index = location.mirror_of_export_index
                if mirror_index is not None and location.mirror_of_package_path is not None:
                    mirror_index = index_by_source.get(
                        (location.mirror_of_package_path.casefold(), mirror_index), mirror_index,
                    )
                destination_id = location.destination_id
                if destination_id is not None and destination_id.casefold() == (
                    f"{location.package_path}#{location.export_index}".casefold()
                ):
                    destination_id = f"{location.package_path}#{target_index}"
                updated_rows.append(replace(
                    location,
                    export_index=target_index,
                    destination_id=destination_id,
                    mirror_of_export_index=mirror_index,
                ))
            remapped[package] = updated_rows

        if export_rebindings:
            LOGGER.info(
                "1.0.9 World Loot profile rebound %s map exports by unique exact object name; %s candidates were rejected.",
                len(export_rebindings), len(scan_reasons),
            )
        return remapped, export_rebindings, scan_reasons

    # Prepare binary map descriptors
    def _prepare_binary_map_descriptors(
        self,
        work_root: Path,
        mappings: Path,
        maps: tuple[MaterializedAsset, ...],
        by_package: dict[str, list[WorldLootLocation]],
        candidate_name_values: tuple[str, ...],
        *,
        memory_scheduler: MemoryBudgetScheduler,
        worker_mappings: tuple[Path, ...],
        prepared_cache_root: Path | None,
        progress: Callable[[str, int, int], None] | None,
        diagnostics_callback: Callable[[str, str], None] | None,
        profile_callback: Callable[..., None] | None,
        class_results: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Resolve byte offsets and NameMap indices while the extracted maps are staged."""
        map_by_virtual = {asset.virtual_path.casefold(): asset for asset in maps}
        requests: list[dict[str, Any]] = []
        for package, locations in sorted(by_package.items(), key=lambda item: item[0].casefold()):
            rows = [
                location for location in locations
                if location.destination_kind == "ARTICLE_BULK"
            ]
            if not rows:
                continue
            asset = map_by_virtual.get(package.casefold())
            if asset is None:
                raise WorldLootDependencyError(f"Extract omitted a binary descriptor map: {package}.")
            rewards = []
            for location in rows:
                for reward_index, reward in enumerate(location.rewards):
                    identity = reward.soft_object_path_identity
                    expected_identity = None if identity is None else {
                        "packageName": {"value": identity.package_name.value, "number": identity.package_name.number},
                        "assetName": {"value": identity.asset_name.value, "number": identity.asset_name.number},
                    }
                    rewards.append({
                        "exportIndex": location.export_index,
                        "rewardIndex": reward_index,
                        "exportName": location.export_name,
                        "expected": {
                            "path": reward.article_path,
                            "count": reward.count,
                            "notification": reward.notification,
                            "softObjectPathIdentity": expected_identity,
                        },
                    })
            requests.append({
                "inputFile": str(asset.primary_file.resolve()),
                "packagePath": asset.virtual_path,
                "rewards": rewards,
                "classExportIndices": sorted({location.export_index for location in locations}),
            })
        if not requests:
            return {}

        if prepared_cache_root is not None:
            return self._prepare_world_loot_name_maps(
                work_root, mappings, requests, candidate_name_values,
                prepared_cache_root=prepared_cache_root,
                memory_scheduler=memory_scheduler,
                worker_mappings=worker_mappings,
                progress=progress,
                diagnostics_callback=diagnostics_callback,
                profile_callback=profile_callback,
                class_results=class_results,
            )

        results: dict[str, dict[str, Any]] = {}
        batch_size = 24
        for offset in range(0, len(requests), batch_size):
            batch = requests[offset:offset + batch_size]
            batch_number = offset // batch_size + 1
            if diagnostics_callback is not None:
                diagnostics_callback(
                    "preparing-binary-world-loot",
                    f"batch {batch_number}/{(len(requests) + batch_size - 1) // batch_size}",
                )
            mapping = _mapping_for_current_worker(worker_mappings)
            if profile_callback is not None:
                profile_callback(
                    "helper_launch",
                    helper_operation="prepare-map-binary-descriptors",
                    package_count=len(batch),
                    package_count_total=len(requests),
                    batch_index=batch_number,
                    batch_count=(len(requests) + batch_size - 1) // batch_size,
                    worker_limit=1,
                )
            helper_started = time.perf_counter()
            try:
                batch_result = self._run_helper_json(
                    work_root,
                    mapping,
                    "prepare-map-binary-descriptors",
                    {"maps": batch, "candidateNameValues": candidate_name_values},
                    memory_scheduler=memory_scheduler,
                    job_key=f"prepare-world-loot-binary-descriptors-{batch_number}",
                    diagnostics_callback=diagnostics_callback,
                )
            finally:
                if profile_callback is not None:
                    profile_callback(
                        "helper_batch_wall_time",
                        helper_operation="prepare-map-binary-descriptors",
                        package_count=len(batch),
                        batch_index=batch_number,
                        elapsed_seconds=time.perf_counter() - helper_started,
                    )
            if not isinstance(batch_result, list) or len(batch_result) != len(batch):
                raise WorldLootDependencyError("UAssetAPI binary descriptor result count did not match its map batch.")
            expected_packages = {request["packagePath"].casefold() for request in batch}
            observed_packages: set[str] = set()
            for result in batch_result:
                if not isinstance(result, dict):
                    raise WorldLootDependencyError("UAssetAPI returned an invalid binary map descriptor.")
                package_path = result.get("packagePath")
                if not isinstance(package_path, str) or package_path.casefold() not in expected_packages:
                    raise WorldLootDependencyError("UAssetAPI binary descriptor returned an unexpected package.")
                if package_path.casefold() in observed_packages:
                    raise WorldLootDependencyError("UAssetAPI binary descriptor repeated a package.")
                observed_packages.add(package_path.casefold())
                source_asset = map_by_virtual.get(package_path.casefold())
                if source_asset is None:
                    raise WorldLootDependencyError("Binary descriptor package is absent from Extract staging.")
                package_file_hashes = {}
                if isinstance(result.get("headerSha256"), str):
                    package_file_hashes[source_asset.primary_file.suffix.casefold()] = result["headerSha256"]
                if isinstance(result.get("uexpSha256"), str):
                    package_file_hashes[".uexp"] = result["uexpSha256"]
                for source_file in source_asset.files:
                    suffix = source_file.suffix.casefold()
                    if suffix not in package_file_hashes:
                        package_file_hashes[suffix] = _sha256_path(source_file)
                fields = result.get("fields")
                name_map_indices = result.get("nameMapIndices")
                if not isinstance(fields, list) or not isinstance(name_map_indices, dict):
                    raise WorldLootDependencyError(f"UAssetAPI omitted binary fields or NameMap indices for {package_path}.")
                classes = result.get("classes")
                if class_results is not None and isinstance(classes, list) and len(classes) == len(request["classExportIndices"]):
                    class_results[package_path.casefold()] = classes
                results[package_path.casefold()] = {
                    "version": 3,
                    "package_path": package_path,
                    "header_sha256": result.get("headerSha256"),
                    "uexp_sha256": result.get("uexpSha256"),
                    "header_size": result.get("headerSize"),
                    "uexp_size": result.get("uexpSize"),
                    "package_file_hashes": package_file_hashes,
                    "name_map_indices": name_map_indices,
                    "name_map_count_before": None,
                    "name_map_count_after": None,
                    "names_added_count": None,
                    "fields": [
                        {
                            "export_index": field.get("exportIndex"),
                            "reward_index": field.get("rewardIndex"),
                            "export_name": field.get("exportName"),
                            "offset": field.get("offset"),
                            "expected_bytes_hex": field.get("expectedBytesHex"),
                            "expected_identity": _binary_identity_to_json(field.get("expectedIdentity")),
                            "available": field.get("available"),
                            "unavailable_reason": field.get("unavailableReason"),
                        }
                        for field in fields if isinstance(field, dict)
                    ],
                    "prepared_asset": None,
                    "preparation_reason": "Extract-time NameMap preparation was not requested.",
                    "unavailable_reason": result.get("unavailableReason"),
                }
            if observed_packages != expected_packages:
                raise WorldLootDependencyError("UAssetAPI omitted one or more packages from its binary descriptor batch.")
            if progress:
                progress("preparing-binary-world-loot", min(offset + len(batch), len(requests)), len(requests))
        return results

    # Prepare healing templates
    def _prepare_healing_templates(
        self,
        work_root: Path,
        mappings: Path,
        maps: tuple[MaterializedAsset, ...],
        known_blueprints: dict[str, MaterializedAsset],
        by_package: dict[str, list[WorldLootLocation]],
        candidate_name_values: tuple[str, ...],
        *,
        prepared_cache_root: Path,
        memory_scheduler: MemoryBudgetScheduler,
        worker_mappings: tuple[Path, ...],
        progress: Callable[[str, int, int], None] | None,
        diagnostics_callback: Callable[[str, str], None] | None,
        profile_callback: Callable[..., None] | None,
    ) -> tuple[
        dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str],
        dict[str, list[dict[str, Any]]],
    ]:
        """Prebuild and reopen-check per-location Healing map and Blueprint templates."""
        del memory_scheduler, worker_mappings, diagnostics_callback
        locations_by_map = _healing_instance_locations_by_map(by_package)
        if not locations_by_map:
            return {}, {}, {}, {}
        maps_by_virtual = {asset.virtual_path.casefold(): asset for asset in maps}
        output_root = work_root / ".cv2-healing-template-output"
        healing_cache_root = prepared_cache_root.parent / "healing-prepared-assets"
        package_requests: list[HealingUpgradePackagePatch] = []
        map_inputs: dict[str, Path] = {}

        for map_number, (package_key, locations) in enumerate(sorted(locations_by_map.items()), start=1):
            package_path = locations[0].package_path
            if any(location.package_path.casefold() != package_key for location in locations):
                raise WorldLootDependencyError("Healing locations were grouped under inconsistent map package paths.")
            raw_map = maps_by_virtual.get(package_key)
            if raw_map is None:
                raise WorldLootDependencyError(f"Extract omitted Healing destination map {package_path}.")
            input_header = raw_map.primary_file.resolve(strict=True)
            output_header = input_header.with_name(input_header.stem + ".cv2-prepared.umap")
            if any(path.exists() for path in _package_sidecars(output_header)):
                raise WorldLootDependencyError(f"Healing template output already exists: {output_header}.")
            map_inputs[package_path.casefold()] = input_header
            patches: list[HealingUpgradePatch] = []
            for location in sorted(locations, key=lambda item: item.export_index):
                source = location.instance_override_source
                if source is None or len(location.rewards) != 1:
                    raise WorldLootDependencyError(f"Healing location {location.export_name} has no validated single CDO Article reward.")
                if source.class_package.casefold() != source.cdo_package.casefold():
                    raise WorldLootDependencyError("Healing override source CDO and generated class must share their package.")
                blueprint_asset = known_blueprints.get(source.cdo_package.casefold())
                if blueprint_asset is None:
                    raise WorldLootDependencyError(f"Extract omitted Healing Blueprint dependency {source.cdo_package}.")
                clone_package_path = (
                    f"/Game/CV2Randomizer/Healing/{PurePosixPath(package_path).name}_E{location.export_index}/"
                    f"{PurePosixPath(source.class_package).name}"
                )
                clone_relative = PurePosixPath(clone_package_path.removeprefix("/Game/"))
                clone_output = output_root.joinpath(*clone_relative.parts).with_suffix(".uasset")
                reward = location.rewards[0]
                vanilla_reward = ArticleReward(
                    reward.article_path, reward.count, reward.notification, reward.soft_object_path_identity,
                )
                patches.append(HealingUpgradePatch(
                    export_index=location.export_index,
                    export_name=location.export_name,
                    class_package=source.class_package,
                    class_name=PurePosixPath(source.class_package).name + "_C",
                    cdo_export_index=source.cdo_export_index,
                    cdo_export_name=source.cdo_export_name,
                    blueprint_input=blueprint_asset.primary_file,
                    blueprint_output=clone_output,
                    clone_package_path=clone_package_path,
                    expected_original=vanilla_reward,
                    assigned=vanilla_reward,
                    name_map_values=candidate_name_values,
                ))
            package_requests.append(HealingUpgradePackagePatch(
                input_header,
                output_header,
                tuple(patches),
                package_path=package_path,
                map_name_map_values=(
                    tuple(sorted(set(candidate_name_values)))
                    if any(
                        location.destination_kind == "ARTICLE_BULK"
                        for location in by_package.get(package_path, ())
                    ) else ()
                ),
                discovery_exports=tuple(sorted({
                    (location.export_index, location.export_name)
                    for location in by_package.get(package_path, ())
                })),
                world_loot_fields=tuple(
                    (
                        location.export_index,
                        reward_index,
                        location.export_name,
                        ArticleReward(
                            reward.article_path, reward.count, reward.notification,
                            reward.soft_object_path_identity,
                        ),
                    )
                    for location in by_package.get(package_path, ())
                    if location.destination_kind == "ARTICLE_BULK"
                    for reward_index, reward in enumerate(location.rewards)
                ),
            ))
            if progress:
                progress("preparing-healing-templates", map_number, len(locations_by_map))

        serializer = WorldLootSerializer(helper_project=self.helper_project)
        failures: dict[str, str] = {}
        completed_maps = 0

        healing_batch_index = 0

        # Run healing batch
        def run_healing_batch(batch: tuple[HealingUpgradePackagePatch, ...]):
            nonlocal healing_batch_index
            healing_batch_index += 1
            if profile_callback is not None:
                profile_callback(
                    "helper_launch",
                    helper_operation="patch-healing-upgrades",
                    package_count=len(batch),
                    package_count_total=len(package_requests),
                    batch_index=healing_batch_index,
                    worker_limit=1,
                )
            helper_started = time.perf_counter()
            try:
                return serializer.patch_healing_upgrades(
                    work_root=work_root,
                    mappings_path=mappings,
                    packages=batch,
                )
            finally:
                if profile_callback is not None:
                    profile_callback(
                        "helper_batch_wall_time",
                        helper_operation="patch-healing-upgrades",
                        package_count=len(batch),
                        batch_index=healing_batch_index,
                        elapsed_seconds=time.perf_counter() - helper_started,
                    )

        # Record healing failure
        def record_healing_failure(package: HealingUpgradePackagePatch, error: Exception) -> None:
            package_path = package.package_path or package.input_asset.stem
            failures[package_path.casefold()] = str(error).strip()[-1200:]
            LOGGER.warning(
                "Healing Extract preparation fallback package=%s error=%s",
                package_path, str(error).strip()[-1200:],
            )

        # Record healing progress
        def record_healing_progress(count: int) -> None:
            nonlocal completed_maps
            completed_maps += count
            if progress:
                progress("preparing-healing-template-maps", completed_maps, len(package_requests))

        # Keep the fast single-process batch for the common case. If UAssetAPI
        # rejects a World Partition map, bisect failed batches and retry only
        # the affected half until unsafe maps alone are left for Generate's
        # structural fallback.
        results = _run_batch_with_failure_isolation(
            package_requests,
            run_healing_batch,
            record_healing_failure,
            record_healing_progress,
        )
        map_descriptors: dict[str, dict[str, Any]] = {}
        clone_descriptors: dict[str, dict[str, Any]] = {}
        map_class_results: dict[str, list[dict[str, Any]]] = {}
        for result in results:
            package_key = next(key for key, value in map_inputs.items() if value.resolve() == result.input_path.resolve())
            map_descriptor = dict(result.prepared_descriptor or {})
            if not map_descriptor:
                raise WorldLootDependencyError(f"Healing helper omitted a prepared map descriptor for {package_key}.")
            package_path = map_descriptor.get("package_path")
            if not isinstance(package_path, str):
                raise WorldLootDependencyError(f"Healing helper returned an invalid map path for {package_key}.")
            prepared_map = self._store_prepared_world_package(
                work_root,
                healing_cache_root,
                {"packagePath": package_path},
                {"outputFiles": [str(path) for path in _package_sidecars(result.output_path)]},
                root_name="healing-prepared-assets",
            )
            map_descriptor.update({
                "fields": list(result.world_loot_fields),
                "prepared_asset": prepared_map,
                "preparation_reason": None,
                "unavailable_reason": None,
            })
            map_descriptors[package_key] = map_descriptor
            map_class_results[package_key] = list(result.class_inspections)
            map_timings = result.timings if isinstance(result.timings, dict) else {}
            if profile_callback is not None:
                systems = ["Healing"]
                if any(
                    location.destination_kind == "ARTICLE_BULK"
                    for location in by_package.get(package_key, ())
                ):
                    systems.append("World Loot")
                profile_callback(
                    "package_preparation",
                    package=map_descriptor.get("package_path", package_key),
                    package_operation="patch-healing-upgrades",
                    reason="Combined Healing template and World Loot NameMap preparation",
                    systems=systems,
                    load_ms=map_timings.get("loadMilliseconds"),
                    analysis_ms=map_timings.get("discoveryValidationMilliseconds"),
                    namemap_ms=map_timings.get("mapMutationMilliseconds"),
                    serialize_ms=map_timings.get("mapSerializationMilliseconds"),
                    reopen_validate_ms=map_timings.get("mapReopenValidationMilliseconds"),
                    serialization_reopen_ms=map_timings.get("serializationMetadataReopenMilliseconds"),
                    total_ms=map_timings.get("totalMilliseconds"),
                    discovery_open_count=result.discovery_open_count,
                    preparation_reload_count=result.preparation_reload_count,
                    serialization_reopen_count=result.serialization_reopen_count,
                    validation_reopen_count=result.validation_reopen_count,
                    status="prepared",
                    name_map_count_before=map_descriptor.get("name_map_count_before"),
                    name_map_count_after=map_descriptor.get("name_map_count_after"),
                )
            for clone in result.clones:
                if not isinstance(clone.prepared_descriptor, dict):
                    raise WorldLootDependencyError(f"Healing helper omitted the clone descriptor for {clone.clone_package_path}.")
                clone_key = clone.clone_package_path.casefold()
                prepared_clone = self._store_prepared_world_package(
                    work_root,
                    healing_cache_root,
                    {"packagePath": clone.clone_package_path},
                    {"outputFiles": [str(path) for path in _package_sidecars(clone.output_path)]},
                    root_name="healing-prepared-assets",
                )
                clone_descriptor = dict(clone.prepared_descriptor)
                clone_descriptor["prepared_asset"] = prepared_clone
                clone_descriptors[clone_key] = clone_descriptor
                if profile_callback is not None:
                    clone_timings = clone.timings if isinstance(clone.timings, dict) else {}
                    profile_callback(
                        "package_preparation",
                        package=clone.clone_package_path,
                        package_operation="prepare-healing-blueprint-clone",
                        reason="Healing per-location Blueprint/CDO clone",
                        systems=["Healing"],
                        load_ms=clone_timings.get("loadMilliseconds"),
                        analysis_ms=clone_timings.get("discoveryValidationMilliseconds"),
                        serialize_ms=clone_timings.get("serializationMilliseconds"),
                        reopen_validate_ms=clone_timings.get("reopenValidationMilliseconds"),
                        total_ms=clone_timings.get("totalMilliseconds"),
                        status="prepared",
                    )
        return map_descriptors, clone_descriptors, failures, map_class_results

    # Prepare world loot name maps
    def _prepare_world_loot_name_maps(
        self,
        work_root: Path,
        mappings: Path,
        requests: list[dict[str, Any]],
        candidate_name_values: tuple[str, ...],
        *,
        prepared_cache_root: Path,
        memory_scheduler: MemoryBudgetScheduler,
        worker_mappings: tuple[Path, ...],
        progress: Callable[[str, int, int], None] | None,
        diagnostics_callback: Callable[[str, str], None] | None,
        profile_callback: Callable[..., None] | None,
        class_results: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Prepare legal World Loot Names during Extract and cache validated templates."""
        package_requests: list[dict[str, Any]] = []
        fallback: dict[str, tuple[dict[str, Any], str]] = {}
        output_root = work_root / ".cv2-prepared-world-loot"
        for request in requests:
            package_path = request["packagePath"]
            source = Path(request["inputFile"]).resolve(strict=True)
            rewards: list[dict[str, Any]] = []
            reason = None
            for field in request["rewards"]:
                expected = field["expected"]
                identity = expected.get("softObjectPathIdentity")
                if not isinstance(identity, dict):
                    reason = "The vanilla ArticleDataPath has no exact FName identity."
                    break
                rewards.append({
                    "exportIndex": field["exportIndex"],
                    "rewardIndex": field["rewardIndex"],
                    "exportName": field["exportName"],
                    "articlePath": expected["path"],
                    "count": expected["count"],
                    "notification": expected["notification"],
                    "identity": identity,
                })
            if reason is not None:
                fallback[package_path.casefold()] = (request, reason)
                continue
            relative = PurePosixPath(package_path.removeprefix("/Game/"))
            output_file = output_root.joinpath(*relative.parts).with_suffix(".umap")
            if not _is_within(output_file, work_root) or not is_world_map_package(package_path):
                fallback[package_path.casefold()] = (request, "Prepared map output path failed ownership validation.")
                continue
            package_requests.append({
                "packagePath": package_path,
                "inputFile": str(source),
                "outputFile": str(output_file),
                "nameValues": list(candidate_name_values),
                "rewards": rewards,
                "classExportIndices": list(request.get("classExportIndices", ())),
            })

        prepared_cache_root = prepared_cache_root.expanduser().resolve()
        prepared_cache_root.mkdir(parents=True, exist_ok=True)
        results: dict[str, dict[str, Any]] = {}
        batches = _size_bounded_batches(
            package_requests,
            maximum_items=_WORLD_LOOT_NAMEMAP_BATCH_SIZE,
        )
        if not package_requests:
            worker_limit = 0
        else:
            if not worker_mappings:
                raise WorldLootDependencyError("No isolated UAssetAPI worker mappings are available for NameMap preparation.")
            package_sizes = tuple(
                _package_size(Path(request["inputFile"]))
                for request in package_requests
            )
            package_estimates = tuple(
                memory_scheduler.estimate_job_bytes(
                    f"world-map:{request['packagePath'].casefold()}", size,
                )
                for request, size in zip(package_requests, package_sizes, strict=True)
            )
            worker_limit = max(1, memory_scheduler.recommended_worker_count(package_estimates))
            worker_mappings = worker_mappings[:worker_limit]
            worker_limit = min(worker_limit, len(worker_mappings))
            if worker_limit <= 0:
                raise WorldLootDependencyError("No isolated UAssetAPI worker mappings are available for NameMap preparation.")

        # Prepare batch
        def prepare_batch(
            batch_index: int,
            batch: tuple[dict[str, Any], ...],
        ) -> tuple[tuple[dict[str, Any], ...], list[dict[str, Any]], int, int]:
            batch_number = batch_index + 1
            batch_key = f"prepare-world-loot-namemaps-batch-{batch_number}"
            largest_size = max(_package_size(Path(request["inputFile"])) for request in batch)
            estimate = max(
                memory_scheduler.estimate_job_bytes(
                    f"world-map:{request['packagePath'].casefold()}",
                    _package_size(Path(request["inputFile"])),
                )
                for request in batch
            )
            if diagnostics_callback is not None:
                diagnostics_callback(
                    "preparing-world-loot-namemaps",
                    f"batch {batch_number}/{len(batches)}; {len(batch)} maps; {worker_limit} workers",
                )
            if profile_callback is not None:
                profile_callback(
                    "helper_launch",
                    helper_operation="prepare-world-loot-namemaps",
                    package_count=len(batch),
                    package_count_total=len(package_requests),
                    batch_index=batch_number,
                    batch_count=len(batches),
                    worker_limit=worker_limit,
                    maximum_package_bytes=largest_size,
                    estimated_memory_bytes=estimate,
                )
            helper_started = time.perf_counter()
            try:
                with memory_scheduler.job(
                    batch_key,
                    package_bytes=largest_size,
                    estimated_bytes=estimate,
                ):
                    batch_result = self._run_helper_json(
                        work_root,
                        _mapping_for_current_worker(worker_mappings),
                        "prepare-world-loot-namemaps",
                        {"packages": batch},
                        memory_scheduler=memory_scheduler,
                        job_key=batch_key,
                        job_reserved=True,
                        diagnostics_callback=diagnostics_callback,
                    )
            finally:
                if profile_callback is not None:
                    profile_callback(
                        "helper_batch_wall_time",
                        helper_operation="prepare-world-loot-namemaps",
                        package_count=len(batch),
                        batch_index=batch_number,
                        batch_count=len(batches),
                        elapsed_seconds=time.perf_counter() - helper_started,
                    )
            if not isinstance(batch_result, list) or len(batch_result) != len(batch):
                raise WorldLootDependencyError("Prepared World Loot NameMap result count did not match its package batch.")
            return batch, batch_result, largest_size, estimate

        # Collect batch
        def collect_batch(batch: tuple[dict[str, Any], ...], batch_result: list[dict[str, Any]]) -> None:
            if not isinstance(batch_result, list) or len(batch_result) != len(batch):
                raise WorldLootDependencyError("Prepared World Loot NameMap result count did not match its package batch.")
            requested_by_key = {item["packagePath"].casefold(): item for item in batch}
            for result in batch_result:
                if not isinstance(result, dict):
                    raise WorldLootDependencyError("UAssetAPI returned an invalid prepared World Loot package.")
                package_path = result.get("packagePath")
                key = package_path.casefold() if isinstance(package_path, str) else ""
                request = requested_by_key.get(key)
                if request is None:
                    raise WorldLootDependencyError("Prepared World Loot result returned an unrequested package.")
                classes = result.get("classes")
                if (
                    class_results is not None
                    and isinstance(classes, list)
                    and len(classes) == len(request.get("classExportIndices", ()))
                ):
                    class_results[key] = classes
                if profile_callback is not None:
                    profile_callback(
                        "package_preparation",
                        package=package_path,
                        package_operation="prepare-world-loot-namemaps",
                        reason="World Loot NameMap and binary descriptor preparation",
                        systems=["World Loot"],
                        total_ms=result.get("preparationMilliseconds"),
                        load_ms=result.get("loadMilliseconds"),
                        analysis_ms=result.get("analysisMilliseconds"),
                        namemap_ms=result.get("nameMapMilliseconds"),
                        serialize_ms=result.get("serializationMilliseconds"),
                        reopen_validate_ms=result.get("validationReopenMilliseconds"),
                        serialization_reopen_ms=result.get("serializationReopenMilliseconds"),
                        discovery_open_count=result.get("discoveryOpenCount"),
                        preparation_reload_count=result.get("preparationReloadCount"),
                        validation_reopen_count=result.get("validationReopenCount"),
                        serialization_reopen_count=result.get("serializationReopenCount"),
                        working_set_bytes=result.get("workingSetBytes"),
                        namemap_count_before=result.get("nameMapCountBefore"),
                        namemap_count_after=result.get("nameMapCountAfter"),
                        names_added=(
                            len(result["namesAdded"])
                            if isinstance(result.get("namesAdded"), list) else None
                        ),
                        status=result.get("status"),
                    )
                status = result.get("status")
                if status not in {"prepared_binary_candidate", "raw_binary_candidate"}:
                    fallback[key] = (request, str(result.get("fallbackReason") or "NameMap preparation failed validation."))
                    continue
                if result.get("reopenedAndValidated") is not True or result.get("rawUexpByteIdentical") is not True:
                    fallback[key] = (request, "Prepared package did not pass reopen/raw-payload validation.")
                    continue

                prepared_asset = None
                if status == "prepared_binary_candidate":
                    prepared_asset = self._store_prepared_world_package(
                        work_root, prepared_cache_root, request, result,
                    )
                    package_file_hashes = {
                        Path(row["name"]).suffix.casefold(): row["sha256"]
                        for row in prepared_asset["files"]
                    }
                else:
                    source_asset = Path(request["inputFile"]).resolve(strict=True)
                    package_file_hashes = {
                        file.suffix.casefold(): _sha256_path(file)
                        for file in _package_sidecars(source_asset)
                    }
                fields = result.get("fields")
                name_indices = result.get("nameMapIndices")
                if not isinstance(fields, list) or not isinstance(name_indices, dict):
                    raise WorldLootDependencyError(f"Prepared helper omitted fields or NameMap entries for {package_path}.")
                expected_field_count = len(request["rewards"])
                if len(fields) != expected_field_count:
                    raise WorldLootDependencyError(f"Prepared helper omitted ArticleDataPath fields for {package_path}.")
                results[key] = {
                    "version": 3,
                    "package_path": package_path,
                    "header_sha256": result.get("preparedUassetSha256"),
                    "uexp_sha256": result.get("preparedUexpSha256"),
                    "header_size": result.get("uassetSizeAfter"),
                    "uexp_size": result.get("uexpSizeAfter"),
                    "package_file_hashes": package_file_hashes,
                    "name_map_indices": name_indices,
                    "name_map_count_before": result.get("nameMapCountBefore"),
                    "name_map_count_after": result.get("nameMapCountAfter"),
                    "names_added_count": (
                        len(result["namesAdded"])
                        if isinstance(result.get("namesAdded"), list) else None
                    ),
                    "fields": [
                        {
                            "export_index": field.get("exportIndex"),
                            "reward_index": field.get("rewardIndex"),
                            "export_name": field.get("exportName"),
                            "offset": field.get("sidecarOffset"),
                            "expected_bytes_hex": field.get("expectedBytesHex"),
                            "expected_identity": _binary_identity_to_json(field.get("expectedIdentity")),
                            "available": True,
                            "unavailable_reason": None,
                        }
                        for field in fields if isinstance(field, dict)
                    ],
                    "prepared_asset": prepared_asset,
                    "preparation_reason": None,
                    "unavailable_reason": None,
                }

        pending_batches: dict[Any, int] = {}
        completed_packages = 0
        if batches:
            with ThreadPoolExecutor(max_workers=worker_limit, thread_name_prefix="cv2-namemap") as pool:
                for batch_index, batch in enumerate(batches):
                    future = pool.submit(prepare_batch, batch_index, batch)
                    pending_batches[future] = batch_index
                pending = set(pending_batches)
                while pending:
                    completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        batch_index = pending_batches[future]
                        batch, batch_result, _largest_size, _estimate = future.result()
                        collect_batch(batch, batch_result)
                        completed_packages += len(batch)
                        if progress:
                            progress(
                                "preparing-world-loot-namemaps",
                                completed_packages,
                                len(package_requests),
                            )

        if profile_callback is not None:
            prepared_count = sum(row.get("prepared_asset") is not None for row in results.values())
            raw_count = sum(
                row.get("preparation_reason") is None and row.get("prepared_asset") is None
                for row in results.values()
            )
            profile_callback(
                "namemap_package_summary",
                packages=len(package_requests),
                serialized_packages=prepared_count,
                already_safe_packages=raw_count,
                fallback_packages=len(fallback),
                names_added=sum(row.get("names_added_count") or 0 for row in results.values()),
                worker_limit=worker_limit,
                batch_count=len(batches),
            )

        for key, (request, reason) in fallback.items():
            source = Path(request["inputFile"]).resolve(strict=True)
            hashes = {file.suffix.casefold(): _sha256_path(file) for file in _package_sidecars(source)}
            primary_hash = hashes.get(source.suffix.casefold())
            sidecar_hash = hashes.get(".uexp")
            results[key] = {
                "version": 3,
                "package_path": request["packagePath"],
                "header_sha256": primary_hash,
                "uexp_sha256": sidecar_hash,
                "header_size": source.stat().st_size,
                "uexp_size": source.with_suffix(".uexp").stat().st_size if sidecar_hash else None,
                "package_file_hashes": hashes,
                "name_map_indices": {},
                "name_map_count_before": None,
                "name_map_count_after": None,
                "names_added_count": None,
                "fields": [],
                "prepared_asset": None,
                "preparation_reason": reason,
                "unavailable_reason": reason,
            }
        return {key: results[key] for key in sorted(results)}

    # Cache the prepared world package
    @staticmethod
    def _store_prepared_world_package(
        work_root: Path,
        prepared_cache_root: Path,
        request: dict[str, Any],
        result: dict[str, Any],
        *,
        root_name: str = "prepared-assets",
    ) -> dict[str, Any]:
        package_path = request["packagePath"]
        relative = PurePosixPath(package_path.removeprefix("/Game/"))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise WorldLootDependencyError("Prepared World Loot package has an unsafe virtual path.")
        if root_name not in {"prepared-assets", "healing-prepared-assets"}:
            raise WorldLootDependencyError("Prepared cache root name is not allowed.")
        destination = prepared_cache_root.joinpath(*relative.parts)
        if not _is_within(destination, prepared_cache_root) or destination.exists() or destination.is_symlink():
            raise WorldLootDependencyError("Prepared World Loot cache destination is unsafe or already exists.")
        temporary = destination.parent / f".{destination.name}.cv2-{uuid.uuid4().hex}.tmp"
        temporary.mkdir(parents=True, exist_ok=False)
        copied: list[Path] = []
        try:
            output_files = result.get("outputFiles")
            if not isinstance(output_files, list) or not output_files:
                raise WorldLootDependencyError("UAssetAPI omitted prepared World Loot package sidecars.")
            helper_hashes = {
                ".umap": result.get("preparedUassetSha256"),
                ".uexp": result.get("preparedUexpSha256"),
            }
            allowed = {".uasset", ".umap", ".uexp", ".ubulk", ".uptnl"}
            rows: list[dict[str, Any]] = []
            for value in output_files:
                if not isinstance(value, str):
                    raise WorldLootDependencyError("Prepared World Loot sidecar path is invalid.")
                source_file = Path(value).resolve(strict=True)
                if not _is_within(source_file, work_root) or source_file.suffix.casefold() not in allowed:
                    raise WorldLootDependencyError("Prepared World Loot sidecar escaped its temporary workspace.")
                target = temporary / (relative.name + source_file.suffix.casefold())
                if target.exists():
                    raise WorldLootDependencyError("Prepared World Loot package contains duplicate sidecars.")
                shutil.copyfile(source_file, target)
                target_hash = _sha256_path(target)
                helper_hash = helper_hashes.get(source_file.suffix.casefold())
                source_hash = helper_hash if isinstance(helper_hash, str) else _sha256_path(source_file)
                if source_hash.casefold() != target_hash.casefold():
                    raise WorldLootDependencyError("Prepared World Loot sidecar failed copy integrity validation.")
                copied.append(target)
                rows.append({"name": target.name, "size": target.stat().st_size, "sha256": target_hash})
            package_header_suffix = ".uasset" if package_path.startswith("/Game/CV2Randomizer/Healing/") else ".umap"
            if not any(path.suffix.casefold() == package_header_suffix for path in copied) or not any(
                path.suffix.casefold() == ".uexp" for path in copied
            ):
                raise WorldLootDependencyError(f"Prepared package is missing its {package_header_suffix} or .uexp sidecar.")
            rows.sort(key=lambda row: Path(row["name"]).suffix.casefold())
            manifest = {"schema_version": 1, "virtual_path": package_path, "files": rows}
            (temporary / ".cv2randomizer-prepared-asset.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            _publish_prepared_cache_entry(temporary, destination)
            result_files = [
                {"name": row["name"], "size": row["size"], "sha256": row["sha256"]}
                for row in rows
            ]
            return {"relative_path": relative.as_posix(), "root_name": root_name, "files": result_files}
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    # Run blueprint parents
    def _run_blueprint_parents(
        self,
        work_root: Path,
        mappings: Path,
        assets: tuple[MaterializedAsset, ...],
        *,
        memory_scheduler: MemoryBudgetScheduler,
        diagnostics_callback: Callable[[str, str], None] | None,
        helper_metrics_callback: Callable[[str, object], None] | None,
        worker_mappings: tuple[Path, ...] | None = None,
        profile_callback: Callable[..., None] | None = None,
    ) -> Any:
        if not assets:
            return []
        estimates = [
            memory_scheduler.estimate_job_bytes(
                f"blueprint-parent:{asset.virtual_path.casefold()}",
                _package_size(asset.primary_file),
            )
            for asset in assets
        ]
        worker_limit = max(1, memory_scheduler.recommended_worker_count(estimates))
        if worker_mappings is None or len(worker_mappings) < worker_limit:
            worker_mappings = self._prepare_worker_mappings(work_root, mappings, worker_limit)
        else:
            worker_mappings = worker_mappings[:worker_limit]
        results: list[dict[str, Any] | None] = [None] * len(assets)

        indexed_assets = tuple(enumerate(assets))
        batches = _size_bounded_batches(indexed_assets, maximum_items=_BLUEPRINT_PARENT_BATCH_SIZE)

        # Inspect parent batch
        def inspect_parent_batch(
            batch_index: int,
            batch: tuple[tuple[int, MaterializedAsset], ...],
        ) -> tuple[tuple[int, dict[str, Any]], ...]:
            batch_assets = tuple(asset for _index, asset in batch)
            batch_key = f"blueprint-parent-batch:{batch_index}:{batch_assets[0].virtual_path.casefold()}"
            if diagnostics_callback is not None:
                diagnostics_callback(
                    "resolving-blueprint-parents",
                    f"batch {batch_index}/{len(batches)}; {len(batch_assets)} packages",
                )
            largest_size = max(_package_size(asset.primary_file) for asset in batch_assets)
            estimate = max(
                memory_scheduler.estimate_job_bytes(
                    f"blueprint-parent:{asset.virtual_path.casefold()}",
                    _package_size(asset.primary_file),
                )
                for asset in batch_assets
            )
            worker_mapping = _mapping_for_current_worker(worker_mappings)
            if profile_callback is not None:
                profile_callback(
                    "helper_launch",
                    helper_operation="blueprint-parents",
                    package_count=len(batch_assets),
                    package_count_total=len(assets),
                    batch_index=batch_index,
                    batch_count=len(batches),
                    worker_limit=worker_limit,
                    maximum_package_bytes=largest_size,
                )
            command = self._helper_command(
                "blueprint-parents", str(work_root), str(worker_mapping), self.engine_version,
                *(str(asset.primary_file.resolve()) for asset in batch_assets),
            )
            helper_started = time.perf_counter()
            with memory_scheduler.job(batch_key, package_bytes=largest_size, estimated_bytes=estimate):
                result = self._run(
                    command,
                    work_root,
                    memory_job=batch_key,
                    memory_reserved=True,
                    diagnostics_callback=diagnostics_callback,
                )
            if profile_callback is not None:
                profile_callback(
                    "helper_batch_wall_time",
                    helper_operation="blueprint-parents",
                    package_count=len(batch_assets),
                    batch_index=batch_index,
                    batch_count=len(batches),
                    elapsed_seconds=time.perf_counter() - helper_started,
                )
            if not isinstance(result, list) or len(result) != len(batch) or any(
                not isinstance(row, dict) for row in result
            ):
                raise WorldLootDependencyError(
                    f"UAssetAPI Blueprint-parent result count did not match batch {batch_index}."
                )
            completed_rows: list[tuple[int, dict[str, Any]]] = []
            for (index, asset), row in zip(batch, result, strict=True):
                memory = row.get("memory")
                if isinstance(memory, dict):
                    job_key = f"blueprint-parent:{asset.virtual_path.casefold()}"
                    memory_scheduler.record_peak(job_key, _positive_int(memory.get("peakWorkingSetBytes")) or 0)
                    if helper_metrics_callback is not None:
                        helper_metrics_callback(job_key, {
                            **memory,
                            "package": asset.virtual_path,
                            "operation": "blueprint-parents",
                            "reason": "World Loot Blueprint parent dependency",
                            "systems": ["World Loot"],
                            "loadMilliseconds": row.get("loadMilliseconds"),
                            "analysisMilliseconds": row.get("analysisMilliseconds"),
                        })
                completed_rows.append((index, row))
            return tuple(completed_rows)

        with ThreadPoolExecutor(max_workers=worker_limit, thread_name_prefix="cv2-parent-inspect") as pool:
            future_indexes = {
                pool.submit(inspect_parent_batch, batch_index, batch): batch_index
                for batch_index, batch in enumerate(batches, start=1)
            }
            pending = set(future_indexes)
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    for index, row in future.result():
                        results[index] = row
        return [result for result in results if result is not None]

    # Prepare worker mappings
    @staticmethod
    def _prepare_worker_mappings(work_root: Path, mappings: Path, worker_count: int) -> tuple[Path, ...]:
        """Give each concurrently running UAssetAPI process its own identical .usmap file.

        UAssetAPI opens mapping paths with exclusive sharing on Windows, so
        multiple helper processes cannot safely open one shared file handle path.
        """
        if worker_count <= 0:
            raise WorldLootDependencyError("UAssetAPI worker count must be positive.")
        root = work_root.resolve()
        mapping_root = root / ".cv2-helper-mappings"
        if mapping_root.is_symlink() or not _is_within(mapping_root, root):
            raise WorldLootDependencyError("Temporary UAssetAPI mappings escaped the dependency workspace.")
        mapping_root.mkdir(parents=True, exist_ok=True)
        try:
            data = mappings.read_bytes()
        except OSError as error:
            raise WorldLootDependencyError(f"Could not read CV2.usmap for isolated helper workers: {error}") from error
        expected_digest = hashlib.sha256(data).hexdigest()
        copies: list[Path] = []
        for index in range(worker_count):
            target = mapping_root / f"worker-{index}.usmap"
            temporary = mapping_root / f".worker-{index}-{uuid.uuid4().hex}.tmp"
            try:
                temporary.write_bytes(data)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected_digest:
                    raise WorldLootDependencyError(f"Temporary CV2.usmap copy failed integrity validation: {target.name}.")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            copies.append(target)
        return tuple(copies)

    # Run helper JSON
    def _run_helper_json(
        self,
        work_root: Path,
        mappings: Path,
        operation: str,
        document: dict[str, Any],
        *,
        memory_scheduler: MemoryBudgetScheduler | None = None,
        job_key: str | None = None,
        job_reserved: bool = False,
        diagnostics_callback: Callable[[str, str], None] | None = None,
    ) -> Any:
        manifest = work_root / f".cv2-world-loot-{uuid.uuid4().hex}.json"
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            command = self._helper_command(
                operation, str(work_root), str(manifest), str(mappings), self.engine_version,
            )
            return self._run(
                command,
                work_root,
                memory_scheduler=memory_scheduler,
                memory_job=job_key,
                memory_reserved=job_reserved,
                diagnostics_callback=diagnostics_callback,
            )
        finally:
            if manifest.is_file() and _is_within(manifest, work_root):
                manifest.unlink()

    # Run the external helper command
    def _run(
        self,
        command: list[str],
        work_root: Path,
        *,
        memory_scheduler: MemoryBudgetScheduler | None = None,
        memory_job: str | None = None,
        memory_reserved: bool = False,
        diagnostics_callback: Callable[[str, str], None] | None = None,
    ) -> Any:
        if memory_job and diagnostics_callback is not None:
            diagnostics_callback("uassetapi-helper", memory_job)

        # Run the requested helper operation
        def execute() -> subprocess.CompletedProcess[str]:
            kwargs: dict[str, Any] = {
                "check": False,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": self.timeout_seconds,
                "cwd": work_root,
            }
            if self.runner is run_hidden:
                environment = os.environ.copy()
                environment["DOTNET_GCHeapHardLimit"] = DOTNET_GC_HEAP_HARD_LIMIT
                kwargs["env"] = environment
            return self.runner(command, **kwargs)

        try:
            if memory_job and memory_scheduler is not None and not memory_reserved:
                with memory_scheduler.job(memory_job, estimated_bytes=1024 * 1024**2):
                    completed = execute()
            else:
                completed = execute()
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WorldLootDependencyError(f"Could not run UAssetAPI dependency inspection: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-6000:]
            raise WorldLootDependencyError(
                f"UAssetAPI dependency inspection failed (exit {completed.returncode}): {detail}"
            )
        try:
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            return json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError) as error:
            raise WorldLootDependencyError("UAssetAPI dependency inspector returned invalid JSON.") from error

    # Ensure helper ready
    def _ensure_helper_ready(
        self,
        memory_scheduler: MemoryBudgetScheduler,
        diagnostics_callback: Callable[[str, str], None] | None,
    ) -> None:
        if self.runner is not run_hidden:
            return
        if self.helper_project.suffix.casefold() == ".exe":
            if not self.helper_project.is_file():
                raise WorldLootDependencyError(f"Bundled UAssetAPI helper was not found: {self.helper_project}")
            self._helper_ready = True
            return
        assembly = self._helper_assembly_path()
        sources = (*self.helper_project.parent.glob("*.cs"), self.helper_project)
        needs_build = not assembly.is_file() or any(source.stat().st_mtime_ns > assembly.stat().st_mtime_ns for source in sources)
        if not needs_build:
            self._helper_ready = True
            return
        with self._helper_build_lock:
            if self._helper_ready:
                return
            if diagnostics_callback is not None:
                diagnostics_callback("building-uassetapi-helper", str(self.helper_project))
            command = _uassetapi_build_command(self.dotnet_executable, self.helper_project)
            try:
                with memory_scheduler.job("build UAssetAPI helper", estimated_bytes=1024 * 1024**2):
                    completed = self.runner(
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=self.timeout_seconds,
                        cwd=self.helper_project.parent,
                    )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise WorldLootDependencyError(f"Could not build the UAssetAPI helper: {error}") from error
            if completed.returncode != 0 or not assembly.is_file():
                detail = (completed.stderr or completed.stdout).strip()[-4000:]
                raise WorldLootDependencyError(f"Could not build the UAssetAPI helper: {detail or 'assembly missing after build'}")
            self._helper_ready = True

    # Find the built helper assembly
    def _helper_assembly_path(self) -> Path:
        return self.helper_project.parent / "bin" / "Release" / "net10.0" / "UAssetAPIWorldLoot.dll"

    # Read helper command
    def _helper_command(self, *arguments: str) -> list[str]:
        if self.helper_project.suffix.casefold() == ".exe":
            return uassetapi_command(self.helper_project, self.dotnet_executable, *arguments)
        if self.runner is run_hidden and self._helper_ready:
            return [self.dotnet_executable, "exec", str(self._helper_assembly_path()), *arguments]
        return [
            self.dotnet_executable, "run", "--configuration", "Release", "--project", str(self.helper_project), "--",
            *arguments,
        ]


# Build the UAssetAPI helper command
def _uassetapi_build_command(dotnet_executable: str, helper_project: Path) -> list[str]:
    """Build the helper using dotnet's positional project argument."""
    command = [dotnet_executable, "build", str(helper_project), "--configuration", "Release"]
    if (helper_project.parent / "obj" / "project.assets.json").is_file():
        command.append("--no-restore")
    return command


# Validate cached location descriptor
def _validate_cached_location_descriptor(
    location: WorldLootLocation,
    descriptor: Any,
) -> None:
    if not isinstance(descriptor, dict):
        raise WorldLootDependencyError(
            f"Cached World Loot descriptor omitted {location.package_path}#{location.export_index}."
        )
    if (
        descriptor.get("package_path") != location.package_path
        or descriptor.get("export_index") != location.export_index
        or descriptor.get("mirror_of_package_path") != location.mirror_of_package_path
        or descriptor.get("mirror_of_export_index") != location.mirror_of_export_index
        or descriptor.get("export_name") != location.export_name
        or descriptor.get("actor_class") != location.actor_class
        or descriptor.get("actor_category") != location.actor_category
        or descriptor.get("destination_kind") != location.destination_kind
        or descriptor.get("location_type") != location.location_type
        or descriptor.get("structural_class_package") != location.structural_class_package
        or descriptor.get("protected") != location.protected
        or descriptor.get("protection_reasons") != list(location.protection_reasons)
        or descriptor.get("physical_instance_count") != location.physical_instance_count
        or descriptor.get("parent_blueprint_packages") != list(location.parent_blueprint_packages)
        or descriptor.get("package_available") is not True
        or descriptor.get("prepared") is not True
    ):
        raise WorldLootDependencyError(
            f"Cached World Loot descriptor no longer matches {location.package_path}#{location.export_index}."
        )
    cached_rewards = descriptor.get("rewards")
    if not isinstance(cached_rewards, list) or len(cached_rewards) != len(location.rewards):
        raise WorldLootDependencyError(
            f"Cached World Loot descriptor has the wrong reward count for {location.package_path}#{location.export_index}."
        )
    for reward_index, (reward, cached) in enumerate(zip(location.rewards, cached_rewards, strict=True)):
        identity = reward.soft_object_path_identity
        expected_identity = None if identity is None else {
            "package_name": {"value": identity.package_name.value, "number": identity.package_name.number},
            "asset_name": {"value": identity.asset_name.value, "number": identity.asset_name.number},
        }
        if (
            not isinstance(cached, dict)
            or cached.get("reward_index") != reward_index
            or cached.get("article_path") != reward.article_path
            or cached.get("count") != reward.count
            or cached.get("notification") != reward.notification
            or cached.get("soft_object_path_identity") != expected_identity
        ):
            raise WorldLootDependencyError(
                f"Cached World Loot reward descriptor differs from the catalog at "
                f"{location.package_path}#{location.export_index}:{reward_index}."
            )
    if location.instance_override_source is not None:
        source = location.instance_override_source
        expected_source = {
            "cdo_package": source.cdo_package,
            "cdo_export_index": source.cdo_export_index,
            "cdo_export_name": source.cdo_export_name,
            "class_package": source.class_package,
            "parent_blueprint_packages": list(source.parent_blueprint_packages),
        }
        if descriptor.get("instance_override_source") != expected_source:
            raise WorldLootDependencyError("Cached World Loot instance override source no longer matches the catalog.")


# Validate blueprint path
def _validate_blueprint_path(value: str) -> str:
    parsed = PurePosixPath(value)
    if (
        not value.startswith("/Game/") or "\\" in value or not parsed.is_absolute()
        or parsed.suffix or any(part in {"", ".", ".."} for part in parsed.parts[1:])
        or len(parsed.parts) < 3
    ):
        raise WorldLootDependencyError(f"Invalid generated Blueprint package path: {value!r}.")
    return "/" + parsed.as_posix().lstrip("/")


# Find binary identity to JSON
def _binary_identity_to_json(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    package_name = value.get("packageName")
    asset_name = value.get("assetName")
    if not isinstance(package_name, dict) or not isinstance(asset_name, dict):
        return None
    return {
        "package_name": {"value": package_name.get("value"), "number": package_name.get("number")},
        "asset_name": {"value": asset_name.get("value"), "number": asset_name.get("number")},
    }


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


# Build package size
def _package_size(header: Path) -> int:
    """Estimate source bytes from a package header and its Unreal sidecars."""
    total = 0
    for suffix in (".uasset", ".uexp", ".ubulk", ".uptnl"):
        sidecar = header.with_suffix(suffix)
        try:
            total += sidecar.stat().st_size
        except OSError:
            continue
    return total


# Build package sidecars
def _package_sidecars(header: Path) -> tuple[Path, ...]:
    return tuple(
        path for path in (header, *(header.with_suffix(extension) for extension in (".uexp", ".ubulk", ".uptnl")))
        if path.is_file()
    )


# Parse SHA-256 path
def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Parse a positive integer
def _positive_int(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    return None


# Parse mapping for current worker
def _mapping_for_current_worker(copies: tuple[Path, ...]) -> Path:
    if not copies:
        raise WorldLootDependencyError("No isolated CV2.usmap copy was assigned to the helper worker.")
    suffix = threading.current_thread().name.rsplit("_", 1)[-1]
    index = int(suffix) if suffix.isdecimal() else 0
    return copies[index % len(copies)]
