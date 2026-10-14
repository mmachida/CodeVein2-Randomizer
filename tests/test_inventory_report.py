from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.content_availability import PackageInventory, package_path_from_object_path
from cv2randomizer.game.inventory_report import export_randomizer_inventory
from cv2randomizer.game.renewable_ingredients import renewable_material_locations


class InventoryReportTests(unittest.TestCase):
    # Check export is deterministic covers current
    def test_export_is_deterministic_and_covers_current_production_catalogs(self) -> None:
        options = GenerationOptions(
            randomize_shops=True,
            randomize_enemy_drops=True,
            randomize_healing=True,
            randomize_materials=True,
            mix_shops_with_world=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = export_randomizer_inventory(root / "first", options=options, seed="inventory-audit")
            second = export_randomizer_inventory(root / "second", options=options, seed="inventory-audit")

            names = (
                "randomizer_item_inventory.csv",
                "randomizer_item_inventory.json",
                "randomizer_article_summary.csv",
                "randomizer_inventory_summary.csv",
            )
            for name in names:
                self.assertEqual((root / "first" / name).read_bytes(), (root / "second" / name).read_bytes())

            self.assertEqual(first.total_rows, second.total_rows)
            self.assertEqual(first.status_counts, second.status_counts)
            self.assertTrue(all(first.validation.values()), first.validation)

            with (root / "first" / names[0]).open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            with (root / "first" / names[1]).open(encoding="utf-8") as stream:
                document = json.load(stream)
            with (root / "first" / names[3]).open(encoding="utf-8-sig", newline="") as stream:
                summary_rows = list(csv.DictReader(stream))

            self.assertEqual(len(rows), first.total_rows)
            self.assertEqual(document["seed"], "inventory-audit")
            self.assertEqual(document["generation_options"], options.to_dict())
            self.assertNotIn("created_at", document)
            quest_grants = [row for row in rows if row["quest_grant_audit"] == "true"]
            quest_audit_only = [row for row in quest_grants if row["category"] == "QUEST_REWARD_AUDIT"]
            # Two mirrored Iris task exports are one logical grant each and
            # therefore share the existing World Loot inventory rows.
            self.assertEqual(len(quest_grants), 242)
            self.assertEqual(len(quest_audit_only), 91)
            self.assertTrue(all(
                row["status"] in {"UNSUPPORTED", "PROTECTED", "EXCLUDED"}
                and row["included_in_randomizer"] == "false"
                and row["eligible_as_source"] == "false"
                and row["eligible_as_destination"] == "false"
                and row["is_one_time"] == "false"
                for row in quest_audit_only
            ))
            self.assertEqual(document["quest_grant_audit"]["quest_packages_inspected"], 1088)
            self.assertEqual(document["quest_grant_audit"]["matched_existing_rows"], 153)
            self.assertEqual(document["quest_grant_audit"]["added_audit_rows"], 91)
            iris_booster_rows = [
                row for row in quest_grants
                if row["article_name"] in {
                    "DA_Booster_CharaEpisodeIris_1",
                    "DA_Booster_CharaEpisodeIris_2",
                }
            ]
            self.assertEqual(len(iris_booster_rows), 2)
            self.assertTrue(all(row["category"] == "QUEST_REWARD" for row in iris_booster_rows))
            added_quest_rows = [
                row for row in rows
                if row["location_id"].startswith("quest_reward_")
            ]
            self.assertEqual(len(added_quest_rows), 46)
            self.assertTrue(all(
                row["category"] == "QUEST_REWARD"
                and row["status"] == "INCLUDED"
                and row["included_in_randomizer"] == "true"
                and row["is_quest_item"] == "false"
                and not row["article_name"].casefold().startswith("da_item_important")
                for row in added_quest_rows
            ))
            josee_quest_item = next(
                row for row in quest_grants
                if row["resolved_article"] == "/Game/Blueprint/Article/Item/DA_Item_Important_Josee_RuneBlade"
            )
            self.assertEqual(josee_quest_item["package"], "/Game/Quest/MainEpisode_Josee/QST_MainEpisode_Josee_e20520")
            self.assertEqual(josee_quest_item["status"], "PROTECTED")
            self.assertEqual(josee_quest_item["exclusion_reason"], "QUEST_ITEM")
            lise_ornament = next(
                row for row in quest_grants
                if row["resolved_article"] == "/Game/Blueprint/Article/Item/DA_Important_LiseHairOrnament_Name"
            )
            self.assertEqual(lise_ornament["status"], "PROTECTED")
            self.assertEqual(lise_ornament["exclusion_reason"], "QUEST_ITEM")
            lise_world_pickup = next(
                row for row in rows
                if row["resolved_article"] == "/Game/Blueprint/Article/Item/DA_Important_LiseHairOrnament_Name"
                and row["category"] == "GROUND_PICKUP"
            )
            self.assertEqual(lise_world_pickup["status"], "PROTECTED")
            self.assertEqual(lise_world_pickup["exclusion_reason"], "QUEST_ITEM")
            executioner_alternate = next(
                row for row in quest_audit_only
                if row["article_name"] == "DA_ExecutionSword"
            )
            self.assertEqual(executioner_alternate["package"], "/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00560")
            self.assertEqual(executioner_alternate["status"], "EXCLUDED")
            self.assertEqual(executioner_alternate["exclusion_reason"], "LOGICAL_REWARD_MIRROR")
            self.assertIn("not an additional item", executioner_alternate["exclusion_detail"])
            forma_mirror = next(
                row for row in rows
                if row["category"] == "BEQUEATHED_FORMAE"
                and row["subcategory"] == "ALTERNATE_QUEST_GRANT_PATH"
                and row["article_name"] == "DA_ExecutionSword"
            )
            self.assertEqual(forma_mirror["status"], "EXCLUDED")
            self.assertEqual(forma_mirror["exclusion_reason"], "LOGICAL_REWARD_MIRROR")
            self.assertEqual(forma_mirror["eligible_as_source"], "false")
            self.assertEqual(forma_mirror["eligible_as_destination"], "false")
            hot_spring_rows = [
                row for row in rows
                if row["location_id"].startswith("hot_spring_")
            ]
            self.assertEqual(len(hot_spring_rows), 19)
            self.assertTrue(all(
                row["category"] == "QUEST_REWARD"
                and row["status"] == "INCLUDED"
                and row["included_in_randomizer"] == "true"
                and row["eligible_as_destination"] == "true"
                and row["quest_grant_audit"] == "true"
                and row["is_quest_item"] == "false"
                for row in hot_spring_rows
            ))
            repeated_recipe_rows = [
                row for row in hot_spring_rows
                if row["location_id"].startswith((
                    "hot_spring_forest02:", "hot_spring_forest03:", "hot_spring_forest04:",
                ))
            ]
            self.assertEqual(len(repeated_recipe_rows), 6)
            self.assertTrue(all(row["eligible_as_source"] == "false" for row in repeated_recipe_rows))
            self.assertTrue(all(
                "one-time unlocks" in row["notes"]
                for row in hot_spring_rows
            ))
            trader_recipe = next(
                row for row in rows
                if row["category"] == "NPC_TRADER"
                and row["article_name"] == "DA_Item_Important_Recipe_FoodSourPork"
            )
            self.assertEqual(trader_recipe["status"], "INCLUDED")
            self.assertEqual(trader_recipe["included_in_randomizer"], "true")
            self.assertTrue(all(row["available_in_installed_game"] == "" for row in rows))
            self.assertTrue(all(row["status"] in {"INCLUDED", "EXCLUDED", "PROTECTED", "DISABLED", "UNSUPPORTED", "UNAVAILABLE"} for row in rows))
            self.assertTrue(all(
                row["included_in_randomizer"] == str(row["status"] == "INCLUDED").lower()
                for row in rows
            ))

            magmell_tools = [
                row for row in rows
                if row["category"] == "SHOP" and row["actor"] == "Shop_Jadwiga_Item"
            ]
            self.assertEqual(len(magmell_tools), 56)
            self.assertTrue(all(row["status"] == "PROTECTED" for row in magmell_tools))
            self.assertTrue(all(row["exclusion_reason"] == "MAGMELL_TOOL_SHOP" for row in magmell_tools))

            dex_grant = next(
                row for row in rows
                if row["location_id"] == "jadwiga_servant_booster_dex_reward"
            )
            self.assertEqual(dex_grant["category"], "DIRECT_REWARD")
            self.assertEqual(dex_grant["status"], "INCLUDED")
            self.assertEqual(dex_grant["exclusion_reason"], "")
            self.assertEqual(dex_grant["exclusion_detail"], "")
            self.assertEqual(dex_grant["controlled_by_option"], "ALWAYS_ON_WORLD_LOOT")
            self.assertEqual(dex_grant["eligible_as_source"], "true")
            self.assertEqual(dex_grant["eligible_as_destination"], "true")

            scenario_reports = [
                row for row in rows
                if row["article_name"] in {"DA_Report_A", "DA_Report_B", "DA_Report_C"}
                and row["package"] in {
                    "/Game/Map/MainWorld/MainWorld/_Generated_/9LFQFB2HAT6FVDAA311S0L2ZD",
                    "/Game/Map/MainWorld/DlcWorld/_Generated_/AJH345RIDQ5AW61DCGZIYS6DT",
                }
            ]
            self.assertEqual(len(scenario_reports), 6)
            self.assertTrue(all(row["status"] == "INCLUDED" for row in scenario_reports))
            self.assertTrue(all(row["exclusion_reason"] == "" for row in scenario_reports))
            self.assertTrue(all(
                "ARTICLE_ONLY_PRESERVE_SCENARIO" in row["notes"] for row in scenario_reports
            ))

            confirmed_quest_articles = {
                *(f"/Game/Blueprint/Article/Item/Important/DA_Item_Important_EngravedWeapon_{variant}" for variant in ("01", "02", "03")),
                *(f"/Game/Blueprint/Article/Item/Important/DA_Item_Important_RiftBook_{variant}" for variant in ("01", "02", "03", "04")),
                *(f"/Game/Blueprint/Article/Item/Important/DA_Item_Important_Spice_{variant}" for variant in ("01", "02", "03")),
            }
            confirmed_quest_rows = [
                row for row in rows if row["resolved_article"] in confirmed_quest_articles
            ]
            self.assertEqual(len(confirmed_quest_rows), 20)
            self.assertTrue(all(row["is_quest_item"] == "true" for row in confirmed_quest_rows))
            self.assertTrue(all(row["is_progression_item"] == "true" for row in confirmed_quest_rows))
            self.assertTrue(all(row["status"] in {"PROTECTED", "UNSUPPORTED"} for row in confirmed_quest_rows))
            self.assertTrue(all(row["exclusion_reason"] == "QUEST_ITEM" for row in confirmed_quest_rows))
            self.assertTrue(all(row["included_in_randomizer"] == "false" for row in confirmed_quest_rows))
            self.assertTrue(all(row["eligible_as_source"] == "false" for row in confirmed_quest_rows))
            self.assertTrue(all(row["eligible_as_destination"] == "false" for row in confirmed_quest_rows))

            categories = {row["category"] for row in rows}
            self.assertTrue({"CHEST", "GROUND_PICKUP", "INITIAL_INVENTORY", "BLOOD_CODE", "BOOSTER", "BOOSTER_CRAFTING", "SHOP", "NPC_TRADER", "ENEMY_DROP", "ONE_TIME_ENEMY_REWARD", "JAIL", "DEFENSIVE_GADGET", "BEQUEATHED_FORMAE", "HEALING_UPGRADE", "RENEWABLE_INGREDIENT"}.issubset(categories))
            self.assertTrue(any(row["scope"] == "SYSTEM_OWNER" and row["category"] == "WORLD_LOOT" for row in summary_rows))
            self.assertEqual(sum(row["category"] == "BLOOD_CODE" for row in rows), 79)
            self.assertEqual(sum(row["category"] == "BOOSTER" for row in rows), 102)
            self.assertEqual(sum(row["category"] == "BOOSTER_CRAFTING" for row in rows), 43)
            shared_enemy_drops = [
                row for row in rows
                if row["category"] == "ENEMY_DROP" and row["subcategory"] == "SHARED_BOSS_NORMAL"
            ]
            self.assertEqual(len(shared_enemy_drops), 63)
            self.assertTrue(all(row["status"] == "INCLUDED" for row in shared_enemy_drops))
            self.assertTrue(all(row["included_in_randomizer"] == "true" for row in shared_enemy_drops))
            self.assertTrue(all(row["intrinsically_eligible"] == "true" for row in shared_enemy_drops))
            self.assertTrue(all(row["randomizer_pool"] == "ENEMY_DROP_ONLY" for row in shared_enemy_drops))
            self.assertTrue(all("shared_by_normal_and_boss_miniboss" in row["notes"] for row in shared_enemy_drops))
            self.assertFalse(any(row["exclusion_reason"] == "UNSUPPORTED_SHARED_ENCOUNTER_ROW" for row in rows))
            self.assertEqual(sum(row["category"] == "RENEWABLE_INGREDIENT" for row in rows), 2253)
            self.assertEqual(sum(row["subcategory"] == "MATERIALS_CATALOGLED_RENEWABLE" for row in rows), 2253)
            renewable = [row for row in rows if row["category"] == "RENEWABLE_INGREDIENT"]
            self.assertEqual(len(renewable), 2253)
            self.assertTrue(all(row["status"] == "INCLUDED" for row in renewable))
            self.assertTrue(all(row["included_in_randomizer"] == "true" for row in renewable))
            self.assertTrue(all(row["intrinsically_eligible"] == "true" for row in renewable))
            self.assertTrue(all(row["enabled_by_current_config"] == "true" for row in renewable))
            self.assertTrue(all(row["system_owner"] == "MATERIALS" for row in renewable))
            self.assertTrue(all(row["randomizer_pool"] == "INGREDIENTS" for row in renewable))

            target_material_classes = {
                "BP_it20_001_A1_0_C", "BP_it20_001_B1_0_C", "BP_it20_001_C1_0_C",
                "BP_it20_001_C2_0_C", "BP_it21_008_A_0_C", "BP_it21_008_A_1_C",
                "BP_it21_012_A_0_C", "BP_it22_004_X1_0_C",
            }
            target_material_rows = [
                row for row in renewable if row["actor_class"] in target_material_classes
            ]
            self.assertEqual(len(target_material_rows), 198)
            self.assertTrue(all(row["included_in_randomizer"] == "true" for row in target_material_rows))
            self.assertFalse(any(
                row["subcategory"] == "UNSUPPORTED_CDO_ARTICLE_BULK"
                and row["actor_class"] in target_material_classes
                for row in rows
            ))

            protected_key_rows = [
                row for row in rows
                if row["article_name"] in {"DA_Item_Important_StorageKey", "DA_Item_Important_WarehouseKey"}
            ]
            self.assertTrue(protected_key_rows)
            self.assertTrue(all(row["status"] == "PROTECTED" for row in protected_key_rows))
            self.assertTrue(all(row["exclusion_reason"] == "KEY_ITEM" for row in protected_key_rows))

    # Check cache availability check map class
    def test_cache_availability_checks_map_class_and_article_for_all_ingredients(self) -> None:
        catalogs = CatalogStore().load("current")
        materials = renewable_material_locations(catalogs.world_loot)
        packages = {
            path
            for item in materials
            for path in (
                item.package_path,
                item.resource_class_package,
                package_path_from_object_path(item.reward.article_path),
            )
        }
        cache_inventory = PackageInventory.create(packages)

        with tempfile.TemporaryDirectory() as temporary:
            result = export_randomizer_inventory(
                Path(temporary) / "report",
                options=GenerationOptions(randomize_materials=True),
                seed="ingredient-cache-inventory",
                cache_inventory=cache_inventory,
            )
            with result.files[0].open(encoding="utf-8-sig", newline="") as stream:
                rows = [row for row in csv.DictReader(stream) if row["category"] == "RENEWABLE_INGREDIENT"]

        self.assertEqual(len(rows), len(materials))
        self.assertTrue(all(row["status"] == "INCLUDED" for row in rows))
        self.assertTrue(all(row["available_in_cache"] == "true" for row in rows))
        self.assertTrue(all(row["randomizer_pool"] == "INGREDIENTS" for row in rows))


if __name__ == "__main__":
    unittest.main()
