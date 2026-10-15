from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.important_items import ImportantItemExclusionStore
from cv2randomizer.game.catalogs import (
    FLAG_PICKUP_ARTICLE_ONLY_ALLOWLIST,
    CatalogStore,
    is_world_map_package,
)
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan
from tools.import_catalogs import (
    import_materials,
    import_shops,
    import_world_loot,
    read_explicit_scripted_reward_modes,
    read_explicit_world_source_protections,
)


_ROOT = Path(__file__).resolve().parents[1]


class ImportantItemPolicyTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.exclusions = ImportantItemExclusionStore(
            _ROOT / "data" / "reward_pools" / "current",
        ).load("current")

    # Check archive line endings validate
    def test_exclusion_store_accepts_lf_and_crlf_catalog_files(self) -> None:
        source_dir = _ROOT / "data" / "reward_pools" / "current"
        source_catalog = (source_dir / "important_item_exclusions.json").read_bytes()
        lf_catalog = source_catalog.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        with tempfile.TemporaryDirectory() as temporary:
            target_dir = Path(temporary)
            (target_dir / "manifest.json").write_bytes((source_dir / "manifest.json").read_bytes())
            catalog_path = target_dir / "important_item_exclusions.json"
            for payload in (lf_catalog, lf_catalog.replace(b"\n", b"\r\n")):
                catalog_path.write_bytes(payload)
                loaded = ImportantItemExclusionStore(target_dir).load("current")
                self.assertEqual(len(loaded.keys), 17)
                self.assertEqual(len(loaded.quest_flag_exclusions), 26)

    # Check authoritative JSON retains keys exact
    def test_authoritative_json_retains_keys_and_exact_quest_flag_list(self) -> None:
        self.assertEqual(len(self.exclusions.keys), 17)
        self.assertEqual(len(self.exclusions.quest_flag_exclusions), 26)
        self.assertEqual(len(self.exclusions.key_identities), 17)
        self.assertIn(
            "/Game/Blueprint/Article/Item/DA_Item_Important_StorageKey".casefold(),
            self.exclusions.key_identities,
        )
        self.assertIn(
            "/Game/Blueprint/Article/Item/DA_Item_Important_WarehouseKey".casefold(),
            self.exclusions.key_identities,
        )
        self.assertEqual(len(self.exclusions.quest_flag_identities), 26)
        self.assertIn(
            "/Game/Blueprint/Article/Item/DA_Item_Important_Josee_RuneBlade".casefold(),
            self.exclusions.quest_flag_identities,
        )
        self.assertIn(
            "/Game/Blueprint/Article/Item/Important/DA_Item_Important_SpellForTimeTravel".casefold(),
            self.exclusions.quest_flag_identities,
        )
        self.assertIn(
            "/Game/Blueprint/Article/Item/DA_Important_ZenonPassport".casefold(),
            self.exclusions.quest_flag_identities,
        )
        self.assertIn(
            "/Game/Blueprint/Article/Item/DA_Important_ZenonPassport_Upgraded".casefold(),
            self.exclusions.quest_flag_identities,
        )
        self.assertIn(
            "/Game/Blueprint/Article/Item/DA_Important_LiseHairOrnament_Name".casefold(),
            self.exclusions.quest_flag_identities,
        )
        self.assertFalse(any(
            "/recipe_" in path.casefold()
            for path in self.exclusions.quest_flag_identities
        ))
        for family, variants in (
            ("EngravedWeapon", ("01", "02", "03")),
            ("RiftBook", ("01", "02", "03", "04")),
            ("Spice", ("01", "02", "03")),
        ):
            for variant in variants:
                self.assertIn(
                    f"/Game/Blueprint/Article/Item/Important/DA_Item_Important_{family}_{variant}".casefold(),
                    self.exclusions.quest_flag_identities,
                )

    # Check world import unlocks generic important
    def test_world_import_unlocks_only_the_generic_important_rule(self) -> None:
        key = self.exclusions.keys[0]
        quest_flag = self.exclusions.quest_flag_exclusions[0]
        rows = [
            _world_row("SafeImportant.umap", 1, 0, "/Game/Blueprint/Article/Item/Important/DA_Recipe", "GROUND_PICKUP", "keyword:important"),
            _world_row("StoryTagged.umap", 1, 0, "/Game/Blueprint/Article/Item/Luxury/DA_Item_LuxuryHistoryBook_1", "GROUND_PICKUP", "keyword:story"),
            _world_row("Key.umap", 1, 0, key, "GROUND_PICKUP", ""),
            _world_row("QuestFlag.umap", 1, 0, quest_flag, "GROUND_PICKUP", "keyword:important"),
            _world_row("Bundle.umap", 1, 0, "/Game/Blueprint/Article/Item/Support/DA_Consumable", "GROUND_PICKUP", ""),
            _world_row("Bundle.umap", 1, 1, key, "GROUND_PICKUP", ""),
            _world_row("Scripted.umap", 1, 0, "/Game/Blueprint/Article/Item/Important/DA_Scripted", "GROUND_PICKUP", "", export_name="BP_PickupItem_WithSmallScenario_UAID_1"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "world.csv"
            _write_csv(source, rows)
            result = import_world_loot(source, set(), self.exclusions)
        by_source = {item["source_file"]: item for item in result["locations"]}
        safe = by_source["SafeImportant.umap"]
        story_tagged = by_source["StoryTagged.umap"]
        self.assertFalse(safe["protected"])
        self.assertFalse(story_tagged["protected"])
        self.assertEqual(story_tagged["protection_reasons"], [])
        self.assertEqual(safe["content_class"], "UNKNOWN")
        self.assertEqual(safe["rewards"][0]["content_class"], "UNKNOWN")
        self.assertNotIn("Important item", safe["protection_reasons"])
        self.assertNotIn("keyword:important", safe["protection_reasons"])
        self.assertEqual(by_source["Key.umap"]["protection_reasons"], ["KEY_EXCLUDED"])
        self.assertEqual(by_source["QuestFlag.umap"]["protection_reasons"], ["QUEST_FLAG_EXCLUDED"])
        self.assertTrue(by_source["Bundle.umap"]["protected"])
        self.assertIn("KEY_EXCLUDED", by_source["Bundle.umap"]["protection_reasons"])
        self.assertIn("scenario pickup", by_source["Scripted.umap"]["protection_reasons"])

    # Check lise hair ornament is protected
    def test_lise_hair_ornament_is_protected_by_exact_quest_identity(self) -> None:
        catalogs = CatalogStore(_ROOT / "data" / "catalogs").load("current")
        location = next(
            item for item in catalogs.world_loot
            if any("DA_Important_LiseHairOrnament_Name" in reward.article_path for reward in item.rewards)
        )
        self.assertTrue(location.protected)
        self.assertEqual(location.protection_reasons, ("QUEST_FLAG_EXCLUDED",))

    # Check recipe articles are not blanket
    def test_recipe_articles_are_not_blanket_protected_by_important_name(self) -> None:
        recipe = "/Game/Blueprint/Article/Item/Important/DA_Item_Important_Recipe_FoodSourPork"
        self.assertNotIn(recipe.casefold(), self.exclusions.quest_flag_identities)
        catalogs = CatalogStore(_ROOT / "data" / "catalogs").load("current")
        matching = [item for item in catalogs.shops if item.original_product_path.casefold() == recipe.casefold()]
        self.assertTrue(matching)
        self.assertTrue(all(not item.protected for item in matching))

    # Check world import keep where what
    def test_world_import_keeps_where_and_what_classes_independent(self) -> None:
        row = _world_row(
            "DlcSlot.umap", 4, 0, "/Game/Blueprint/Article/Item/Support/DA_BaseItem",
            "GROUND_PICKUP", "",
        )
        row["where_content_class"] = "DLC"
        row["what_content_class"] = "BASE"
        row["map_package"] = "/Game/Map/MainWorld/DlcWorld/_Generated_/DlcSlot"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "world.csv"
            _write_csv(source, [row])
            result = import_world_loot(source, set(), self.exclusions)
        entry = result["locations"][0]
        self.assertEqual(entry["content_class"], "DLC")
        self.assertEqual(entry["rewards"][0]["content_class"], "BASE")
        self.assertEqual(entry["package_path"], row["map_package"])

    # Check world map paths support installed
    def test_world_map_paths_support_installed_map_trees_and_reject_traversal(self) -> None:
        self.assertTrue(is_world_map_package("/Game/Map/MainWorld/DlcWorld/_Generated_/DlcSlot"))
        self.assertTrue(is_world_map_package("/Game/Map/BossStage/Sublevels/BossStage_Quest00"))
        self.assertTrue(is_world_map_package("/Game/User/Cosmo_Maruyama/C_Prison"))
        self.assertTrue(is_world_map_package("/Game/Cinematics/CinematicScene/MemoryRemnant/Memory_e20110"))
        self.assertFalse(is_world_map_package("/Game/Map/../Blueprint/NotAMap"))
        self.assertFalse(is_world_map_package("/Game/Blueprint/Pickup/BP_Pickup"))

    # Check world import reject map package
    def test_world_import_rejects_map_package_that_does_not_match_map_file(self) -> None:
        row = _world_row(
            "DlcSlot.umap", 4, 0, "/Game/Blueprint/Article/Item/Support/DA_BaseItem",
            "GROUND_PICKUP", "",
        )
        row["map_package"] = "/Game/Map/MainWorld/DlcWorld/_Generated_/AnotherMap"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "world.csv"
            _write_csv(source, [row])
            with self.assertRaisesRegex(ValueError, "matching its source file"):
                import_world_loot(source, set(), self.exclusions)

    # Check world import preserve exact numbered
    def test_world_import_preserves_exact_numbered_article_fnames(self) -> None:
        row = _world_row(
            "Numbered.umap", 9, 0,
            "/Game/Blueprint/Article/Item/Support/DA_Item_1", "GROUND_PICKUP", "",
        )
        row.update({
            "article_package_literal_fname": "/Game/Blueprint/Article/Item/Support/DA_Item",
            "article_package_fname_number": "2",
            "article_asset_literal_fname": "DA_Item",
            "article_asset_fname_number": "2",
        })
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "world.csv"
            _write_csv(source, [row])
            result = import_world_loot(source, set(), self.exclusions)
        identity = result["locations"][0]["rewards"][0]["soft_object_path_identity"]
        self.assertEqual(identity, {
            "package_name": {
                "value": "/Game/Blueprint/Article/Item/Support/DA_Item", "number": 2,
            },
            "asset_name": {"value": "DA_Item", "number": 2},
        })

    # Check world import support inherited reward
    def test_world_import_supports_inherited_reward_on_physical_chest(self) -> None:
        row = _world_row(
            "CDOChest.umap", 19, 0,
            "/Game/Blueprint/Article/Item/Ammo/DA_Item_AmmoRifle", "TREASURE_BOX", "",
            export_name="BP_it00_002_A_0_C_3_70b10c9565b132e0",
        )
        row.update({
            "map_package": "/Game/Map/MainWorld/DlcWorld/_Generated_/CDOChest",
            "actor_class": "BP_it00_002_A_0_C",
            "destination_kind": "INSTANCE_ARTICLE_BULK_OVERRIDE",
            "override_cdo_package": "/Game/Environment/Item/it00/it00_002/BP_it00_002_A_0",
            "override_cdo_export_index": "3",
            "override_cdo_export_name": "Default__BP_it00_002_A_0_C",
            "override_class_package": "/Game/Environment/Item/it00/it00_002/BP_it00_002_A_0",
            "override_parent_blueprint_packages": "/Game/Blueprint/FieldGimmick/TreasureBox/BP_TreasureBox_Base",
        })
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "world.csv"
            _write_csv(source, [row])
            imported = import_world_loot(source, set(), self.exclusions)
        parsed = CatalogStore._parse_world_location(
            imported["locations"][0], frozenset(), self.exclusions,
        )
        self.assertEqual(parsed.destination_kind, "INSTANCE_ARTICLE_BULK_OVERRIDE")
        self.assertEqual(parsed.location_type, "NORMAL_WORLD_LOOT")
        self.assertEqual(parsed.actor_class, "BP_it00_002_A_0_C")
        self.assertEqual(parsed.rewards[0].article_path, "/Game/Blueprint/Article/Item/Ammo/DA_Item_AmmoRifle")
        self.assertEqual(parsed.instance_override_source.cdo_export_index, 3)
        plan = build_world_loot_randomization_plan((parsed,), "instance-override-regression")
        self.assertEqual(len(plan.assignments), 1)
        self.assertFalse(plan.locked_locations)

    # Check world import reject unresolved unprotected
    def test_world_import_rejects_unresolved_or_unprotected_missing_fname_identity(self) -> None:
        row = _world_row(
            "BadFName.umap", 1, 0,
            "/Game/Blueprint/Article/Item/Support/DA_Item", "GROUND_PICKUP", "",
        )
        row["article_package_literal_fname"] = "/Game/Blueprint/Article/Item/Support/DA_Other"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "world.csv"
            _write_csv(source, [row])
            with self.assertRaisesRegex(ValueError, "does not resolve exactly"):
                import_world_loot(source, set(), self.exclusions)

            row["article_package_literal_fname"] = ""
            row["article_package_fname_number"] = ""
            row["article_asset_literal_fname"] = ""
            row["article_asset_fname_number"] = ""
            row["progression_risk"] = "LOW"
            _write_csv(source, [row])
            with self.assertRaisesRegex(ValueError, "must be explicitly protected"):
                import_world_loot(source, set(), self.exclusions)

    # Check known item blueprint class resolve
    def test_known_item_blueprint_class_resolves_legacy_unresolved_world_rows(self) -> None:
        rows = [
            _world_row(
                "Pickup.umap", 1, 0, "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpWeaponA_01",
                "WORLD_LOOT_UNRESOLVED", "",
                export_name="BP_it00_001_A_0_C_UAID_A8A159A4A14BA15202_1",
            ),
            _world_row(
                "Pickup.umap", 2, 0, "/Game/Blueprint/Article/Item/Support/DA_Scripted",
                "WORLD_LOOT_UNRESOLVED", "",
                export_name="BP_it00_001_A_0_C_UAID_A8A159A4A14BA15202_2",
            ),
        ]
        rows[1]["scanner_flag"] = "BP_PickupItem_WithFlagChange"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "world.csv"
            _write_csv(source, rows)
            result = import_world_loot(source, set(), self.exclusions)

        pickup, scripted = result["locations"]
        self.assertEqual(pickup["actor_category"], "GROUND_PICKUP")
        self.assertFalse(pickup["protected"])
        self.assertEqual(pickup["protection_reasons"], [])
        self.assertTrue(scripted["protected"])
        self.assertEqual(scripted["actor_category"], "WORLD_LOOT_UNRESOLVED")
        self.assertIn("flag-associated pickup", scripted["protection_reasons"])

    # Check hounds is not injected extra
    def test_hounds_is_not_injected_as_an_extra_protected_jail(self) -> None:
        protections = read_explicit_world_source_protections(
            _ROOT / "data" / "jails" / "current" / "jails.json",
        )
        slot = ("/game/map/mainworld/mainworld/_generated_/bdvi2f84sbj124t1jmdjbrawy", 60)
        self.assertNotIn(slot, protections)
        row = _world_row(
            "BDVI2F84SBJ124T1JMDJBRAWY.umap", 60, 0,
            "/Game/Blueprint/Article/Jail/DA_Jail_Hounds", "WORLD_LOOT_UNRESOLVED", "",
            export_name="BP_it00_001_A_0_C_UAID_A8A159A4A14BED8402_1127248614_e9d8caf26e11788b",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "world.csv"
            _write_csv(source, [row])
            imported = import_world_loot(source, set(), self.exclusions, protections)
        location = imported["locations"][0]
        self.assertEqual(location["actor_category"], "GROUND_PICKUP")
        self.assertFalse(location["protected"])
        self.assertEqual(location["protection_reasons"], [])

    # Check curated stinger exception randomizes article
    def test_curated_stinger_exception_randomizes_article_and_preserves_flag_metadata(self) -> None:
        jail_path = _ROOT / "data" / "jails" / "current" / "jails.json"
        modes = {
            slot: mode for slot, mode in read_explicit_scripted_reward_modes(jail_path).items()
            if mode == "ARTICLE_ONLY_PRESERVE_FLAGS"
        }
        self.assertEqual(len(modes), 2)
        source_csv = _ROOT / "data" / "catalogs" / "current" / "world_loot_catalog.csv"
        with source_csv.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = reader.fieldnames
            rows = [
                row for row in reader
                if row["map_package"].casefold() in {
                    "/game/map/mainworld/mainworld/_generated_/61y5lav87nwvd213f35x2vk87",
                    "/game/map/mainworld/dlcworld/_generated_/3z2kpigcuslgydyc5dqht6afa",
                }
                and row["export_index"] in {"272", "42"}
            ]
        self.assertIsNotNone(fieldnames)
        self.assertEqual(len(rows), 2)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "stinger.csv"
            with source.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            imported = import_world_loot(
                source,
                set(),
                self.exclusions,
                scripted_reward_modes=modes,
            )
        by_package = {location["package_path"].casefold(): location for location in imported["locations"]}
        self.assertEqual(set(by_package), set(modes_key[0] for modes_key in modes))
        for location in by_package.values():
            self.assertFalse(location["protected"])
            self.assertEqual(location["protection_reasons"], [])
            self.assertEqual(location["scripted_reward_mode"], "ARTICLE_ONLY_PRESERVE_FLAGS")
            self.assertEqual(location["rewards"][0]["article_path"], "/Game/Blueprint/Article/Jail/DA_Jail_Stinger")
            self.assertEqual((location["rewards"][0]["count"], location["rewards"][0]["notification"]), (1, True))

    # Check curated flag pickup articles randomize
    def test_curated_flag_pickup_articles_randomize_reward_only(self) -> None:
        source_dir = _ROOT / "data" / "catalogs" / "current"
        jail_path = _ROOT / "data" / "jails" / "current" / "jails.json"
        imported = import_world_loot(
            source_dir / "world_loot_catalog.csv",
            set(),
            self.exclusions,
            read_explicit_world_source_protections(jail_path),
            read_explicit_scripted_reward_modes(jail_path),
        )
        targets = [
            item for item in imported["locations"]
            if item.get("scripted_reward_mode") == "ARTICLE_ONLY_PRESERVE_FLAGS"
            and item["rewards"][0]["article_path"].casefold()
            in {path.casefold() for path in FLAG_PICKUP_ARTICLE_ONLY_ALLOWLIST}
        ]
        self.assertEqual(len(targets), 38)
        self.assertEqual(
            {item["rewards"][0]["article_path"].casefold() for item in targets},
            {path.casefold() for path in FLAG_PICKUP_ARTICLE_ONLY_ALLOWLIST},
        )
        self.assertTrue(all(not item["protected"] and not item["protection_reasons"] for item in targets))
        self.assertEqual(
            {item["rewards"][0]["count"] for item in targets if item["rewards"][0]["count"] > 1},
            {2},
        )

        parsed = [
            CatalogStore._parse_world_location(item, frozenset(), self.exclusions)
            for item in targets
        ]
        plan = build_world_loot_randomization_plan(parsed, "flag-pickup-allowlist")
        self.assertEqual(len(plan.locked_locations), 0)
        self.assertEqual(len(plan.assignments), 38)
        self.assertEqual(len(plan.mirrored_locations), 19)
        for assignment in plan.assignments:
            self.assertEqual(assignment.assigned_rewards[0].count, assignment.destination.rewards[0].count)
            self.assertEqual(assignment.assigned_rewards[0].notification, assignment.destination.rewards[0].notification)

    # Check world mirror inherits more restrictive
    def test_world_mirror_inherits_the_more_restrictive_protection(self) -> None:
        base_package = "/Game/Map/MainWorld/MainWorld/_Generated_/BaseMirror"
        mirror_package = "/Game/Map/MainWorld/DlcWorld/_Generated_/DlcMirror"
        base = _world_row(
            "BaseMirror.umap", 9, 0,
            "/Game/Blueprint/Article/Item/Support/DA_MirrorReward", "GROUND_PICKUP", "",
        )
        mirror = dict(base)
        base.update({
            "map_package": base_package,
            "unique_save_id": "shared-save-id",
            "mirror_of_package_path": "",
            "mirror_of_export_index": "",
        })
        mirror.update({
            "map_file": "DlcMirror.umap",
            "map_package": mirror_package,
            "unique_save_id": "shared-save-id",
            "mirror_of_package_path": base_package,
            "mirror_of_export_index": "9",
        })
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "world.csv"
            _write_csv(source, [base, mirror])
            imported = import_world_loot(source, {"basemirror.umap"}, self.exclusions)
        by_package = {entry["package_path"]: entry for entry in imported["locations"]}
        self.assertTrue(by_package[base_package]["protected"])
        self.assertTrue(by_package[mirror_package]["protected"])
        self.assertEqual(
            by_package[base_package]["protection_reasons"],
            ["unsafe or unparsed package"],
        )
        self.assertEqual(
            by_package[mirror_package]["protection_reasons"],
            ["unsafe or unparsed package"],
        )

    # Check material import keep unreviewed classification
    def test_material_import_keeps_unreviewed_classification_unknown(self) -> None:
        imported = import_materials(_ROOT / "data" / "catalogs" / "current" / "materials_catalog.csv")
        self.assertEqual(len(imported["locations"]), 734)
        self.assertTrue(all(item["where_content_class"] == "UNKNOWN" for item in imported["locations"]))
        self.assertTrue(all(item["resource_content_class"] == "UNKNOWN" for item in imported["locations"]))
        self.assertTrue(all(item["reward_content_class"] == "UNKNOWN" for item in imported["locations"]))
        self.assertTrue(all(item["reward"]["content_class"] == "UNKNOWN" for item in imported["locations"]))

    # Check shop import use actual product
    def test_shop_import_uses_actual_product_article_and_exact_exclusions(self) -> None:
        rows = [
            _shop_row("/Game/Blueprint/Article/Item/Important/DA_Recipe", "DA_Recipe", True),
            _shop_row(self.exclusions.quest_flag_exclusions[0], "DA_QuestReward", True),
            _shop_row(self.exclusions.keys[0], "DA_CastleKey_e00200", False),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "shops.csv"
            _write_csv(source, rows)
            result = import_shops(source, self.exclusions)
        products = result["products"]
        self.assertFalse(products[0]["protected"])
        self.assertIsNone(products[0]["protection_reason"])
        self.assertEqual(products[1]["protection_reason"], "QUEST_FLAG_EXCLUDED")
        self.assertEqual(products[2]["protection_reason"], "KEY_EXCLUDED")
        self.assertTrue(all(product["what_content_class"] == "UNKNOWN" for product in products))

    # Check shop import preserve explicit what
    def test_shop_import_preserves_an_explicit_what_class(self) -> None:
        row = _shop_row("/Game/Blueprint/Article/Item/Support/DA_Test", "DA_Test", False)
        row["what_content_class"] = "BASE"
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "shops.csv"
            _write_csv(source, [row])
            result = import_shops(source, self.exclusions)
        self.assertEqual(result["products"][0]["what_content_class"], "BASE")


# List world row
def _world_row(
    map_file: str,
    export_index: int,
    reward_index: int,
    item_path: str,
    location_type: str,
    progression_reason: str,
    *,
    export_name: str | None = None,
) -> dict[str, str]:
    return {
        "map_file": map_file,
        "export_index": str(export_index),
        "export_name": export_name or f"BP_Pickup_UAID_{map_file}_{export_index}",
        "location_type": location_type,
        "reward_index": str(reward_index),
        "item_path": item_path,
        "count": "1",
        "bNotification": "True",
        "unique_save_id": f"{map_file}-{export_index}",
        "progression_risk": "REVIEW" if progression_reason else "LOW",
        "progression_reason": progression_reason,
        "scanner_flag": "",
        "scanner_scenario": "",
        "article_package_literal_fname": item_path,
        "article_package_fname_number": "0",
        "article_asset_literal_fname": item_path.rsplit("/", 1)[-1],
        "article_asset_fname_number": "0",
    }


# List shop row
def _shop_row(path: str, object_name: str, legacy_locked: bool) -> dict[str, str]:
    return {
        "table": "NORMAL",
        "shop_row": "Shop_Test",
        "item_path": path,
        "item_object": object_name,
        "price": "100",
        "uses_stock": "True",
        "stock": "1",
        "is_important_locked": "True" if legacy_locked else "False",
    }


# Write csv
def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
