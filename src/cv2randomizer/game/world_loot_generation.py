"""Apply a complete world-loot assignment plan to isolated staged map copies."""

from __future__ import annotations

import hashlib
import inspect
import logging
import shutil
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from cv2randomizer.randomizer.world_loot import WorldLootRandomizationPlan

from .asset_extraction import (
    AssetExtractionError,
    IoStoreAssetExtractor,
    materialize_extracted_assets,
)
from .catalogs import RewardPayload, WorldLootLocation
from .world_loot_dependencies import WorldLootDependencyResolver
from .world_loot_binary_patch import (
    binary_name_map_fallback_reason,
    preflight_binary_package,
    write_binary_package,
)
from .world_loot_serialization import (
    ArticleReward,
    HealingUpgradePackagePatch,
    HealingUpgradePatch,
    WorldLootPackagePatch,
    WorldLootPackagePatchResult,
    WorldLootSerializer,
    WorldLootTuplePatch,
)

LOGGER = logging.getLogger(__name__)


class WorldLootGenerationError(RuntimeError):
    """Raised when a world-loot plan cannot be committed to staging safely."""


@dataclass(frozen=True, slots=True)
class WorldLootGenerationResult:
    """Validated assignments and map packages copied into a separate output tree."""

    assignment_count: int
    reward_patch_count: int
    package_results: tuple[WorldLootPackagePatchResult, ...]
    output_packages: tuple[Path, ...]


# Apply world loot plan
def apply_world_loot_plan(
    plan: WorldLootRandomizationPlan,
    *,
    game_root: Path,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    resolver: WorldLootDependencyResolver | None = None,
    extractor: IoStoreAssetExtractor | None = None,
    dependency_index: dict[str, object] | None = None,
    serializer: WorldLootSerializer | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> WorldLootGenerationResult:
    """Stage required dependencies, patch maps in batches and copy only validated outputs.

    Destination payloads are compared to their source payloads first. Identical
    payloads remain unwritten while the assignment ledger still accounts for every
    destination in the plan.
    """
    root = work_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    output_root = output_content_root.expanduser().resolve()
    if not _is_within(output_root, root) or output_root == root:
        raise WorldLootGenerationError("Output Content tree must be a separate directory inside the generation work root.")
    if output_root == (root / "source" / "Content").resolve():
        raise WorldLootGenerationError("Patched output Content cannot replace source staging inputs.")
    mappings = mappings_path.expanduser().resolve(strict=True)
    if not mappings.is_file():
        raise WorldLootGenerationError("World-loot mapping file does not exist.")

    by_package: dict[str, list[WorldLootTuplePatch]] = defaultdict(list)
    healing_by_package: dict[str, list[tuple[WorldLootLocation, ArticleReward, ArticleReward]]] = defaultdict(list)
    required_locations = []
    changed_reward_count = 0
    for assignment in plan.assignments:
        destination = assignment.destination
        if destination.destination_kind not in {
            "ARTICLE_BULK", "BLUEPRINT_CDO_ARTICLE_BULK", "INSTANCE_ARTICLE_BULK_OVERRIDE",
        }:
            continue
        assigned_rewards = assignment.assigned_rewards
        if len(destination.rewards) != len(assigned_rewards):
            raise WorldLootGenerationError("Assignment changes a location's Articles array arity.")
        for reward_index, (original, assigned) in enumerate(zip(destination.rewards, assigned_rewards, strict=True)):
            if original == assigned:
                continue
            if destination.destination_kind == "INSTANCE_ARTICLE_BULK_OVERRIDE":
                healing_by_package[destination.package_path].append((
                    destination, _to_article_reward(original), _to_article_reward(assigned),
                ))
                changed_reward_count += 1
                required_locations.append(destination)
                continue
            by_package[destination.package_path].append(WorldLootTuplePatch(
                export_index=destination.export_index,
                reward_index=reward_index,
                export_name=destination.export_name,
                expected_original=_to_article_reward(original),
                assigned=_to_article_reward(assigned),
                require_unique_save_id=destination.destination_kind == "ARTICLE_BULK",
            ))
            changed_reward_count += 1
            required_locations.append(destination)

    if not by_package and not healing_by_package:
        return WorldLootGenerationResult(len(plan.assignments), 0, (), ())

    dependency_resolver = resolver or WorldLootDependencyResolver()
    package_serializer = serializer or WorldLootSerializer()
    asset_source = extractor or getattr(dependency_resolver, "extractor", None) or IoStoreAssetExtractor()
    raw_binary_descriptors = (
        dependency_index.get("world_loot_binary_patch_packages", {})
        if isinstance(dependency_index, dict) else {}
    )
    binary_descriptors = {
        key.casefold(): value for key, value in raw_binary_descriptors.items()
    } if isinstance(raw_binary_descriptors, dict) else {}
    healing_map_descriptors = (
        dependency_index.get("healing_prepared_maps", {})
        if isinstance(dependency_index, dict) else {}
    )
    if isinstance(healing_map_descriptors, dict):
        # A map prepared for Healing may also contain ordinary World Loot rows.
        # Its single final Extract output carries both transformations and can
        # serve as the binary baseline when this seed changes no Healing reward.
        for package_key, descriptor in healing_map_descriptors.items():
            if (
                isinstance(package_key, str)
                and isinstance(descriptor, dict)
                and isinstance(descriptor.get("fields"), list)
                and descriptor.get("fields")
                and isinstance(descriptor.get("prepared_asset"), dict)
            ):
                binary_descriptors.setdefault(package_key.casefold(), descriptor)
    healing_package_keys = {value.casefold() for value in healing_by_package}
    binary_candidates = tuple(
        package for package in sorted(by_package, key=str.casefold)
        if package.casefold() not in healing_package_keys
        and package.casefold() in binary_descriptors
    )
    binary_package_results: dict[str, WorldLootPackagePatchResult] = {}
    if binary_candidates:
        structural_fallback_count = 0
        namemap_fallback_count = 0
        byte_validation_candidates: list[str] = []
        for package in binary_candidates:
            fallback_reason = binary_name_map_fallback_reason(
                package,
                binary_descriptors[package.casefold()],
                tuple(sorted(by_package[package], key=lambda patch: (patch.export_index, patch.reward_index))),
            )
            if fallback_reason is None:
                byte_validation_candidates.append(package)
                continue
            structural_fallback_count += 1
            if "NameMap entry" in fallback_reason:
                namemap_fallback_count += 1
            LOGGER.info("World Loot structural fallback package=%s reason=%s", package, fallback_reason)

        assets_by_key: dict[str, object] = {}
        prepared_candidates = tuple(
            package for package in byte_validation_candidates
            if isinstance(binary_descriptors[package.casefold()].get("prepared_asset"), dict)
        )
        raw_candidates = tuple(
            package for package in byte_validation_candidates
            if binary_descriptors[package.casefold()].get("prepared_asset") is None
        )
        extract_prepared = getattr(asset_source, "extract_prepared_assets", None)
        extract_prepared_with_descriptors = getattr(
            asset_source, "extract_prepared_assets_with_descriptors", None,
        )
        prepared_descriptors = {
            package.casefold(): binary_descriptors[package.casefold()]
            for package in prepared_candidates
            if isinstance(binary_descriptors.get(package.casefold()), dict)
        }
        if (
            prepared_candidates
            and callable(extract_prepared_with_descriptors)
            and len(prepared_descriptors) == len(prepared_candidates)
        ):
            try:
                prepared_assets = extract_prepared_with_descriptors(
                    game_root, prepared_descriptors,
                    game_version=game_version, data_revision=data_revision,
                    progress=progress,
                )
            except Exception as error:
                raise WorldLootGenerationError(
                    f"Could not read prepared cache packages for binary World Loot patching: {error}"
                ) from error
            assets_by_key.update({asset.virtual_path.casefold(): asset for asset in prepared_assets})
            if len(prepared_assets) != len(prepared_candidates):
                raise WorldLootGenerationError("Prepared cache omitted a binary World Loot package.")
        elif prepared_candidates and callable(extract_prepared):
            try:
                prepared_assets = extract_prepared(
                    game_root, prepared_candidates,
                    game_version=game_version, data_revision=data_revision,
                    progress=progress,
                )
            except Exception as error:
                raise WorldLootGenerationError(
                    f"Could not read prepared cache packages for binary World Loot patching: {error}"
                ) from error
            assets_by_key.update({asset.virtual_path.casefold(): asset for asset in prepared_assets})
            if len(prepared_assets) != len(prepared_candidates):
                raise WorldLootGenerationError("Prepared cache omitted a binary World Loot package.")
        elif prepared_candidates:
            for package in prepared_candidates:
                binary_candidates = tuple(item for item in binary_candidates if item.casefold() != package.casefold())
                byte_validation_candidates = tuple(
                    item for item in byte_validation_candidates if item.casefold() != package.casefold()
                )
                structural_fallback_count += 1
                LOGGER.info("World Loot structural fallback package=%s reason=prepared cache reader unavailable", package)
        if raw_candidates:
            try:
                binary_assets = asset_source.extract_assets(
                    game_root, raw_candidates,
                    game_version=game_version, data_revision=data_revision,
                    progress=progress,
                )
            except Exception as error:
                raise WorldLootGenerationError(f"Could not read cached packages for binary World Loot patching: {error}") from error
            assets_by_key.update({asset.virtual_path.casefold(): asset for asset in binary_assets})
            if len(binary_assets) != len(raw_candidates):
                raise WorldLootGenerationError("Cached extraction omitted a binary World Loot package.")
        binary_plans = {}
        for package in byte_validation_candidates:
            binary_plan, fallback_reason = preflight_binary_package(
                package,
                assets_by_key[package.casefold()],
                binary_descriptors[package.casefold()],
                tuple(sorted(by_package[package], key=lambda patch: (patch.export_index, patch.reward_index))),
            )
            if binary_plan is None:
                structural_fallback_count += 1
                LOGGER.info("World Loot structural fallback package=%s reason=%s", package, fallback_reason)
            else:
                binary_plans[package.casefold()] = binary_plan
        for package_key, binary_plan in sorted(binary_plans.items()):
            try:
                binary_package_results[package_key] = write_binary_package(
                    binary_plan, work_root=root, output_content_root=output_root,
                )
            except Exception as error:
                raise WorldLootGenerationError(
                    f"Binary World Loot patch failed after preflight for {binary_plan.package_path}: {error}"
                ) from error
        LOGGER.info(
            "World Loot binary fast path: patched %s/%s candidate packages; %s fell back to structural patching",
            len(binary_package_results), len(binary_candidates), structural_fallback_count,
        )
        LOGGER.info(
            "World Loot NameMap fallback: %s/%s packages require missing destination names",
            namemap_fallback_count,
            len(binary_candidates),
        )

    # Per-instance ArticleDataPath overrides need their owning Blueprint/CDO.
    # Extract prepares the audited Healing structures once; other instance
    # override cases stay on the structural fallback. Keep each map+clone as
    # one preflight unit so a failed descriptor cannot leave partial files.
    healing_clone_binary_results: dict[str, WorldLootPackagePatchResult] = {}
    healing_clone_descriptors = (
        dependency_index.get("healing_prepared_clones", {})
        if isinstance(dependency_index, dict) else {}
    )
    if healing_by_package and isinstance(healing_map_descriptors, dict) and isinstance(healing_clone_descriptors, dict):
        fast_candidate_maps: dict[str, tuple[dict, dict[str, dict], list[tuple[WorldLootLocation, ArticleReward, ArticleReward]]]] = {}
        prepared_requests: dict[str, dict] = {}
        structural_reasons: dict[str, str] = {}
        for package_path, entries in sorted(healing_by_package.items(), key=lambda item: item[0].casefold()):
            map_key = package_path.casefold()
            map_descriptor = healing_map_descriptors.get(map_key)
            if not isinstance(map_descriptor, dict):
                structural_reasons[map_key] = "no Extract-time prepared Healing map"
                continue
            clone_descriptors_for_map: dict[str, dict] = {}
            patches_by_clone: list[tuple[str, WorldLootTuplePatch]] = []
            valid = True
            reason = None
            for location, expected, assigned in sorted(entries, key=lambda item: item[0].export_index):
                source = location.instance_override_source
                if source is None:
                    valid, reason = False, "catalog location has no Blueprint override identity"
                    break
                clone_path = _healing_clone_package_path(package_path, location)
                clone_key = clone_path.casefold()
                clone_descriptor = healing_clone_descriptors.get(clone_key)
                if not isinstance(clone_descriptor, dict):
                    valid, reason = False, f"no Extract-time prepared Healing clone for {clone_path}"
                    break
                clone_descriptors_for_map[clone_key] = clone_descriptor
                patch = WorldLootTuplePatch(
                    export_index=source.cdo_export_index,
                    reward_index=0,
                    export_name=source.cdo_export_name,
                    expected_original=expected,
                    assigned=assigned,
                    require_unique_save_id=False,
                )
                fallback = binary_name_map_fallback_reason(clone_path, clone_descriptor, (patch,))
                if fallback is not None:
                    valid, reason = False, f"Healing clone {clone_path}: {fallback}"
                    break
                patches_by_clone.append((clone_path, patch))
            direct_patches = tuple(sorted(by_package.get(package_path, ()), key=lambda patch: (patch.export_index, patch.reward_index)))
            if valid:
                map_fallback = binary_name_map_fallback_reason(package_path, map_descriptor, direct_patches)
                if map_fallback is not None:
                    valid, reason = False, f"Healing map {package_path}: {map_fallback}"
            if not valid:
                structural_reasons[map_key] = reason or "Healing template preflight failed"
                continue
            prepared_requests[map_key] = map_descriptor
            prepared_requests.update(clone_descriptors_for_map)
            fast_candidate_maps[map_key] = (map_descriptor, clone_descriptors_for_map, entries)

        extract_prepared_custom = getattr(asset_source, "extract_prepared_assets_with_descriptors", None)
        prepared_assets_by_key: dict[str, object] = {}
        if prepared_requests and callable(extract_prepared_custom):
            try:
                prepared_assets = extract_prepared_custom(
                    game_root,
                    prepared_requests,
                    game_version=game_version,
                    data_revision=data_revision,
                    progress=progress,
                )
                prepared_assets_by_key = {asset.virtual_path.casefold(): asset for asset in prepared_assets}
                if len(prepared_assets_by_key) != len(prepared_requests):
                    raise WorldLootGenerationError("Prepared cache omitted a Healing template package.")
            except Exception as error:
                LOGGER.info("Instance override binary fast path unavailable; using structural fallback: %s", error)
                structural_reasons.update({key: "prepared Healing cache package could not be read" for key in fast_candidate_maps})
                fast_candidate_maps.clear()
                prepared_requests.clear()
        elif prepared_requests:
            structural_reasons.update({key: "prepared-cache descriptor reader unavailable" for key in fast_candidate_maps})
            fast_candidate_maps.clear()
            prepared_requests.clear()

        map_plans = {}
        clone_plans_by_map: dict[str, dict[str, object]] = {}
        for map_key, (map_descriptor, clone_descriptors_for_map, entries) in fast_candidate_maps.items():
            package_path = next(location.package_path for location, _, _ in entries)
            map_asset = prepared_assets_by_key.get(map_key)
            if map_asset is None:
                structural_reasons[map_key] = "prepared Healing map was not returned by cache reader"
                continue
            direct_patches = tuple(sorted(by_package.get(package_path, ()), key=lambda patch: (patch.export_index, patch.reward_index)))
            map_plan, reason = preflight_binary_package(package_path, map_asset, map_descriptor, direct_patches)
            local_clone_plans = {}
            if map_plan is None:
                structural_reasons[map_key] = reason or "prepared Healing map preflight failed"
                continue
            for location, expected, assigned in sorted(entries, key=lambda item: item[0].export_index):
                source = location.instance_override_source
                assert source is not None
                clone_path = _healing_clone_package_path(package_path, location)
                clone_key = clone_path.casefold()
                clone_asset = prepared_assets_by_key.get(clone_key)
                if clone_asset is None:
                    reason = f"prepared Healing clone was not returned: {clone_path}"
                    break
                patch = WorldLootTuplePatch(
                    export_index=source.cdo_export_index,
                    reward_index=0,
                    export_name=source.cdo_export_name,
                    expected_original=expected,
                    assigned=assigned,
                    require_unique_save_id=False,
                )
                clone_plan, reason = preflight_binary_package(
                    clone_path, clone_asset, clone_descriptors_for_map[clone_key], (patch,),
                )
                if clone_plan is None:
                    reason = f"Healing clone {clone_path}: {reason}"
                    break
                local_clone_plans[clone_key] = clone_plan
            if reason is not None:
                structural_reasons[map_key] = reason
                continue
            map_plans[map_key] = map_plan
            clone_plans_by_map[map_key] = local_clone_plans

        # Commit only after every map and all of its clones passed validation.
        for map_key, binary_plan in sorted(map_plans.items()):
            try:
                binary_package_results[map_key] = write_binary_package(
                    binary_plan, work_root=root, output_content_root=output_root,
                )
                for clone_key, clone_plan in sorted(clone_plans_by_map[map_key].items()):
                    healing_clone_binary_results[clone_key] = write_binary_package(
                        clone_plan, work_root=root, output_content_root=output_root,
                    )
            except Exception as error:
                raise WorldLootGenerationError(
                    f"Prepared Healing binary patch failed after preflight for {binary_plan.package_path}: {error}"
                ) from error
        for map_key, reason in sorted(structural_reasons.items()):
            LOGGER.info("Instance override structural fallback package=%s reason=%s", map_key, reason)
        LOGGER.info(
            "Instance override fast path: patched %s/%s map packages and %s isolated Healing clones; "
            "%s packages fell back structurally",
            len(map_plans), len(healing_by_package), len(healing_clone_binary_results),
            len(structural_reasons),
        )
    try:
        map_locations = tuple(
            location for location in required_locations
            if location.destination_kind in {"ARTICLE_BULK", "INSTANCE_ARTICLE_BULK_OVERRIDE"}
            and location.package_path.casefold() not in binary_package_results
        )
        cdo_locations = tuple(
            location for location in required_locations
            if location.destination_kind == "BLUEPRINT_CDO_ARTICLE_BULK"
        )
        staged = None
        structural_healing_keys = {
            package.casefold() for package in healing_by_package
        } - set(binary_package_results)
        if map_locations:
            resolver_kwargs = dict(
                game_root=game_root,
                locations=map_locations,
                work_root=root,
                mappings_path=mappings,
                game_version=game_version,
                data_revision=data_revision,
                progress=progress,
            )
            if extractor is not None and "extractor" in inspect.signature(dependency_resolver.stage_for_locations).parameters:
                resolver_kwargs["extractor"] = extractor
            if dependency_index is not None and "dependency_index" in inspect.signature(dependency_resolver.stage_for_locations).parameters:
                resolver_kwargs["dependency_index"] = dependency_index
            staged = dependency_resolver.stage_for_locations(**resolver_kwargs)
        content_root = (
            staged.content_root if staged is not None else root / "source" / "Content"
        )
        cdo_assets = ()
        instance_override_locations = tuple(
            location for location in required_locations
            if location.instance_override_source is not None
            and location.package_path.casefold() in structural_healing_keys
        )
        if cdo_locations or instance_override_locations:
            cdo_and_parent_packages = {
                package
                for location in cdo_locations
                for package in (location.package_path, *location.parent_blueprint_packages)
            }
            cdo_and_parent_packages.update(
                package
                for location in instance_override_locations
                for package in (
                    location.instance_override_source.cdo_package,
                    location.instance_override_source.class_package,
                    *location.instance_override_source.parent_blueprint_packages,
                )
            )
            extracted = asset_source.extract_assets(
                game_root,
                sorted(cdo_and_parent_packages, key=str.casefold),
                game_version=game_version,
                data_revision=data_revision,
                progress=progress,
            )
            cdo_assets = materialize_extracted_assets(extracted, content_root)
    except Exception as error:
        if isinstance(error, WorldLootGenerationError):
            raise
        raise WorldLootGenerationError(f"Could not stage world-loot dependencies: {error}") from error

    package_by_virtual = {
        asset.virtual_path.casefold(): asset
        for asset in (staged.maps if staged is not None else ())
    }
    package_by_virtual.update({asset.virtual_path.casefold(): asset for asset in cdo_assets})
    package_requests: list[WorldLootPackagePatch] = []
    virtual_by_source: dict[Path, str] = {}
    for package_path in sorted(by_package, key=str.casefold):
        if package_path.casefold() in binary_package_results:
            continue
        asset = package_by_virtual.get(package_path.casefold())
        if asset is None:
            raise WorldLootGenerationError(f"Source staging omitted destination package {package_path}.")
        source_file = asset.primary_file.resolve(strict=True)
        output_file = source_file.with_name(source_file.stem + ".cv2-patched" + source_file.suffix)
        package_requests.append(WorldLootPackagePatch(
            source_file,
            output_file,
            tuple(sorted(by_package[package_path], key=lambda patch: (patch.export_index, patch.reward_index))),
        ))
        virtual_by_source[source_file] = package_path

    if progress and package_requests:
        progress("patching-world-loot", 0, len(package_requests))
    try:
        package_results = package_serializer.patch_packages(
            work_root=root,
            mappings_path=mappings,
            packages=package_requests,
            progress=(lambda current, total: progress("patching-world-loot-package", current, total)) if progress else None,
        ) if package_requests else ()
    except Exception as error:
        if isinstance(error, WorldLootGenerationError):
            raise
        raise WorldLootGenerationError(f"World-loot map patching failed validation: {error}") from error

    direct_output_by_package = {
        virtual_by_source[result.input_path.resolve()].casefold(): result.output_path
        for result in package_results
    }
    direct_output_by_package.update({
        package_key: result.output_path
        for package_key, result in binary_package_results.items()
    })
    healing_requests: list[HealingUpgradePackagePatch] = []
    healing_virtual_by_input: dict[Path, str] = {}
    clone_work_root = root / "healing-blueprint-clones"
    for package_path, entries in sorted(healing_by_package.items(), key=lambda item: item[0].casefold()):
        if package_path.casefold() in binary_package_results:
            continue
        staged_asset = package_by_virtual.get(package_path.casefold())
        if staged_asset is None:
            raise WorldLootGenerationError(f"Source staging omitted per-instance override map {package_path}.")
        input_asset = direct_output_by_package.get(package_path.casefold(), staged_asset.primary_file.resolve(strict=True))
        output_asset = input_asset.with_name(input_asset.stem + ".cv2-instance-override-patched" + input_asset.suffix)
        instance_patches = []
        for location, expected, assigned in sorted(entries, key=lambda item: item[0].export_index):
            source = location.instance_override_source
            if source is None:
                raise WorldLootGenerationError("World location has no source Blueprint/CDO identity.")
            if source.class_package.casefold() != source.cdo_package.casefold():
                raise WorldLootGenerationError("Override source CDO and Blueprint class must resolve to the same package.")
            blueprint_asset = package_by_virtual.get(source.cdo_package.casefold())
            if blueprint_asset is None:
                raise WorldLootGenerationError(f"Staging omitted override Blueprint/CDO {source.cdo_package}.")
            class_name = PurePosixPath(source.class_package).name + "_C"
            if location.actor_class != class_name:
                raise WorldLootGenerationError("Healing actor class differs from its validated Blueprint source.")
            clone_package_path = _healing_clone_package_path(package_path, location)
            clone_relative = PurePosixPath(clone_package_path.removeprefix("/Game/"))
            clone_output = clone_work_root.joinpath(*clone_relative.parts).with_suffix(".uasset")
            instance_patches.append(HealingUpgradePatch(
                export_index=location.export_index,
                export_name=location.export_name,
                class_package=source.class_package,
                class_name=class_name,
                cdo_export_index=source.cdo_export_index,
                cdo_export_name=source.cdo_export_name,
                blueprint_input=blueprint_asset.primary_file.resolve(strict=True),
                blueprint_output=clone_output,
                clone_package_path=clone_package_path,
                expected_original=expected,
                assigned=assigned,
            ))
        healing_requests.append(HealingUpgradePackagePatch(
            input_asset, output_asset, tuple(instance_patches),
        ))
        healing_virtual_by_input[input_asset.resolve()] = package_path
    try:
        healing_results = package_serializer.patch_healing_upgrades(
            work_root=root,
            mappings_path=mappings,
            packages=healing_requests,
            progress=(lambda current, total: progress("patching-world-loot-instance-overrides", current, total)) if progress else None,
        ) if healing_requests else ()
    except Exception as error:
        if isinstance(error, WorldLootGenerationError):
            raise
        raise WorldLootGenerationError(f"Isolated per-instance Blueprint/CDO patching failed validation: {error}") from error

    final_output_by_package = dict(direct_output_by_package)
    for result in healing_results:
        package_path = healing_virtual_by_input.get(result.input_path.resolve())
        if package_path is None:
            raise WorldLootGenerationError("Healing serializer returned an unrequested package.")
        final_output_by_package[package_path.casefold()] = result.output_path

    direct_results_by_virtual: dict[str, WorldLootPackagePatchResult] = {}
    for result in package_results:
        package_path = virtual_by_source.get(result.input_path.resolve())
        if package_path is None:
            raise WorldLootGenerationError("World Loot serializer returned an unrequested map package.")
        direct_results_by_virtual[package_path.casefold()] = result

    direct_results_by_virtual.update(binary_package_results)

    output_root.mkdir(parents=True, exist_ok=True)
    copy_jobs: list[tuple[Path, Path]] = []
    copied: list[Path] = [
        path for result in binary_package_results.values()
        for path in _package_files(result.output_path) if path.is_file()
    ]
    copied.extend(
        path for result in healing_clone_binary_results.values()
        for path in _package_files(result.output_path) if path.is_file()
    )
    for package_key, output_header in sorted(final_output_by_package.items()):
        if package_key in binary_package_results:
            continue
        package_path = next(
            (path for path in (*by_package.keys(), *healing_by_package.keys()) if path.casefold() == package_key),
            None,
        )
        if package_path is None:
            raise WorldLootGenerationError("Final staged map output lost its virtual package identity.")
        relative = PurePosixPath(*package_path.split("/")[2:])
        destination_dir = output_root.joinpath(*relative.parts[:-1])
        if not _is_within(destination_dir, output_root):
            raise WorldLootGenerationError("Resolved output map path escaped the output Content tree.")
        final_header = destination_dir / (relative.name + output_header.suffix)
        for source_file in _package_files(output_header):
            if source_file.is_file():
                final_file = destination_dir / (relative.name + source_file.suffix)
                copy_jobs.append((source_file, final_file))
        if not any(target == final_header for _, target in copy_jobs):
            raise WorldLootGenerationError("Validated package output has no package header.")

    for result in healing_results:
        for clone in result.clones:
            relative = PurePosixPath(clone.clone_package_path.removeprefix("/Game/"))
            destination_dir = output_root.joinpath(*relative.parts[:-1])
            if not _is_within(destination_dir, output_root):
                raise WorldLootGenerationError("Resolved Healing Blueprint path escaped the output Content tree.")
            final_header = destination_dir / (relative.name + clone.output_path.suffix)
            for source_file in _package_files(clone.output_path):
                if source_file.is_file():
                    final_file = destination_dir / (relative.name + source_file.suffix)
                    copy_jobs.append((source_file, final_file))
            if not any(target == final_header for _, target in copy_jobs):
                raise WorldLootGenerationError("Validated Healing Blueprint clone has no package header.")

    targets = [target for _, target in copy_jobs]
    if len(set(targets)) != len(targets) or any(target.exists() for target in targets):
        raise WorldLootGenerationError("Refusing to overwrite an existing output map or sidecar.")
    try:
        for source_file, target in copy_jobs:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".cv2-tmp")
            try:
                shutil.copyfile(source_file, temporary)
                if _sha256_file(source_file) != _sha256_file(temporary):
                    raise WorldLootGenerationError(f"Copied map package failed integrity validation: {target.name}.")
                temporary.replace(target)
                copied.append(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
    except Exception:
        for target in copied:
            if target.is_file() and _is_within(target, output_root):
                target.unlink()
        raise
    final_package_results: list[WorldLootPackagePatchResult] = []
    for package_key, output_header in sorted(final_output_by_package.items()):
        direct_result = direct_results_by_virtual.get(package_key)
        if direct_result is not None:
            patches = tuple(replace(patch, output_path=output_header) for patch in direct_result.patches)
            final_package_results.append(replace(direct_result, output_path=output_header, patches=patches))
            continue
        staged_asset = package_by_virtual.get(package_key)
        if staged_asset is None:
            raise WorldLootGenerationError("Final package output lost its source identity.")
        final_package_results.append(WorldLootPackagePatchResult(
            staged_asset.primary_file.resolve(strict=True), output_header, (),
        ))

    return WorldLootGenerationResult(
        assignment_count=len(plan.assignments),
        reward_patch_count=changed_reward_count,
        package_results=tuple(final_package_results),
        output_packages=tuple(sorted(copied, key=lambda path: str(path).casefold())),
    )


# Convert article reward
def _to_article_reward(reward: RewardPayload) -> ArticleReward:
    try:
        return ArticleReward(
            reward.article_path,
            reward.count,
            reward.notification,
            reward.soft_object_path_identity,
        )
    except ValueError as error:
        raise WorldLootGenerationError(f"Invalid reward tuple in randomization plan: {error}") from error


# Resolve healing clone package path
def _healing_clone_package_path(package_path: str, location: WorldLootLocation) -> str:
    source = location.instance_override_source
    if source is None:
        raise WorldLootGenerationError("Healing location has no validated Blueprint override source.")
    return (
        f"/Game/CV2Randomizer/Healing/{PurePosixPath(package_path).name}_E{location.export_index}/"
        f"{PurePosixPath(source.class_package).name}"
    )


# Build package files
def _package_files(header: Path) -> tuple[Path, ...]:
    return (header, *(header.with_suffix(extension) for extension in (".uexp", ".ubulk", ".uptnl")))


# Parse SHA-256 file
def _sha256_file(path: Path) -> str:
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
