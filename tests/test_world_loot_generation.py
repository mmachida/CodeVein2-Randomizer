from __future__ import annotations

import tempfile
import unittest
import hashlib
import struct
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from cv2randomizer.game.asset_extraction import ExtractedAsset, MaterializedAsset
from cv2randomizer.game.catalogs import RewardPayload, WorldLootInstanceOverrideSource, WorldLootLocation
from cv2randomizer.game.fname_identity import SoftObjectPathIdentity
from cv2randomizer.game.world_loot_generation import apply_world_loot_plan
from cv2randomizer.game.world_loot_serialization import (
    HealingUpgradeCloneResult,
    HealingUpgradePackagePatchResult,
    WorldLootPackagePatchResult,
)
from cv2randomizer.randomizer.world_loot import WorldLootAssignment, WorldLootRandomizationPlan


class FakeResolver:
    # Stage for locations
    def stage_for_locations(self, *, game_root: Path, locations: object, work_root: Path, mappings_path: Path,
                            game_version: str, data_revision: str, progress: object = None) -> object:
        content = work_root / "source" / "Content"
        relative = Path("Map") / "Generated" / "Map001"
        directory = content / relative.parent
        directory.mkdir(parents=True, exist_ok=True)
        header = directory / "Map001.umap"
        sidecar = directory / "Map001.uexp"
        header.write_bytes(b"original map")
        sidecar.write_bytes(b"original exports")
        asset = MaterializedAsset("/Game/Map/Generated/Map001", (header, sidecar))
        return SimpleNamespace(maps=(asset,), content_root=content)


class FakeSerializer:
    # Initialize this object
    def __init__(self) -> None:
        self.requested_patches = ()
        self.requested_packages = ()
        self.requested_healing = ()

    # Patch package reward data
    def patch_packages(self, *, work_root: Path, mappings_path: Path, packages: object, progress: object = None) -> tuple[WorldLootPackagePatchResult, ...]:
        packages = tuple(packages)
        self.requested_packages = packages
        self.requested_patches = tuple(patch for package in packages for patch in package.patches)
        results = []
        for package in packages:
            package.output_asset.write_bytes(b"validated patched map")
            package.output_asset.with_suffix(".uexp").write_bytes(b"validated exports")
            results.append(WorldLootPackagePatchResult(package.input_asset, package.output_asset, ()))
        return tuple(results)

    # Patch healing upgrades
    def patch_healing_upgrades(self, *, work_root: Path, mappings_path: Path, packages: object,
                               progress: object = None) -> tuple[HealingUpgradePackagePatchResult, ...]:
        self.requested_healing = tuple(packages)
        results = []
        for package in self.requested_healing:
            package.output_asset.parent.mkdir(parents=True, exist_ok=True)
            package.output_asset.write_bytes(b"validated healing map")
            package.output_asset.with_suffix(".uexp").write_bytes(b"preserved map exports")
            clones = []
            for patch in package.patches:
                patch.blueprint_output.parent.mkdir(parents=True, exist_ok=True)
                patch.blueprint_output.write_bytes(b"validated isolated Blueprint")
                patch.blueprint_output.with_suffix(".uexp").write_bytes(b"patched CDO payload")
                clones.append(HealingUpgradeCloneResult(
                    patch.blueprint_input, patch.blueprint_output, patch.clone_package_path,
                    patch.expected_original, patch.assigned, True, True,
                ))
            results.append(HealingUpgradePackagePatchResult(
                package.input_asset, package.output_asset, len(package.patches), tuple(clones), True, True,
            ))
        return tuple(results)


class FakeAssetExtractor:
    # Initialize this object
    def __init__(self, root: Path) -> None:
        self.root = root
        self.requested: tuple[str, ...] = ()

    # Extract requested game assets
    def extract_assets(self, game_root: Path, virtual_paths: object, **kwargs: object) -> tuple[ExtractedAsset, ...]:
        self.requested = tuple(virtual_paths)
        assets = []
        for virtual_path in self.requested:
            relative = Path(*virtual_path.split("/")[2:])
            header = self.root / "extracted" / relative.with_suffix(".uasset")
            sidecar = header.with_suffix(".uexp")
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_bytes((virtual_path + " header").encode())
            sidecar.write_bytes((virtual_path + " exports").encode())
            assets.append(ExtractedAsset(virtual_path, (header, sidecar)))
        return tuple(assets)


# Build a test location record
def _location(name: str, path: str) -> WorldLootLocation:
    return WorldLootLocation(
        "/Game/Map/Generated/Map001", "Map001.umap", 8, name, "BP_Pickup_C", "GROUND_PICKUP",
        name, False, (), (RewardPayload(path, 1, True),),
    )


# Find CDO location
def _cdo_location(name: str, path: str) -> WorldLootLocation:
    return WorldLootLocation(
        path, path.removeprefix("/Game/") + ".uasset", 3, name, "BP_it23_001_A_0_C",
        "GROUND_PICKUP", None, False, (), (RewardPayload("/Game/Item/DA_Item_Source", 1, True),),
        "BLUEPRINT_CDO_ARTICLE_BULK", "world_loot_cdo:" + path.casefold(), "CDO source",
        None, None, "BASE", 30,
        "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_ItemIngredient",
        (
            "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_ItemIngredient",
            "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base",
        ),
    )


class WorldLootGenerationTests(unittest.TestCase):
    # Check prepared healing templates binary patch
    def test_prepared_healing_templates_binary_patch_without_structural_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"map")
            work = root / "work"
            cache_root = root / "prepared-cache"
            map_path = "/Game/Map/Generated/Map001"
            class_path = "/Game/Environment/Item/it10/it10_002/BP_it10_002_B_0"
            clone_path = "/Game/CV2Randomizer/Healing/Map001_E8/BP_it10_002_B_0"
            vanilla_path = "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryCountUp_1"
            assigned_path = "/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight_1"
            vanilla_identity = SoftObjectPathIdentity.from_literal_path(vanilla_path)
            assigned_identity = SoftObjectPathIdentity.from_literal_path(assigned_path)
            location = replace(
                _location("BP_it10_002_B_0_C_UAID_healing", vanilla_path),
                rewards=(RewardPayload(vanilla_path, 1, True, soft_object_path_identity=vanilla_identity),),
                destination_kind="INSTANCE_ARTICLE_BULK_OVERRIDE",
                actor_class="BP_it10_002_B_0_C",
                destination_id=map_path + "#8",
                location_type="HEALING_UPGRADE_GOLDEN_SEED",
                instance_override_source=WorldLootInstanceOverrideSource(
                    class_path, 2, "Default__BP_it10_002_B_0_C", class_path, (),
                ),
            )
            source = replace(
                _location("world-source", assigned_path),
                rewards=(RewardPayload(assigned_path, 1, True, soft_object_path_identity=assigned_identity),),
            )
            plan = WorldLootRandomizationPlan((WorldLootAssignment(location, source),), (), (), ())
            map_header = cache_root / "prepared-assets" / "Map" / "Generated" / "Map001.umap"
            map_uexp = map_header.with_suffix(".uexp")
            map_header.parent.mkdir(parents=True)
            map_header.write_bytes(b"prepared map header")
            map_uexp.write_bytes(b"prepared map exports")
            clone_header = cache_root / "healing-prepared-assets" / "CV2Randomizer" / "Healing" / "Map001_E8" / "BP_it10_002_B_0.uasset"
            clone_uexp = clone_header.with_suffix(".uexp")
            clone_header.parent.mkdir(parents=True)
            clone_header.write_bytes(b"prepared clone header")
            expected_bytes = struct.pack(
                "<iiii", 10, vanilla_identity.package_name.number,
                11, vanilla_identity.asset_name.number,
            )
            clone_uexp.write_bytes(expected_bytes + b"clone export bytes")
            name_indices = {
                assigned_identity.package_name.value: 20,
                assigned_identity.asset_name.value: 21,
            }

            class PreparedExtractor:
                # Extract prepared assets with descriptors
                def extract_prepared_assets_with_descriptors(self, _game_root, descriptors, **_kwargs):
                    assets = []
                    for package in descriptors:
                        if package.casefold() == map_path.casefold():
                            files = (map_header, map_uexp)
                        else:
                            files = (clone_header, clone_uexp)
                        assets.append(ExtractedAsset(package, files))
                    return tuple(assets)

                # Extract requested game assets
                def extract_assets(self, *_args, **_kwargs):
                    raise AssertionError("prepared Healing binary path should not extract raw packages")

            # Read digest
            def digest(path: Path) -> str:
                return hashlib.sha256(path.read_bytes()).hexdigest()

            map_descriptor = {
                "version": 3, "package_path": map_path,
                "header_sha256": digest(map_header), "uexp_sha256": digest(map_uexp),
                "header_size": map_header.stat().st_size, "uexp_size": map_uexp.stat().st_size,
                "package_file_hashes": {".umap": digest(map_header), ".uexp": digest(map_uexp)},
                "name_map_indices": {}, "fields": [],
            }
            clone_descriptor = {
                "version": 3, "package_path": clone_path,
                "header_sha256": digest(clone_header), "uexp_sha256": digest(clone_uexp),
                "header_size": clone_header.stat().st_size, "uexp_size": clone_uexp.stat().st_size,
                "package_file_hashes": {".uasset": digest(clone_header), ".uexp": digest(clone_uexp)},
                "name_map_indices": name_indices,
                "fields": [{
                    "export_index": 2, "reward_index": 0,
                    "export_name": "Default__BP_it10_002_B_0_C",
                    "offset": 0, "expected_bytes_hex": expected_bytes.hex(),
                    "expected_identity": {
                        "package_name": {"value": vanilla_identity.package_name.value, "number": vanilla_identity.package_name.number},
                        "asset_name": {"value": vanilla_identity.asset_name.value, "number": vanilla_identity.asset_name.number},
                    },
                    "available": True, "unavailable_reason": None,
                }],
            }
            result = apply_world_loot_plan(
                plan,
                game_root=root / "game",
                work_root=work,
                output_content_root=work / "output" / "Content",
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=FakeResolver(),
                extractor=PreparedExtractor(),
                dependency_index={
                    "world_loot_binary_patch_packages": {},
                    "healing_prepared_maps": {map_path.casefold(): map_descriptor},
                    "healing_prepared_clones": {clone_path.casefold(): clone_descriptor},
                },
                serializer=FakeSerializer(),
            )
            generated_clone = work / "output" / "Content" / "CV2Randomizer" / "Healing" / "Map001_E8" / "BP_it10_002_B_0.uasset"
            self.assertTrue((work / "output" / "Content" / "Map" / "Generated" / "Map001.umap").is_file())
            self.assertEqual(generated_clone.with_suffix(".uexp").read_bytes()[:16], struct.pack(
                "<iiii", name_indices[assigned_identity.package_name.value], assigned_identity.package_name.number,
                name_indices[assigned_identity.asset_name.value], assigned_identity.asset_name.number,
            ))
            self.assertEqual(len(result.package_results), 1)
            self.assertEqual({path.suffix for path in result.output_packages}, {".umap", ".uexp", ".uasset"})

    # Check missing namemap name skips binary
    def test_missing_namemap_name_skips_binary_package_read_and_uses_structural_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"map")
            expected_path = "/Game/Blueprint/Article/Item/DA_Item_A"
            assigned_path = "/Game/Blueprint/Article/Item/DA_Item_B"
            expected_identity = SoftObjectPathIdentity.from_literal_path(expected_path)
            assigned_identity = SoftObjectPathIdentity.from_literal_path(assigned_path)
            destination = replace(
                _location("BP_Pickup_C_UAID_fallback", expected_path),
                rewards=(RewardPayload(expected_path, 1, True, soft_object_path_identity=expected_identity),),
            )
            source = replace(
                _location("source", assigned_path),
                rewards=(RewardPayload(assigned_path, 1, True, soft_object_path_identity=assigned_identity),),
            )
            plan = WorldLootRandomizationPlan((WorldLootAssignment(destination, source),), (), (), ())
            package = destination.package_path

            class NoReadExtractor:
                # Extract requested game assets
                def extract_assets(self, *_args, **_kwargs):
                    raise AssertionError("NameMap-ineligible package must not be read for binary preflight")

            descriptor = {
                "version": 1,
                "package_path": package,
                "name_map_indices": {
                    expected_identity.package_name.value: 2,
                    expected_identity.asset_name.value: 3,
                },
                "fields": [{
                    "export_index": destination.export_index,
                    "reward_index": 0,
                    "export_name": destination.export_name,
                    "expected_identity": {
                        "package_name": {
                            "value": expected_identity.package_name.value,
                            "number": expected_identity.package_name.number,
                        },
                        "asset_name": {
                            "value": expected_identity.asset_name.value,
                            "number": expected_identity.asset_name.number,
                        },
                    },
                }],
            }
            serializer = FakeSerializer()
            result = apply_world_loot_plan(
                plan,
                game_root=root / "game",
                work_root=root / "work",
                output_content_root=root / "work" / "output" / "Content",
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=FakeResolver(),
                extractor=NoReadExtractor(),
                dependency_index={
                    "world_loot_binary_patch_packages": {package.casefold(): descriptor},
                },
                serializer=serializer,
            )

            self.assertEqual(len(serializer.requested_packages), 1)
            self.assertEqual(serializer.requested_patches[0].assigned.article_data_path, assigned_path)
            self.assertTrue(result.output_packages)

    # Check safe binary map patch skips
    def test_safe_binary_map_patch_skips_structural_resolver_and_serializer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"map")
            expected_path = "/Game/Blueprint/Article/Item/DA_Item_A"
            assigned_path = "/Game/Blueprint/Article/Item/DA_Item_B"
            expected_identity = SoftObjectPathIdentity.from_literal_path(expected_path)
            assigned_identity = SoftObjectPathIdentity.from_literal_path(assigned_path)
            destination = replace(
                _location("BP_Pickup_C_UAID_binary", expected_path),
                rewards=(RewardPayload(expected_path, 1, True, soft_object_path_identity=expected_identity),),
            )
            source = replace(
                _location("source", assigned_path),
                rewards=(RewardPayload(assigned_path, 1, True, soft_object_path_identity=assigned_identity),),
            )
            plan = WorldLootRandomizationPlan((WorldLootAssignment(destination, source),), (), (), ())
            package = destination.package_path
            package_root = root / "cache" / "Map" / "Generated"
            package_root.mkdir(parents=True)
            header = package_root / "Map001.umap"
            sidecar = package_root / "Map001.uexp"
            header.write_bytes(b"known immutable map header")
            expected_bytes = struct.pack("<iiii", 2, 0, 3, 0)
            sidecar.write_bytes(expected_bytes + b"unchanged export payload")

            class BinaryExtractor:
                # Extract prepared assets with descriptors
                def extract_prepared_assets_with_descriptors(self, _game_root, descriptors, **_kwargs):
                    self.requested = tuple(descriptors)
                    self.requested_roots = tuple(
                        descriptor["prepared_asset"]["root_name"]
                        for descriptor in descriptors.values()
                    )
                    return (ExtractedAsset(package, (header, sidecar)),)

                # Extract requested game assets
                def extract_assets(self, *_args, **_kwargs):
                    raise AssertionError("prepared Healing map baseline should not read raw map packages")

            extractor = BinaryExtractor()
            serializer = FakeSerializer()
            resolver = SimpleNamespace(extractor=extractor)
            descriptor = {
                "version": 3,
                "package_path": package,
                "header_sha256": hashlib.sha256(header.read_bytes()).hexdigest(),
                "uexp_sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                "header_size": header.stat().st_size,
                "uexp_size": sidecar.stat().st_size,
                "package_file_hashes": {
                    ".umap": hashlib.sha256(header.read_bytes()).hexdigest(),
                    ".uexp": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                },
                "name_map_indices": {
                    expected_identity.package_name.value: 2,
                    expected_identity.asset_name.value: 3,
                    assigned_identity.package_name.value: 7,
                    assigned_identity.asset_name.value: 9,
                },
                "name_map_count_before": 10,
                "name_map_count_after": 12,
                "names_added_count": 2,
                "prepared_asset": {
                    "root_name": "healing-prepared-assets",
                    "relative_path": "Map/Generated/Map001",
                    "files": [
                        {"name": "Map001.umap", "size": header.stat().st_size,
                         "sha256": hashlib.sha256(header.read_bytes()).hexdigest()},
                        {"name": "Map001.uexp", "size": sidecar.stat().st_size,
                         "sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest()},
                    ],
                },
                "fields": [{
                    "export_index": destination.export_index,
                    "reward_index": 0,
                    "export_name": destination.export_name,
                    "offset": 0,
                    "expected_bytes_hex": expected_bytes.hex(),
                    "expected_identity": {
                        "package_name": {
                            "value": expected_identity.package_name.value,
                            "number": expected_identity.package_name.number,
                        },
                        "asset_name": {
                            "value": expected_identity.asset_name.value,
                            "number": expected_identity.asset_name.number,
                        },
                    },
                }],
            }
            output_root = root / "work" / "output" / "Content"
            result = apply_world_loot_plan(
                plan,
                game_root=root / "game",
                work_root=root / "work",
                output_content_root=output_root,
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=resolver,
                dependency_index={
                    "world_loot_binary_patch_packages": {},
                    "healing_prepared_maps": {package.casefold(): descriptor},
                },
                serializer=serializer,
            )

            self.assertEqual(extractor.requested, (package.casefold(),))
            self.assertEqual(extractor.requested_roots, ("healing-prepared-assets",))
            self.assertEqual(serializer.requested_packages, ())
            self.assertEqual(serializer.requested_healing, ())
            output_header = output_root / "Map" / "Generated" / "Map001.umap"
            output_bytes = output_header.with_suffix(".uexp").read_bytes()
            self.assertEqual(output_bytes[:16], struct.pack("<iiii", 7, assigned_identity.package_name.number, 9, assigned_identity.asset_name.number))
            self.assertEqual(output_bytes[16:], sidecar.read_bytes()[16:])
            self.assertEqual(set(result.output_packages), {output_header, output_header.with_suffix(".uexp")})
            self.assertEqual(result.package_results[0].patches[0].assigned.article_data_path,
                             source.rewards[0].article_path)

    # Check CDO patch stages owner complete
    def test_cdo_patch_stages_owner_and_complete_parent_chain_without_save_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"map")
            path = "/Game/Environment/Item/it23/it23_001/BP_it23_001_A_0"
            destination = _cdo_location("Default__BP_it23_001_A_0_C", path)
            source = replace(
                destination,
                package_path="/Game/Environment/Item/it23/it23_001/BP_it23_002_A_0",
                destination_id="world_loot_cdo:/game/environment/item/it23/it23_001/bp_it23_002_a_0",
                rewards=(RewardPayload("/Game/Item/DA_Item_Assigned", 1, True),),
            )
            plan = WorldLootRandomizationPlan((WorldLootAssignment(destination, source),), (), (), ())
            extractor = FakeAssetExtractor(root / "assets")
            resolver = SimpleNamespace(extractor=extractor)
            serializer = FakeSerializer()

            result = apply_world_loot_plan(
                plan,
                game_root=root / "game",
                work_root=root / "work",
                output_content_root=root / "work" / "output" / "Content",
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=resolver,
                serializer=serializer,
            )

            self.assertEqual(set(extractor.requested), {
                path,
                "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_ItemIngredient",
                "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base",
            })
            self.assertEqual(len(serializer.requested_packages), 1)
            self.assertEqual(serializer.requested_packages[0].input_asset.suffix, ".uasset")
            self.assertFalse(serializer.requested_patches[0].require_unique_save_id)
            self.assertEqual(result.output_packages[0].suffix, ".uasset")
            self.assertTrue(result.output_packages[0].with_suffix(".uexp").is_file())

    # Check healing upgrade use unique blueprint
    def test_healing_upgrade_uses_unique_blueprint_cdo_clone_per_physical_actor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"map")
            destination = replace(
                _location("BP_it10_002_B_0_C_UAID_physical", "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryCountUp_1"),
                destination_kind="INSTANCE_ARTICLE_BULK_OVERRIDE",
                actor_class="BP_it10_002_B_0_C",
                destination_id="/Game/Map/Generated/Map001#8",
                display_name="Golden Seed — map #8",
                location_type="HEALING_UPGRADE_GOLDEN_SEED",
                structural_class_package="/Game/Environment/Item/it10/it10_002/BP_it10_002_B_0",
                instance_override_source=WorldLootInstanceOverrideSource(
                    "/Game/Environment/Item/it10/it10_002/BP_it10_002_B_0", 2,
                    "Default__BP_it10_002_B_0_C",
                    "/Game/Environment/Item/it10/it10_002/BP_it10_002_B_0",
                    ("/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_RecoveryCountUp_Base",),
                ),
            )
            source = _location("source", "/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight_1")
            plan = WorldLootRandomizationPlan((WorldLootAssignment(destination, source),), (), (), ())
            serializer = FakeSerializer()
            output_root = root / "work" / "output" / "Content"
            result = apply_world_loot_plan(
                plan,
                game_root=root / "game",
                work_root=root / "work",
                output_content_root=output_root,
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=FakeResolver(),
                extractor=FakeAssetExtractor(root / "cache"),
                serializer=serializer,
            )
            self.assertEqual(len(serializer.requested_healing), 1)
            patch = serializer.requested_healing[0].patches[0]
            self.assertEqual(patch.export_index, destination.export_index)
            self.assertEqual(patch.expected_original.article_data_path, destination.rewards[0].article_path)
            self.assertEqual(patch.assigned.article_data_path, "/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight_1")
            self.assertTrue(patch.blueprint_input.is_file())
            self.assertEqual(patch.clone_package_path, "/Game/CV2Randomizer/Healing/Map001_E8/BP_it10_002_B_0")
            self.assertTrue(patch.blueprint_output.is_file())
            self.assertEqual({path.suffix for path in result.output_packages}, {".umap", ".uexp", ".uasset"})
            self.assertTrue((output_root / "Map" / "Generated" / "Map001.umap").is_file())
            self.assertTrue((output_root / "CV2Randomizer" / "Healing" / "Map001_E8" / "BP_it10_002_B_0.uasset").is_file())
            self.assertFalse((output_root / "Environment" / "Item" / "it10" / "it10_002" / "BP_it10_002_B_0.uasset").exists())

    # Check healing clone chains after direct
    def test_healing_clone_chains_after_direct_world_loot_patch_on_same_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"map")
            healing = replace(
                _location("BP_it10_002_B_0_C_UAID_physical", "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryCountUp_1"),
                destination_kind="INSTANCE_ARTICLE_BULK_OVERRIDE",
                actor_class="BP_it10_002_B_0_C",
                destination_id="/Game/Map/Generated/Map001#8",
                display_name="Golden Seed — map #8",
                location_type="HEALING_UPGRADE_GOLDEN_SEED",
                structural_class_package="/Game/Environment/Item/it10/it10_002/BP_it10_002_B_0",
                instance_override_source=WorldLootInstanceOverrideSource(
                    "/Game/Environment/Item/it10/it10_002/BP_it10_002_B_0", 2,
                    "Default__BP_it10_002_B_0_C",
                    "/Game/Environment/Item/it10/it10_002/BP_it10_002_B_0",
                    ("/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_RecoveryCountUp_Base",),
                ),
            )
            ordinary = _location("BP_Pickup_C_UAID_direct", "/Game/Item/DA_Item_A")
            source = _location("source", "/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight_1")
            plan = WorldLootRandomizationPlan((
                WorldLootAssignment(ordinary, source), WorldLootAssignment(healing, source),
            ), (), (), ())
            serializer = FakeSerializer()
            result = apply_world_loot_plan(
                plan,
                game_root=root / "game",
                work_root=root / "work",
                output_content_root=root / "work" / "output" / "Content",
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=FakeResolver(),
                extractor=FakeAssetExtractor(root / "cache"),
                serializer=serializer,
            )
            self.assertEqual(len(serializer.requested_packages), 1)
            self.assertEqual(len(serializer.requested_healing), 1)
            chained_map = serializer.requested_healing[0].input_asset
            self.assertEqual(chained_map.name, "Map001.cv2-patched.umap")
            self.assertEqual(len(result.package_results), 1)
            self.assertEqual(len(serializer.requested_patches), 1)
            self.assertTrue(result.package_results[0].output_path.name.endswith("cv2-instance-override-patched.umap"))

    # Check applies verified package outputs separate
    def test_applies_verified_package_outputs_to_separate_content_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"map")
            destination = _location("BP_Pickup_C_UAID_dest", "/Game/Item/DA_Item_A")
            source = _location("BP_Pickup_C_UAID_source", "/Game/Item/DA_Item_B")
            plan = WorldLootRandomizationPlan((WorldLootAssignment(destination, source),), (), (), ())
            output_root = root / "work" / "output" / "Content"
            result = apply_world_loot_plan(
                plan,
                game_root=root / "game",
                work_root=root / "work",
                output_content_root=output_root,
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=FakeResolver(),
                serializer=FakeSerializer(),
            )
            output_map = output_root / "Map" / "Generated" / "Map001.umap"
            self.assertEqual(result.assignment_count, 1)
            self.assertEqual(result.reward_patch_count, 1)
            self.assertEqual({path.name for path in result.output_packages}, {"Map001.umap", "Map001.uexp"})
            self.assertEqual(output_map.read_bytes(), b"validated patched map")
            self.assertEqual((output_map.parent / "Map001.uexp").read_bytes(), b"validated exports")
            self.assertEqual((root / "work" / "source" / "Content" / "Map" / "Generated" / "Map001.umap").read_bytes(), b"original map")

    # Check identical reward payloads do not
    def test_identical_reward_payloads_do_not_create_a_package_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"map")
            same = "/Game/Item/DA_Item_A"
            destination = _location("BP_Pickup_C_UAID_dest", same)
            source = _location("BP_Pickup_C_UAID_source", same)
            plan = WorldLootRandomizationPlan((WorldLootAssignment(destination, source),), (), (), ())
            result = apply_world_loot_plan(
                plan,
                game_root=root / "game",
                work_root=root / "work",
                output_content_root=root / "work" / "output" / "Content",
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=FakeResolver(),
                serializer=FakeSerializer(),
            )
            self.assertEqual(result.assignment_count, 1)
            self.assertEqual(result.reward_patch_count, 0)
            self.assertEqual(result.package_results, ())
            self.assertEqual(result.output_packages, ())

    # Check staged patch keep x2 count
    def test_staged_patch_keeps_x2_count_and_destination_notification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"map")
            destination = replace(
                _location("BP_Pickup_C_UAID_dest", "/Game/Item/DA_Item_X2"),
                rewards=(RewardPayload("/Game/Item/DA_Item_X2", 2, False),),
            )
            source = replace(
                _location("BP_Pickup_C_UAID_source", "/Game/Item/DA_Item_X1"),
                rewards=(RewardPayload("/Game/Item/DA_Item_X1", 1, True),),
            )
            plan = WorldLootRandomizationPlan((WorldLootAssignment(destination, source),), (), (), ())
            serializer = FakeSerializer()
            result = apply_world_loot_plan(
                plan,
                game_root=root / "game",
                work_root=root / "work",
                output_content_root=root / "work" / "output" / "Content",
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=FakeResolver(),
                serializer=serializer,
            )

            self.assertEqual(result.reward_patch_count, 1)
            self.assertEqual(len(serializer.requested_patches), 1)
            assigned = serializer.requested_patches[0].assigned
            self.assertEqual(assigned.article_data_path, "/Game/Item/DA_Item_X1")
            self.assertEqual(assigned.count, 2)
            self.assertFalse(assigned.b_notification)


if __name__ == "__main__":
    unittest.main()
