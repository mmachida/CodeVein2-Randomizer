import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cv2randomizer.game.catalogs import CatalogError, CatalogStore
from cv2randomizer.game.locator import check_installation
from cv2randomizer.game.renewable_ingredients import renewable_material_locations
from cv2randomizer.game.versioning import (
    KNOWN_EXECUTABLE_MD5_DISPLAY_VERSIONS,
    GameVersionDetector,
    VersionDefinition,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CORE_PACKAGE = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"


class GameLocatorAndVersionTests(unittest.TestCase):
    # Check installation structure require content archives
    def test_installation_structure_requires_content_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "CodeVein2" / "Content" / "Paks").mkdir(parents=True)

            empty = check_installation(root)
            self.assertFalse(empty.valid)
            self.assertIn("contains no", empty.reason)

            (root / "CodeVein2" / "Content" / "Paks" / "chunk0.utoc").write_bytes(b"fixture")
            valid = check_installation(root)
            self.assertTrue(valid.valid)
            self.assertEqual(valid.pak_directory, root / "CodeVein2" / "Content" / "Paks")

    # Check detector support exact executable signature
    def test_detector_supports_only_an_exact_executable_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            paks = root / "CodeVein2" / "Content" / "Paks"
            executable_relative = Path("CodeVein2/Binaries/Win64/CodeVein2-Win64-Shipping.exe")
            executable = root / executable_relative
            paks.mkdir(parents=True)
            executable.parent.mkdir(parents=True)
            (paks / "chunk0.utoc").write_bytes(b"fixture container")
            executable.write_bytes(b"known fixture build")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            definitions = Path(temporary) / "versions"
            definitions.mkdir()
            (definitions / "fixture.json").write_text(
                json.dumps({
                    "version_id": "fixture-build",
                    "executable": {"path": executable_relative.as_posix(), "sha256": [digest]},
                    "catalog_revision": "current",
                    "required_asset_paths": [_CORE_PACKAGE],
                    "required_containers": ["CodeVein2/Content/Paks/chunk0.utoc"],
                }),
                encoding="utf-8",
            )
            detector = GameVersionDetector(definitions)

            supported = detector.detect(root)
            self.assertTrue(supported.supported)
            self.assertEqual(supported.version_id, "fixture-build")
            self.assertEqual(supported.required_asset_paths, (_CORE_PACKAGE,))
            self.assertFalse(hasattr(supported, "dlc_available"))

            executable.write_bytes(b"changed build")
            unsupported = detector.detect(root)
            self.assertFalse(unsupported.supported)
            self.assertIsNone(unsupported.version_id)

            local = GameVersionDetector(
                definitions, allow_local_unverified=True,
            ).detect(root)
            self.assertFalse(local.supported)
            self.assertTrue(local.local_unverified)
            self.assertTrue(local.version_id.startswith("local-unverified-"))
            self.assertEqual(local.catalog_revision, "current")
            self.assertEqual(local.required_asset_paths, ())
            self.assertTrue(local.usable_for_local_validation)
            self.assertIn("not officially supported", local.reason)

    # Check local validation can identify valid
    def test_local_validation_can_identify_a_valid_install_without_version_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            paks = root / "CodeVein2" / "Content" / "Paks"
            executable = root / "CodeVein2" / "Binaries" / "Win64" / "CodeVein2-Win64-Shipping.exe"
            paks.mkdir(parents=True)
            executable.parent.mkdir(parents=True)
            (paks / "chunk0.utoc").write_bytes(b"fixture container")
            executable.write_bytes(b"local fixture build")
            definitions = Path(temporary) / "empty-versions"
            definitions.mkdir()

            detection = GameVersionDetector(
                definitions, allow_local_unverified=True,
            ).detect(root)

            self.assertFalse(detection.supported)
            self.assertTrue(detection.local_unverified)
            self.assertEqual(detection.executable_path, executable)
            self.assertIsNone(detection.display_version)

    # Check known executable md5 adds local
    def test_known_executable_md5_adds_only_a_local_display_label(self) -> None:
        expected_labels = {
            "AED1E2455C9978BC986DC8DD2FB8FDF6": "1.0.2.0",
            "F55ABDC919FCC87208674C5E78F33E02": "1.0.3.0",
            "A68382E1A308443C9B807FB3E803DF93": "1.0.5.0",
            "24F801822281A01AE7634C8D21128B1E": "1.0.6.0",
            "C01534CEE9E893CA1237FB24E95A55F4": "1.0.7.0",
            "A3C395779465AC4009796ED0964942D7": "1.0.9.0",
            "E9BC16005E960B49EDF82C88853F4C75": "2.0.1.0",
            "2E8999C26D26F1B7B9FFCDA0986F44CB": "2.0.2.0",
            "C904F7B7F771AE6C14AD5288EDACBFD5": "2.0.3.0",
        }
        self.assertEqual(KNOWN_EXECUTABLE_MD5_DISPLAY_VERSIONS, expected_labels)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            paks = root / "CodeVein2" / "Content" / "Paks"
            executable = root / "CodeVein2" / "Binaries" / "Win64" / "CodeVein2-Win64-Shipping.exe"
            paks.mkdir(parents=True)
            executable.parent.mkdir(parents=True)
            (paks / "chunk0.utoc").write_bytes(b"fixture container")
            executable.write_bytes(b"known build fixture")
            versions = Path(temporary) / "versions"
            versions.mkdir()

            with patch(
                "cv2randomizer.game.versioning._executable_hashes",
                return_value=("a" * 64, "f55abd c919fcc87208674c5e78f33e02".replace(" ", "")),
            ):
                detection = GameVersionDetector(
                    versions, allow_local_unverified=True,
                ).detect(root)

        self.assertFalse(detection.supported)
        self.assertTrue(detection.local_unverified)
        self.assertEqual(detection.version_id, f"local-unverified-{'a' * 64}")
        self.assertEqual(detection.display_version, "1.0.3.0")

    # Check version support is independent optional
    def test_version_support_is_independent_from_optional_dlc_marker_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            paks = root / "CodeVein2" / "Content" / "Paks"
            executable_relative = Path("CodeVein2/Binaries/Win64/CodeVein2-Win64-Shipping.exe")
            executable = root / executable_relative
            paks.mkdir(parents=True)
            executable.parent.mkdir(parents=True)
            (paks / "chunk0.utoc").write_bytes(b"fixture container")
            executable.write_bytes(b"fixture supported build")
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            definitions = Path(temporary) / "versions"
            definitions.mkdir()
            (definitions / "fixture.json").write_text(json.dumps({
                "version_id": "fixture-build",
                "executable": {"path": executable_relative.as_posix(), "sha256": [digest]},
                "catalog_revision": "current",
                "required_asset_paths": [_CORE_PACKAGE],
            }), encoding="utf-8")
            supported = GameVersionDetector(definitions).detect(root)
            self.assertTrue(supported.supported)
            self.assertEqual(supported.required_asset_paths, (_CORE_PACKAGE,))

    # Check version definition require core source
    def test_version_definition_requires_a_core_source_package(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one mandatory core"):
            VersionDefinition.from_dict({
                "version_id": "fixture",
                "executable": {"path": "CodeVein2.exe", "sha256": ["a" * 64]},
                "catalog_revision": "current",
            })

    # Check version paths cannot escape game
    def test_version_paths_cannot_escape_the_game_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "traverse parent folders"):
            VersionDefinition.from_dict({
                "version_id": "fixture",
                "executable": {"path": "../outside.exe", "sha256": ["a" * 64]},
                "catalog_revision": "current",
            })


class RuntimeCatalogTests(unittest.TestCase):
    # Check current catalog has expected complete
    def test_current_catalog_has_expected_complete_pools(self) -> None:
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")

        self.assertEqual(len(catalogs.materials), 734)
        self.assertEqual(len(catalogs.shops), 855)
        self.assertEqual(len({item.save_id.casefold() for item in catalogs.materials}), 734)
        self.assertTrue(all(item.reward.count == 1 and item.reward.notification for item in catalogs.materials))
        important_shops = [
            item for item in catalogs.shops
            if "/item/important/" in item.original_product_path.casefold()
        ]
        self.assertEqual(len(important_shops), 135)
        self.assertTrue(all(not item.protected for item in important_shops))
        self.assertEqual(len(catalogs.important_item_exclusions.keys), 17)
        self.assertEqual(len(catalogs.important_item_exclusions.quest_flag_exclusions), 26)
        self.assertEqual(catalogs.important_item_audit["important_world_locations_unlocked"], 49)
        self.assertEqual(catalogs.important_item_audit["important_shop_products_unlocked"], 135)
        self.assertEqual(
            {key: sum(item.content_class == key for item in catalogs.world_loot)
             for key in ("BASE", "DLC", "AFTER_ENDING", "UNKNOWN")},
            {"BASE": 2435, "DLC": 4, "AFTER_ENDING": 10, "UNKNOWN": 3585},
        )
        self.assertEqual(
            {key: sum(reward.content_class == key for item in catalogs.world_loot for reward in item.rewards)
             for key in ("BASE", "DLC", "AFTER_ENDING", "UNKNOWN")},
            {"BASE": 210, "DLC": 13, "AFTER_ENDING": 6, "UNKNOWN": 5825},
        )
        quest_grants = [
            item for item in catalogs.world_loot
            if item.destination_kind == "QUEST_ARTICLE_GRANT"
        ]
        self.assertEqual(len(quest_grants), 77)
        added_quest_grants = [item for item in quest_grants if (item.destination_id or "").startswith("quest_reward_")]
        self.assertEqual(len(added_quest_grants), 41)
        self.assertEqual(sum(len(item.rewards) for item in added_quest_grants), 46)
        self.assertEqual(sum(len(item.rewards) > 1 for item in added_quest_grants), 5)
        partial_bundles = [
            item for item in added_quest_grants
            if item.quest_article_layout is not None
            and item.quest_article_layout.expected_articles_count == 2
        ]
        self.assertEqual(len(partial_bundles), 3)
        self.assertEqual(sorted(item.quest_article_layout.reward_index for item in partial_bundles), [0, 1, 1])
        self.assertTrue(all(len(item.rewards) == 1 for item in partial_bundles))
        self.assertFalse(any(
            "DA_Item_Important" in reward.article_path
            for item in added_quest_grants for reward in item.rewards
        ))
        self.assertTrue(all(item.where_content_class == "UNKNOWN" for item in catalogs.materials))
        self.assertTrue(all(item.resource_content_class == "UNKNOWN" for item in catalogs.materials))
        self.assertTrue(all(item.reward_content_class == "UNKNOWN" for item in catalogs.materials))
        self.assertEqual(
            {key: sum(item.what_content_class == key for item in catalogs.shops)
             for key in ("BASE", "DLC", "AFTER_ENDING", "UNKNOWN")},
            {"BASE": 7, "DLC": 2, "AFTER_ENDING": 0, "UNKNOWN": 846},
        )
        catalogs.require_features(world_loot=True, materials=True, shops=True)

    # Check renewable ingredient pool use all
    def test_renewable_ingredient_pool_uses_all_audited_nodes_and_keeps_world_loot_locked(self) -> None:
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        world_renewables = tuple(
            location for location in catalogs.world_loot
            if location.location_type == "RENEWABLE_INGREDIENT"
        )
        ingredients = renewable_material_locations(catalogs.world_loot)

        self.assertEqual(len(world_renewables), 2253)
        self.assertEqual(len(ingredients), 2253)
        self.assertEqual(sum(item.mirror_of_package_path is None for item in ingredients), 1278)
        self.assertEqual(sum(item.mirror_of_package_path is not None for item in ingredients), 975)
        self.assertEqual(len({item.resource_class_package.casefold() for item in ingredients}), 49)
        self.assertTrue(all(not item.protected for item in ingredients))
        self.assertTrue(all(
            item.protected and item.protection_reasons == ("RENEWABLE_INGREDIENT_DISABLED",)
            for item in world_renewables
        ))

    # Check modified catalog is rejected manifest
    def test_modified_catalog_is_rejected_by_manifest_signature(self) -> None:
        source = _REPOSITORY_ROOT / "data" / "catalogs" / "current"
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "current"
            shutil.copytree(source, copied)
            shops = copied / "shops.json"
            shops.write_text(shops.read_text(encoding="utf-8") + " ", encoding="utf-8")

            with self.assertRaisesRegex(CatalogError, "signature mismatch"):
                CatalogStore(Path(temporary)).load("current")


if __name__ == "__main__":
    unittest.main()
