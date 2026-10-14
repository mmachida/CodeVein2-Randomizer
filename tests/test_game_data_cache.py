from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cv2randomizer.core.workspace import CacheManager
from cv2randomizer.game.asset_extraction import _normalize_asset_path, materialize_extracted_assets
from cv2randomizer.game.game_data_cache import (
    CACHE_FORMAT_VERSION,
    CACHE_INFO_FILENAME,
    CachedAssetExtractor,
    GameDataCacheSnapshot,
    GameDataCacheState,
    GameDataCacheStore,
    _link_raw_cache_assets,
    _valid_world_loot_mirror_reference,
)
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.versioning import VersionDetection


class GameDataCacheTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        # These tests use tiny synthetic inventories; catalogued rewards are
        # optional and are filtered against each install's package inventory.
        pass

    # Check cache descriptor accept matching world
    def test_cache_descriptor_accepts_only_matching_world_and_quest_mirror_paths(self) -> None:
        self.assertTrue(_valid_world_loot_mirror_reference(
            "ARTICLE_BULK", "/Game/Map/MainWorld/MainWorld/_Generated_/Mirror", 17,
        ))
        self.assertTrue(_valid_world_loot_mirror_reference(
            "QUEST_ARTICLE_GRANT", "/Game/Quest/CharacterEpisode/Noah/RewardVariant", 20,
        ))
        self.assertTrue(_valid_world_loot_mirror_reference("ARTICLE_BULK", None, None))
        self.assertFalse(_valid_world_loot_mirror_reference(
            "ARTICLE_BULK", "/Game/Quest/CharacterEpisode/Noah/RewardVariant", 20,
        ))
        self.assertFalse(_valid_world_loot_mirror_reference(
            "QUEST_ARTICLE_GRANT", "/Game/Map/MainWorld/MainWorld/_Generated_/Mirror", 17,
        ))
        self.assertFalse(_valid_world_loot_mirror_reference(
            "QUEST_ARTICLE_GRANT", "/Game/Quest/CharacterEpisode/Noah/RewardVariant", True,
        ))

    # Check clear removes all cache revisions
    def test_clear_removes_all_cache_revisions_but_keeps_root_and_does_not_follow_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_home = Path(temporary) / "Cache"
            root = cache_home / "extracted"
            with patch("cv2randomizer.core.workspace.user_cache_dir", return_value=str(cache_home)):
                (root / "current-revision" / "assets").mkdir(parents=True)
                (root / "old-app-version" / "stale.tmp").parent.mkdir(parents=True)
                (root / "current-revision" / "assets" / "asset.uasset").write_bytes(b"asset")
                (root / "old-app-version" / "stale.tmp").write_bytes(b"stale")
                external = Path(temporary) / "outside"
                external.mkdir()
                marker = external / "keep.txt"
                marker.write_text("preserve", encoding="utf-8")
                link = root / "external-link"
                try:
                    link.symlink_to(external, target_is_directory=True)
                    link_created = True
                except OSError:
                    link_created = False

                store = GameDataCacheStore()
                self.assertTrue(store.has_contents())
                store.clear()

                self.assertTrue(root.is_dir())
                self.assertEqual(list(root.iterdir()), [])
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
                if link_created:
                    self.assertFalse(link.exists())

    # Check clear refuses cache parent non
    def test_clear_refuses_cache_parent_and_non_cache_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache_parent = root / "Cache"
            cache_parent.mkdir()
            marker = cache_parent / "keep.txt"
            marker.write_text("preserve", encoding="utf-8")
            for unsafe_root in (
                cache_parent,
                root / "project" / "extracted",
                root / "project" / "Cache" / "extracted",
            ):
                store = GameDataCacheStore(CacheManager(unsafe_root))
                with self.assertRaises(ValueError):
                    store.clear()
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    # Read entry
    def _entry(self, root: Path, *, local_unverified: bool = False):
        game = root / "game"
        pak = game / "CodeVein2" / "Content" / "Paks"
        pak.mkdir(parents=True)
        (pak / "chunk.utoc").write_bytes(b"fixture toc")
        (pak / "chunk.ucas").write_bytes(b"fixture chunk")
        executable = game / "CodeVein2.exe"
        executable.write_bytes(b"fixture executable")
        local_version_id = "local-unverified-" + hashlib.sha256(executable.read_bytes()).hexdigest()
        detection = VersionDetection(
            not local_unverified,
            local_version_id if local_unverified else "fixture-build",
            "current", executable,
            "Local validation only" if local_unverified else "fixture build",
            () if local_unverified else ("/Game/Core",),
            local_unverified,
        )
        store = GameDataCacheStore(CacheManager(root / "persistent-cache"))
        entry = store.cache_entry_path(game, detection, create=True)
        _normalized, relative = _normalize_asset_path("/Game/Core")
        package_root = entry / "assets" / Path(*relative.parts)
        package_root.mkdir(parents=True)
        files = (("Core.uasset", b"header bytes"), ("Core.uexp", b"export bytes"))
        for name, payload in files:
            (package_root / name).write_bytes(payload)
        (package_root / ".cv2randomizer-asset.json").write_text(
            json.dumps({
                "virtual_path": "/Game/Core",
                "files": [
                    {"name": name, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
                    for name, payload in files
                ],
            }),
            encoding="utf-8",
        )
        metadata = store.build_metadata(
            game_root=game,
            detection=detection,
            source_registry={"core": ("/Game/Core",), "optional": ("/Game/Optional",)},
            required_sources=() if local_unverified else ("/Game/Core",),
        )
        store.write_metadata(entry, metadata)
        return game, detection, store, entry, package_root

    # Check ready cache materializes validated persistent
    def test_ready_cache_materializes_only_validated_persistent_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, entry, package_root = self._entry(Path(temporary))
            before = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in package_root.iterdir() if path.is_file() and path.suffix != ".json"
            }

            snapshot = store.inspect(game, detection)
            self.assertEqual(snapshot.state, GameDataCacheState.READY)
            self.assertEqual(snapshot.unavailable_optional_sources, ("/Game/Optional",))
            materialized = CachedAssetExtractor(snapshot).extract_assets(
                Path("missing-installed-game"),
                ("/Game/Core",),
                game_version="fixture-build",
                data_revision="current",
            )

            self.assertEqual(len(materialized), 1)
            self.assertEqual(materialized[0].virtual_path, "/Game/Core")
            self.assertEqual({path.suffix for path in materialized[0].files}, {".uasset", ".uexp"})
            after = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in package_root.iterdir() if path.is_file() and path.suffix != ".json"
            }
            self.assertEqual(after, before)
            self.assertTrue((entry / CACHE_INFO_FILENAME).is_file())

    # Check prepared descriptor reads declared healing
    def test_prepared_descriptor_reads_from_its_declared_healing_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entry = Path(temporary) / "READY"
            package = "/Game/Map/Generated/Map001"
            package_root = entry / "healing-prepared-assets" / "Map" / "Generated" / "Map001"
            package_root.mkdir(parents=True)
            payloads = {
                "Map001.umap": b"prepared map header",
                "Map001.uexp": b"prepared map exports",
            }
            files = []
            for name, payload in payloads.items():
                (package_root / name).write_bytes(payload)
                files.append({
                    "name": name,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
            (package_root / ".cv2randomizer-prepared-asset.json").write_text(
                json.dumps({"schema_version": 1, "virtual_path": package, "files": files}),
                encoding="utf-8",
            )
            snapshot = GameDataCacheSnapshot(
                GameDataCacheState.READY,
                entry,
                "fixture cache",
                game_version="fixture-build",
                catalog_revision="current",
                inventory=PackageInventory.create((package,)),
                metadata={},
            )

            result = CachedAssetExtractor(snapshot).extract_prepared_assets_with_descriptors(
                Path("unused"),
                {
                    package.casefold(): {
                        "package_path": package,
                        "prepared_asset": {
                            "root_name": "healing-prepared-assets",
                            "relative_path": "Map/Generated/Map001",
                        },
                    },
                },
                game_version="fixture-build",
                data_revision="current",
            )

            self.assertEqual(len(result), 1)
            self.assertTrue(all(path.parent == package_root for path in result[0].files))
            self.assertEqual({path.suffix for path in result[0].files}, {".umap", ".uexp"})

    # Check numbered package variant satisfies catalog
    def test_numbered_package_variant_satisfies_catalog_alias_and_cache_only_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, entry, _package_root = self._entry(Path(temporary))
            base = "/Game/Blueprint/Article/Item/Ammo/DA_Item_AmmoRifle"
            actual = base + "_1"
            _normalized, relative = _normalize_asset_path(actual)
            package_root = entry / "assets" / Path(*relative.parts)
            package_root.mkdir(parents=True)
            files = (("DA_Item_AmmoRifle_1.uasset", b"variant header"), ("DA_Item_AmmoRifle_1.uexp", b"variant export"))
            for name, payload in files:
                (package_root / name).write_bytes(payload)
            (package_root / ".cv2randomizer-asset.json").write_text(
                json.dumps({
                    "virtual_path": actual,
                    "files": [
                        {"name": name, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
                        for name, payload in files
                    ],
                }),
                encoding="utf-8",
            )
            metadata = store.build_metadata(
                game_root=game,
                detection=detection,
                source_registry={"core": ("/Game/Core",), "shops": (base,)},
                required_sources=("/Game/Core",),
            )
            store.write_metadata(entry, metadata)

            snapshot = store.inspect(game, detection)

            self.assertEqual(snapshot.state, GameDataCacheState.READY)
            self.assertNotIn(base, snapshot.unavailable_optional_sources)
            self.assertIn(base, snapshot.available_sources)
            self.assertTrue(snapshot.inventory.contains(base))
            extracted = CachedAssetExtractor(snapshot).extract_assets(
                game,
                (base,),
                game_version=detection.version_id,
                data_revision=detection.catalog_revision,
            )
            self.assertEqual(len(extracted), 1)
            self.assertEqual(extracted[0].virtual_path, actual)
            self.assertEqual(extracted[0].primary_file.name, "DA_Item_AmmoRifle_1.uasset")

    # Check generated map cache preserve identity
    def test_generated_map_cache_preserves_identity_through_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game, detection, store, entry, _ = self._entry(root)
            package = "/Game/Map/MainWorld/MainWorld/_Generated_/008YWFVVR3WYH2OZLKIYC10JN"
            _, relative = _normalize_asset_path(package)
            package_root = entry / "assets" / Path(*relative.parts)
            package_root.mkdir(parents=True)
            files = []
            for suffix in (".umap", ".uexp"):
                name = relative.name + suffix
                payload = suffix.encode()
                (package_root / name).write_bytes(payload)
                files.append({"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
            (package_root / ".cv2randomizer-asset.json").write_text(
                json.dumps({"virtual_path": package, "files": files}), encoding="utf-8",
            )
            store.write_metadata(entry, store.build_metadata(
                game_root=game, detection=detection,
                source_registry={"core": ("/Game/Core",), "world": (package,)},
                required_sources=("/Game/Core",),
            ))
            extractor = CachedAssetExtractor(store.inspect(game, detection))
            for request in (package, package.lower()):
                with self.subTest(request=request):
                    extracted = extractor.extract_assets(
                        game, (request,), game_version=detection.version_id,
                        data_revision=detection.catalog_revision,
                    )
                    staged = materialize_extracted_assets(extracted, root / "staged" / "Content")
                    # World Loot joins destination plans to staged maps by this identity.
                    by_virtual = {asset.virtual_path: asset for asset in staged}
                    self.assertIn(package, by_virtual)
                    self.assertEqual(by_virtual[package].primary_file.suffix, ".umap")
                    self.assertEqual(by_virtual[package].primary_file.read_bytes(), b".umap")

    # Check world loot dependency index is
    def test_world_loot_dependency_index_is_persisted_and_bound_to_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, entry, _package_root = self._entry(Path(temporary))
            index = {
                "schema_version": 1,
                "game_version": detection.version_id,
                "catalog_revision": detection.catalog_revision,
                "maps": {},
                "blueprint_packages": [],
                "blueprint_cdo_defaults": {},
                "world_loot_location_descriptors": {},
                "renewable_class_profiles": {},
            }
            metadata = store.build_metadata(
                game_root=game,
                detection=detection,
                source_registry={"core": ("/Game/Core",)},
                required_sources=("/Game/Core",),
                world_loot_dependency_index=index,
            )
            store.write_metadata(entry, metadata)
            snapshot = store.inspect(game, detection)
            self.assertEqual(snapshot.state, GameDataCacheState.READY)
            self.assertEqual(snapshot.metadata["world_loot_dependency_index"], index)

            metadata["world_loot_dependency_index"]["catalog_revision"] = "stale"
            store.write_metadata(entry, metadata)
            stale_snapshot = store.inspect(game, detection)
            self.assertEqual(stale_snapshot.state, GameDataCacheState.INVALID)

    # Check world loot prepared descriptor require
    def test_world_loot_prepared_descriptor_requires_exact_fname_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, entry, _package_root = self._entry(Path(temporary))
            package_path = "/Game/Map/MainWorld/MainWorld/_Generated_/MissingCell"
            reward = {
                "reward_index": 0,
                "article_path": "/Game/Blueprint/Article/Item/DA_Test_1",
                "count": 2,
                "notification": True,
                "soft_object_path_identity": {
                    "package_name": {"value": "/Game/Blueprint/Article/Item/DA_Test", "number": 2},
                    "asset_name": {"value": "DA_Test", "number": 2},
                },
                "article_package_available": False,
            }
            descriptor = {
                "package_path": package_path,
                "export_index": 17,
                "export_name": "BP_Pickup_UAID_test",
                "actor_class": "BP_Pickup_C",
                "actor_category": "GROUND_PICKUP",
                "destination_kind": "ARTICLE_BULK",
                "location_type": "NORMAL_WORLD_LOOT",
                "structural_class_package": None,
                "instance_override_source": None,
                "protected": True,
                "protection_reasons": ["explicit test protection"],
                "physical_instance_count": 1,
                "parent_blueprint_packages": [],
                "package_available": False,
                "blueprint_class_name": None,
                "blueprint_class_package": None,
                "prepared": False,
                "preparation_reason": "destination package is absent from the installed IoStore",
                "rewards": [reward],
                "patch_descriptor": {
                    "serializer": "ARTICLE_BULK",
                    "expected_rewards": [reward],
                },
            }
            index = {
                "schema_version": 1,
                "game_version": detection.version_id,
                "catalog_revision": detection.catalog_revision,
                "maps": {},
                "blueprint_packages": [],
                "blueprint_cdo_defaults": {},
                "renewable_class_profiles": {},
                "world_loot_location_descriptors": {
                    f"{package_path.casefold()}#17": descriptor,
                },
            }
            metadata = store.build_metadata(
                game_root=game,
                detection=detection,
                source_registry={"core": ("/Game/Core",)},
                required_sources=("/Game/Core",),
                world_loot_dependency_index=index,
            )
            store.write_metadata(entry, metadata)
            self.assertEqual(store.inspect(game, detection).state, GameDataCacheState.READY)

            metadata["world_loot_dependency_index"]["world_loot_location_descriptors"][
                f"{package_path.casefold()}#17"
            ]["rewards"][0]["soft_object_path_identity"]["package_name"]["number"] = 1
            store.write_metadata(entry, metadata)
            invalid = store.inspect(game, detection)
            self.assertEqual(invalid.state, GameDataCacheState.INVALID)
            self.assertIn("FName", invalid.reason)

    # Check catalogued locked renewable outside material
    def test_catalogued_locked_renewable_outside_material_pool_needs_no_pool_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, entry, _package_root = self._entry(Path(temporary))
            map_package = "/Game/Map/MainWorld/MainWorld/_Generated_/RenewableCell"
            class_package = "/Game/Environment/Item/it21/it21_004/BP_it21_004_A_0"

            # Add asset
            def add_asset(package_path: str) -> None:
                _normalized, relative = _normalize_asset_path(package_path)
                package_root = entry / "assets" / Path(*relative.parts)
                package_root.mkdir(parents=True)
                files = []
                for suffix in (".uasset", ".uexp"):
                    name = relative.name + suffix
                    payload = (package_path + suffix).encode("utf-8")
                    (package_root / name).write_bytes(payload)
                    files.append({
                        "name": name,
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    })
                (package_root / ".cv2randomizer-asset.json").write_text(
                    json.dumps({"virtual_path": package_path, "files": files}), encoding="utf-8",
                )

            add_asset(map_package)
            add_asset(class_package)
            reward = {
                "reward_index": 0,
                "article_path": "/Game/Blueprint/Article/Item/Material/DA_Item_MatCommonFire_2",
                "count": 1,
                "notification": True,
                "soft_object_path_identity": None,
                "article_package_available": False,
            }
            descriptor = {
                "package_path": map_package,
                "export_index": 2,
                "export_name": "BP_it21_004_A_0_C_UAID_renewable",
                "actor_class": "BP_it21_004_A_0_C",
                "actor_category": "MATERIAL_PICKUP",
                "destination_kind": "ARTICLE_BULK",
                "location_type": "RENEWABLE_INGREDIENT",
                "structural_class_package": class_package,
                "catalogued_effective_reset_on_yadorigi_rest": True,
                "reset_validation_source": "catalogued_physical_audit",
                "instance_override_source": None,
                "destination_id": None,
                "protected": True,
                "protection_reasons": ["RENEWABLE_INGREDIENT_DISABLED"],
                "physical_instance_count": 1,
                "parent_blueprint_packages": [
                    "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base",
                ],
                "package_available": True,
                "blueprint_class_name": "BP_it21_004_A_0_C",
                "blueprint_class_package": class_package,
                "prepared": False,
                "preparation_reason": "catalogued renewable source is locked",
                "rewards": [reward],
                "patch_descriptor": {"serializer": "ARTICLE_BULK", "expected_rewards": [reward]},
            }
            index = {
                "schema_version": 1,
                "game_version": detection.version_id,
                "catalog_revision": detection.catalog_revision,
                "maps": {},
                "blueprint_packages": [],
                "blueprint_cdo_defaults": {},
                "world_loot_location_descriptors": {f"{map_package.casefold()}#2": descriptor},
                "renewable_class_profiles": {},
            }
            store.write_metadata(entry, store.build_metadata(
                game_root=game,
                detection=detection,
                source_registry={
                    "core": ("/Game/Core",),
                    "map": (map_package,),
                    "class": (class_package,),
                },
                required_sources=("/Game/Core",),
                world_loot_dependency_index=index,
            ))

            snapshot = store.inspect(game, detection)
            self.assertEqual(snapshot.state, GameDataCacheState.READY, snapshot.reason)

            descriptor["parent_blueprint_packages"] = []
            store.write_metadata(entry, store.build_metadata(
                game_root=game,
                detection=detection,
                source_registry={
                    "core": ("/Game/Core",),
                    "map": (map_package,),
                    "class": (class_package,),
                },
                required_sources=("/Game/Core",),
                world_loot_dependency_index=index,
            ))
            invalid = store.inspect(game, detection)
            self.assertEqual(invalid.state, GameDataCacheState.INVALID)
            self.assertIn("hierarchy profile", invalid.reason)

    # Check healing pickup does not require
    def test_healing_pickup_does_not_require_a_renewable_hierarchy_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, entry, _package_root = self._entry(Path(temporary))
            map_package = "/Game/Map/MainWorld/MainWorld/_Generated_/HealingCell"
            class_package = "/Game/Environment/Item/it10/it10_002/BP_it10_002_B_0"

            # Add asset
            def add_asset(package_path: str) -> None:
                _normalized, relative = _normalize_asset_path(package_path)
                package_root = entry / "assets" / Path(*relative.parts)
                package_root.mkdir(parents=True)
                files = []
                for suffix in (".umap", ".uexp") if package_path == map_package else (".uasset", ".uexp"):
                    name = relative.name + suffix
                    payload = (package_path + suffix).encode("utf-8")
                    (package_root / name).write_bytes(payload)
                    files.append({
                        "name": name,
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    })
                (package_root / ".cv2randomizer-asset.json").write_text(
                    json.dumps({"virtual_path": package_path, "files": files}), encoding="utf-8",
                )

            add_asset(map_package)
            add_asset(class_package)
            reward = {
                "reward_index": 0,
                "article_path": "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryCountUp_1",
                "count": 1,
                "notification": True,
                "soft_object_path_identity": None,
                "article_package_available": False,
            }
            descriptor = {
                "package_path": map_package,
                "export_index": 52,
                "export_name": "BP_it10_002_B_0_C_UAID_healing",
                "actor_class": "BP_it10_002_B_0_C",
                "actor_category": "GROUND_PICKUP",
                "destination_kind": "INSTANCE_ARTICLE_BULK_OVERRIDE",
                "location_type": "HEALING_UPGRADE_GOLDEN_SEED",
                "structural_class_package": class_package,
                "instance_override_source": None,
                "protected": True,
                "protection_reasons": ["test healing lock"],
                "physical_instance_count": 1,
                "parent_blueprint_packages": [
                    "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_RecoveryCountUp_Base",
                ],
                "package_available": True,
                "prepared": False,
                "preparation_reason": "test fixture",
                "rewards": [reward],
                "patch_descriptor": {
                    "serializer": "INSTANCE_ARTICLE_BULK_OVERRIDE",
                    "expected_rewards": [reward],
                },
            }
            index = {
                "schema_version": 1,
                "game_version": detection.version_id,
                "catalog_revision": detection.catalog_revision,
                "maps": {},
                "blueprint_packages": [],
                "blueprint_cdo_defaults": {},
                "world_loot_location_descriptors": {f"{map_package.casefold()}#52": descriptor},
                "renewable_class_profiles": {},
            }
            store.write_metadata(entry, store.build_metadata(
                game_root=game,
                detection=detection,
                source_registry={
                    "core": ("/Game/Core",),
                    "map": (map_package,),
                    "class": (class_package,),
                },
                required_sources=("/Game/Core",),
                world_loot_dependency_index=index,
            ))

            snapshot = store.inspect(game, detection)

            self.assertEqual(snapshot.state, GameDataCacheState.READY, snapshot.reason)

    # Check missing required cached file invalidates
    def test_missing_required_cached_file_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, _entry, package_root = self._entry(Path(temporary))
            (package_root / "Core.uexp").unlink()
            snapshot = store.inspect(game, detection)
            self.assertEqual(snapshot.state, GameDataCacheState.INVALID)

    # Check required source definition change marks
    def test_required_source_definition_change_marks_cache_outdated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, _entry, _package_root = self._entry(Path(temporary))
            changed = VersionDetection(
                True, detection.version_id, detection.catalog_revision,
                detection.executable_path, detection.reason,
                ("/Game/Core", "/Game/NewCore"),
            )
            snapshot = store.inspect(game, changed)
            self.assertEqual(snapshot.state, GameDataCacheState.OUTDATED)

    # Check cache accept missing optional blood
    def test_cache_accepts_missing_optional_blood_code_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, _entry, _package_root = self._entry(Path(temporary))
            snapshot = store.inspect(game, detection)
            self.assertEqual(snapshot.state, GameDataCacheState.READY, snapshot.reason)
            self.assertEqual(snapshot.required_sources, ("/Game/Core",))

    # Check local unverified build can create
    def test_local_unverified_build_can_create_an_isolated_not_extracted_cache_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            pak = game / "CodeVein2" / "Content" / "Paks"
            pak.mkdir(parents=True)
            (pak / "chunk.utoc").write_bytes(b"fixture toc")
            executable = game / "CodeVein2.exe"
            executable.write_bytes(b"local fixture build")
            detection = VersionDetection(
                False,
                "local-unverified-" + hashlib.sha256(executable.read_bytes()).hexdigest(),
                "current",
                executable,
                "Local validation only",
                (),
                True,
            )
            store = GameDataCacheStore(CacheManager(root / "cache"))

            snapshot = store.inspect(game, detection)

            self.assertEqual(snapshot.state, GameDataCacheState.NOT_EXTRACTED)
            self.assertTrue(snapshot.entry_path.is_relative_to(store.root))
            self.assertEqual(
                store.cache_entry_path(game, detection, create=False),
                snapshot.entry_path,
            )

    # Check local unverified cache reopens explicit
    def test_local_unverified_cache_reopens_with_explicit_validation_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, _entry, _package_root = self._entry(
                Path(temporary), local_unverified=True,
            )

            snapshot = store.inspect(game, detection)

            self.assertEqual(snapshot.state, GameDataCacheState.READY)
            self.assertEqual(snapshot.metadata["version_validation"], "local-unverified")

    # Check cache is reused same archives
    def test_cache_is_reused_for_same_archives_in_another_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, _entry, _package_root = self._entry(Path(temporary))
            second_game = Path(temporary) / "another-install"
            second_pak = second_game / "CodeVein2" / "Content" / "Paks"
            second_pak.mkdir(parents=True)
            source_pak = game / "CodeVein2" / "Content" / "Paks"
            shutil.copy2(source_pak / "chunk.utoc", second_pak / "chunk.utoc")
            shutil.copy2(source_pak / "chunk.ucas", second_pak / "chunk.ucas")
            second_executable = second_game / "CodeVein2.exe"
            second_executable.write_bytes(b"fixture executable")
            second_detection = VersionDetection(
                True, detection.version_id, detection.catalog_revision,
                second_executable, detection.reason, detection.required_asset_paths,
            )
            self.assertEqual(store.inspect(game, detection).state, GameDataCacheState.READY)
            self.assertEqual(store.inspect(second_game, second_detection).state, GameDataCacheState.READY)

    # Check app revision mapping changes reuse
    def test_app_revision_and_mapping_changes_reuse_source_cache_and_drop_stale_preparation(self) -> None:
        from unittest.mock import patch
        import cv2randomizer.game.game_data_cache as cache_module

        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, entry, _package_root = self._entry(Path(temporary))
            with patch.object(cache_module, "cv2_usmap_sha256", return_value="a" * 64):
                first_identity = store.identity(game, detection)
                first_entry = store.cache_entry_path(game, detection)
            metadata = json.loads((entry / CACHE_INFO_FILENAME).read_text(encoding="utf-8"))
            metadata["world_loot_dependency_index"] = {"stale": True}
            store.write_metadata(entry, metadata)
            with (
                patch.object(cache_module, "cv2_usmap_sha256", return_value="b" * 64),
                patch.object(
                    cache_module,
                    "WORLD_LOOT_PREPARATION_REVISION",
                    cache_module.WORLD_LOOT_PREPARATION_REVISION + 1,
                ),
                patch.object(cache_module, "SOURCE_REGISTRY_REVISION", "new-app-source-registry"),
            ):
                changed_entry = store.cache_entry_path(game, detection)
                snapshot = store.inspect(game, detection)

            self.assertEqual(first_identity["usmap_sha256"], "a" * 64)
            self.assertEqual(first_entry, changed_entry)
            self.assertEqual(snapshot.state, GameDataCacheState.PREPARED_OUTDATED, snapshot.reason)
            self.assertNotIn("world_loot_dependency_index", snapshot.metadata)

    # Check changed game archives require reextraction
    def test_changed_game_archives_require_reextraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, _entry, _package_root = self._entry(Path(temporary))
            archive = game / "CodeVein2" / "Content" / "Paks" / "chunk.utoc"
            archive.write_bytes(b"updated fixture toc")

            snapshot = store.inspect(game, detection)

            self.assertEqual(snapshot.state, GameDataCacheState.OUTDATED)
            self.assertIn("game archives changed", snapshot.reason)

    # Check legacy content addressed cache is
    def test_legacy_content_addressed_cache_is_migrated_into_single_ready_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, entry, _package_root = self._entry(Path(temporary))
            store.cache.single_entry = False
            legacy_entry = store.cache_entry_path(game, detection, create=False)
            entry.replace(legacy_entry)
            self.assertTrue(legacy_entry.is_dir())
            store.cache.single_entry = True

            snapshot = store.inspect(game, detection)

            self.assertEqual(snapshot.state, GameDataCacheState.READY, snapshot.reason)
            self.assertEqual(snapshot.entry_path, store.root / "READY")
            self.assertTrue(snapshot.entry_path.is_dir())
            self.assertFalse(legacy_entry.exists())
            self.assertEqual(
                [child.name for child in store.root.iterdir() if child.is_dir()],
                ["READY"],
            )

    # Check older cache format stale dependency
    def test_older_cache_format_and_stale_dependency_index_do_not_force_reextract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            game, detection, store, entry, _package_root = self._entry(Path(temporary))
            metadata = json.loads((entry / CACHE_INFO_FILENAME).read_text(encoding="utf-8"))
            metadata["cache_format_version"] = CACHE_FORMAT_VERSION - 1
            metadata["world_loot_preparation_revision"] = "older-preparation"
            metadata["world_loot_dependency_index"] = {"schema_version": -1}
            store.write_metadata(entry, metadata)

            snapshot = store.inspect(game, detection)

            self.assertEqual(snapshot.state, GameDataCacheState.PREPARED_OUTDATED, snapshot.reason)
            self.assertNotIn("world_loot_dependency_index", snapshot.metadata)

    # Check raw cache reuse hardlinks immutable
    def test_raw_cache_reuse_hardlinks_immutable_assets_without_copying_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "READY"
            destination = root / ".rebuild" / "READY"
            package = source / "assets" / "Blueprint" / "Article" / "DA_Test"
            package.mkdir(parents=True)
            payload = package / "DA_Test.uasset"
            payload.write_bytes(b"immutable source asset")
            manifest = package / ".cv2randomizer-asset.json"
            manifest.write_text("{}", encoding="utf-8")

            files, reused_bytes = _link_raw_cache_assets(source, destination)

            self.assertEqual(files, 2)
            self.assertEqual(reused_bytes, payload.stat().st_size + manifest.stat().st_size)
            linked_payload = destination / "assets" / "Blueprint" / "Article" / "DA_Test" / payload.name
            self.assertTrue(payload.samefile(linked_payload))
            replacement = linked_payload.with_suffix(".new")
            replacement.write_bytes(b"new bytes")
            replacement.replace(linked_payload)
            self.assertEqual(payload.read_bytes(), b"immutable source asset")


if __name__ == "__main__":
    unittest.main()
