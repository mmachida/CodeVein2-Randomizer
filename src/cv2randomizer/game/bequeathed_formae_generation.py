"""Extract, validate, plan, and patch direct Bequeathed Forma quest grants."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    IoStoreAssetExtractor,
    MaterializedAsset,
    materialize_extracted_assets,
)
from cv2randomizer.game.bequeathed_formae_catalog import (
    BequeathedForma,
    BequeathedFormaCatalog,
    BequeathedFormaCatalogError,
)
from cv2randomizer.game.blood_code_generation import (
    BloodCodeGenerationError,
    BloodCodeGrantSnapshot,
    BloodCodeSerializer,
)
from cv2randomizer.randomizer.bequeathed_formae import (
    BequeathedFormaPlan,
    BequeathedFormaPlanError,
    build_bequeathed_formae_plan,
)
from cv2randomizer.randomizer.world_loot import WorldLootRandomizationPlan


class BequeathedFormaGenerationError(RuntimeError):
    """Raised when direct Forma quest sources or staged packages fail validation."""


@dataclass(frozen=True, slots=True)
class BequeathedFormaPreparation:
    plan: BequeathedFormaPlan
    catalog: BequeathedFormaCatalog
    source_assets: tuple[MaterializedAsset, ...]
    snapshots: tuple[BloodCodeGrantSnapshot, ...]
    world_loot_alternate_assignments: tuple[tuple[BequeathedForma, str], ...] = ()


@dataclass(frozen=True, slots=True)
class BequeathedFormaGenerationResult:
    plan: BequeathedFormaPlan
    output_files: tuple[Path, ...]
    patched_packages: tuple[str, ...]
    reopened: bool


# Prepare bequeathed formae plan
def prepare_bequeathed_formae_plan(
    catalog: BequeathedFormaCatalog,
    seed: str | int,
    *,
    game_root: Path,
    work_root: Path,
    mappings_path: Path,
    game_version: str,
    data_revision: str,
    extractor: IoStoreAssetExtractor,
    serializer: BloodCodeSerializer,
    world_loot_plan: WorldLootRandomizationPlan | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> BequeathedFormaPreparation:
    root = work_root.expanduser().resolve()
    mappings = mappings_path.expanduser().resolve(strict=True)
    plan = build_bequeathed_formae_plan(catalog, seed)
    world_assignments_by_id = {
        str(assignment.destination.destination_id): assignment
        for assignment in (world_loot_plan.assignments if world_loot_plan is not None else ())
        if assignment.destination.destination_id is not None
    }
    world_alternates: list[tuple[BequeathedForma, str]] = []
    for item in catalog.world_loot_direct_rewards:
        assignment = world_assignments_by_id.get(str(item.world_loot_destination_id))
        if assignment is None:
            continue
        if len(assignment.assigned_rewards) != 1:
            raise BequeathedFormaGenerationError(
                f"World Loot Bequeathed Forma {item.object} must remain a single Article reward."
            )
        assigned_path = assignment.assigned_rewards[0].article_path
        if (
            len(item.direct_grants) > 1
            and assigned_path.casefold() != item.asset_path.casefold()
        ):
            world_alternates.append((item, assigned_path))
    if not plan.assignments and not world_alternates:
        return BequeathedFormaPreparation(plan, catalog, (), ())
    prepared_items = {
        item.object.casefold(): item
        for item in (
            *(assignment.destination for assignment in plan.assignments),
            *(item for item, _assigned in world_alternates),
        )
    }
    packages = tuple(sorted({
        grant.quest_asset
        for item in prepared_items.values()
        for grant in item.direct_grants
    }, key=str.casefold))
    try:
        if not packages:
            raise BequeathedFormaGenerationError("Bequeathed Forma assignments have no physical quest grant packages.")
        if progress:
            progress("extracting-bequeathed-formae", 0, len(packages))
        extracted = extractor.extract_assets(
            game_root,
            packages,
            game_version=game_version,
            data_revision=data_revision,
            progress=progress,
        )
        by_virtual = {asset.virtual_path.casefold(): asset for asset in extracted}
        expected_packages = {package.casefold() for package in packages}
        if len(by_virtual) != len(packages) or set(by_virtual) != expected_packages:
            raise BequeathedFormaGenerationError(
                "Asset extraction returned missing, duplicate, or unexpected Bequeathed Forma quest packages."
            )
        source_assets = materialize_extracted_assets(
            extracted,
            root / "source" / "bequeathed-formae" / "Content",
        )
        assets_by_virtual = {asset.virtual_path.casefold(): asset for asset in source_assets}
        files = {package: assets_by_virtual[package.casefold()].primary_file for package in packages}
        snapshots = serializer.inspect_bequeathed_formae_packages(
            work_root=root,
            mappings_path=mappings,
            package_files=files,
        )
        _validate_snapshots(tuple(prepared_items.values()), snapshots)
        if progress:
            progress("inspecting-bequeathed-formae", len(packages), len(packages))
        return BequeathedFormaPreparation(
            plan, catalog, tuple(assets_by_virtual[p.casefold()] for p in packages), snapshots,
            tuple(sorted(world_alternates, key=lambda pair: pair[0].object.casefold())),
        )
    except (AssetExtractionError, BloodCodeGenerationError, BequeathedFormaCatalogError, BequeathedFormaPlanError,
            OSError, ValueError, KeyError) as error:
        if isinstance(error, BequeathedFormaGenerationError):
            raise
        raise BequeathedFormaGenerationError(f"Could not prepare direct Bequeathed Forma grants: {error}") from error


# Apply bequeathed formae plan
def apply_bequeathed_formae_plan(
    preparation: BequeathedFormaPreparation,
    *,
    work_root: Path,
    output_content_root: Path,
    mappings_path: Path,
    serializer: BloodCodeSerializer,
    progress: Callable[[str, int, int], None] | None = None,
) -> BequeathedFormaGenerationResult:
    root = work_root.expanduser().resolve(strict=True)
    output_root = output_content_root.expanduser().resolve()
    source_root = (root / "source" / "bequeathed-formae" / "Content").resolve()
    if not _is_within(output_root, root) or output_root == root or output_root == source_root:
        raise BequeathedFormaGenerationError("Bequeathed Forma output must be a separate Content tree inside the workspace.")
    output_root.mkdir(parents=True, exist_ok=True)
    source_by_package = {asset.virtual_path.casefold(): asset for asset in preparation.source_assets}
    assignments_by_package: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assignment in preparation.plan.assignments:
        if assignment.destination.asset_path.casefold() == assignment.source.asset_path.casefold():
            continue
        for grant in assignment.destination.direct_grants:
            snapshot = next((
                item for item in preparation.snapshots
                if item.virtual_package.casefold() == grant.quest_asset.casefold()
                and item.export_index == grant.export_index
                and item.bulk_index == 0
                and item.reward_index == 0
            ), None)
            if snapshot is None:
                raise BequeathedFormaGenerationError(
                    f"No validated GainItem export snapshot is available for {grant.quest_asset}."
                )
            assignments_by_package[grant.quest_asset].append({
                "exportIndex": grant.export_index,
                "exportName": snapshot.export_name,
                "bulkIndex": 0,
                "rewardIndex": 0,
                "expectedPackage": assignment.destination.asset_path,
                "expectedAsset": assignment.destination.object,
                "expectedCount": grant.count,
                "expectedNotification": grant.notification,
                "assignedPackage": assignment.source.asset_path,
                "assignedAsset": assignment.source.object,
            })

    snapshots_by_slot = {
        (snapshot.virtual_package.casefold(), snapshot.export_index, snapshot.bulk_index, snapshot.reward_index): snapshot
        for snapshot in preparation.snapshots
    }
    for item, assigned_path in preparation.world_loot_alternate_assignments:
        if len(item.direct_grants) < 2:
            raise BequeathedFormaGenerationError(
                f"World Loot Forma {item.object} has no alternate quest grant to mirror."
            )
        assigned_asset = assigned_path.rsplit("/", 1)[-1]
        for grant in item.direct_grants[1:]:
            snapshot = snapshots_by_slot.get((grant.quest_asset.casefold(), grant.export_index, 0, 0))
            if snapshot is None:
                raise BequeathedFormaGenerationError(
                    f"No validated alternate GainItem snapshot is available for {grant.quest_asset}#{grant.export_index}."
                )
            assignments_by_package.setdefault(grant.quest_asset, []).append({
                "exportIndex": grant.export_index,
                "exportName": snapshot.export_name,
                "bulkIndex": 0,
                "rewardIndex": 0,
                "expectedPackage": item.asset_path,
                "expectedAsset": item.object,
                "expectedCount": grant.count,
                "expectedNotification": grant.notification,
                "assignedPackage": assigned_path,
                "assignedAsset": assigned_asset,
            })

    requests: dict[str, tuple[Path, Path, tuple[dict[str, Any], ...]]] = {}
    owned_headers: list[Path] = []
    committed: list[Path] = []
    try:
        for virtual, assignments in sorted(assignments_by_package.items(), key=lambda item: item[0].casefold()):
            source_asset = source_by_package.get(virtual.casefold())
            if source_asset is None:
                raise BequeathedFormaGenerationError(f"No extracted source asset is available for {virtual}.")
            source = source_asset.primary_file.resolve(strict=True)
            output = output_root.joinpath(*virtual.split("/")[2:]).with_suffix(source.suffix)
            output.parent.mkdir(parents=True, exist_ok=True)
            if any(path.exists() for path in _package_files(output)):
                raise BequeathedFormaGenerationError(f"Refusing to overwrite staged Bequeathed Forma package {output.name}.")
            owned_headers.append(output)
            requests[virtual] = (source, output, tuple(assignments))

        if requests:
            if progress:
                progress("patching-bequeathed-formae", 0, len(requests))
            # A World Loot destination can receive any Article identity, not only
            # another Gadget/Bequeathed Forma. The source reward is still
            # validated as a Forma during inspection; use the generic Article
            # quest serializer for the randomized destination path.
            raw_results = serializer.patch_world_loot_quest_packages(
                work_root=root,
                mappings_path=mappings_path,
                patch_packages=requests,
            )
            by_package = {item.get("virtualPackage"): item for item in raw_results if isinstance(item, dict)}
            if set(by_package) != set(requests):
                raise BequeathedFormaGenerationError("UAssetAPI patched unexpected Bequeathed Forma quest packages.")
            for index, (virtual, (source, output, assignments)) in enumerate(requests.items(), start=1):
                result = by_package[virtual]
                if (
                    Path(str(result.get("inputFile", ""))).resolve() != source
                    or Path(str(result.get("outputFile", ""))).resolve() != output
                    or result.get("patchedSlots") != len(assignments)
                    or result.get("virtualPackage") != virtual
                ):
                    raise BequeathedFormaGenerationError(f"UAssetAPI returned invalid patch results for {virtual}.")
                output_paths = result.get("outputFiles")
                if not isinstance(output_paths, list):
                    raise BequeathedFormaGenerationError(f"UAssetAPI omitted the output sidecar inventory for {virtual}.")
                package_files = tuple(Path(str(path)).resolve(strict=True) for path in output_paths)
                if output.resolve() not in package_files or any(
                    not _is_within(path, output_root)
                    or path.stem.casefold() != output.stem.casefold()
                    or path.suffix.casefold() not in {".uasset", ".uexp", ".ubulk", ".uptnl"}
                    for path in package_files
                ):
                    raise BequeathedFormaGenerationError(f"UAssetAPI returned an invalid output inventory for {virtual}.")
                if any(path.suffix.casefold() == ".uexp" for path in source_asset.files) and not any(
                    path.suffix.casefold() == ".uexp" for path in package_files
                ):
                    raise BequeathedFormaGenerationError(f"Patched Bequeathed Forma package {virtual} lost its .uexp sidecar.")
                committed.extend(package_files)
                if progress:
                    progress("patching-bequeathed-formae", index, len(requests))

        if len(committed) != len(set(committed)):
            raise BequeathedFormaGenerationError("UAssetAPI returned duplicate Bequeathed Forma output files.")
        changed_packages = tuple(sorted(requests, key=str.casefold))
        return BequeathedFormaGenerationResult(
            preparation.plan,
            tuple(sorted(committed, key=lambda path: str(path).casefold())),
            changed_packages,
            True,
        )
    except Exception as error:
        cleanup = set(committed)
        for header in owned_headers:
            cleanup.update(path for path in _package_files(header) if path.exists())
        for path in cleanup:
            if _is_within(path, output_root):
                path.unlink(missing_ok=True)
        if isinstance(error, BequeathedFormaGenerationError):
            raise
        raise BequeathedFormaGenerationError(f"Could not apply the Bequeathed Forma plan: {error}") from error


# Check staged package snapshots
def _validate_snapshots(
    items: tuple[BequeathedForma, ...],
    snapshots: tuple[BloodCodeGrantSnapshot, ...],
) -> None:
    expected: dict[tuple[str, int, int, int], tuple[str, int, bool]] = {}
    for item in items:
        for grant in item.direct_grants:
            key = (grant.quest_asset.casefold(), grant.export_index, 0, 0)
            expected[key] = (item.asset_path, grant.count, grant.notification)
    observed = {
        (snapshot.virtual_package.casefold(), snapshot.export_index, snapshot.bulk_index, snapshot.reward_index): snapshot
        for snapshot in snapshots
    }
    if len(observed) != len(snapshots) or not set(expected).issubset(observed):
        raise BequeathedFormaGenerationError("Extracted GainItem exports do not match the requested Forma grant locations.")
    for key, (asset_path, count, notification) in expected.items():
        snapshot = observed[key]
        expected_asset = asset_path.rsplit("/", 1)[-1]
        if (
            "gainitem" not in snapshot.export_name.replace("_", "").casefold()
            or snapshot.reward_package.casefold() != asset_path.casefold()
            or snapshot.reward_asset.casefold() != expected_asset.casefold()
            or snapshot.count != count
            or snapshot.notification is not notification
        ):
            raise BequeathedFormaGenerationError(
                f"Extracted quest reward differs from the canonical source at {snapshot.virtual_package}#{snapshot.export_index}."
            )


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
