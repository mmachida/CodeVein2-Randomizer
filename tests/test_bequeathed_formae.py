from __future__ import annotations

import json
import random
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from cv2randomizer.game.bequeathed_formae_catalog import (
    BequeathedFormaCatalogError,
    BequeathedFormaCatalogStore,
)
from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.randomizer.bequeathed_formae import build_bequeathed_formae_plan


ROOT = Path(__file__).resolve().parents[1]


class BequeathedFormaCatalogTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.data_dir = ROOT / "data" / "bFormae" / "current"
        self.catalog = BequeathedFormaCatalogStore(self.data_dir).load()

    # Check catalog inventory current world shop
    def test_catalog_inventory_and_current_world_shop_sources(self) -> None:
        self.assertEqual(len(self.catalog.items), 10)
        self.assertEqual(Counter(item.content_class for item in self.catalog.items), {"BASE": 8, "DLC": 2})
        self.assertEqual(Counter(item.source_class for item in self.catalog.items), {
            "WORLD": 1, "SHOP": 7, "DIRECT_QUEST": 2,
        })
        self.assertEqual(self.catalog.direct_physical_grant_count, 3)
        self.assertFalse(any(item.object.startswith("DA_Buddy_") for item in self.catalog.items))
        direct = {item.object: item for item in self.catalog.direct_rewards}
        lou = direct["DA_LouAxe"]
        self.assertEqual(lou.logical_reward_group, "BEQUEATHED_LOU_AXE")
        self.assertEqual(lou.world_loot_destination_id, "bequeathed_lou_axe_magmell_quest")
        self.assertEqual(len(lou.direct_grants), 1)
        self.assertEqual(
            [(grant.quest_asset, grant.json_export_index, grant.count, grant.notification) for grant in lou.direct_grants],
            [("/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10365", 27, 1, True)],
        )
        executioner = direct["DA_ExecutionSword"]
        self.assertEqual(executioner.logical_reward_group, "BEQUEATHED_EXECUTIONER")
        self.assertEqual(len(executioner.direct_grants), 2)
        self.assertEqual(executioner.world_loot_destination_id, "bequeathed_executioner_lyle_quest")
        self.assertEqual(tuple(item.object for item in self.catalog.separate_direct_rewards), ())
        self.assertEqual(
            tuple(item.object for item in self.catalog.world_loot_direct_rewards),
            ("DA_ExecutionSword", "DA_LouAxe"),
        )
        self.assertEqual(
            [(grant.quest_asset, grant.json_export_index, grant.count, grant.notification) for grant in executioner.direct_grants],
            [
                ("/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00382", 5, 1, True),
                ("/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00560", 19, 1, True),
            ],
        )
        current = CatalogStore(ROOT / "data" / "catalogs").load("current")
        self.catalog.validate_sources(current.world_loot, current.shops)

    # Check available direct forma keep present
    def test_available_direct_forma_keeps_present_routes_when_an_alternate_is_missing(self) -> None:
        executioner = next(item for item in self.catalog.direct_rewards if item.object == "DA_ExecutionSword")
        inventory = PackageInventory.create((
            executioner.asset_path,
            executioner.direct_grants[0].quest_asset,
        ))

        available = self.catalog.available_items(inventory)

        self.assertEqual(tuple(item.object for item in available), ("DA_ExecutionSword",))
        self.assertEqual(len(available[0].direct_grants), 1)
        self.assertEqual(available[0].direct_grants[0], executioner.direct_grants[0])

    # Check loader reject swapped direct logical
    def test_loader_rejects_swapped_direct_logical_group_cardinality(self) -> None:
        document = json.loads((self.data_dir / "bequeathed_formae.json").read_text(encoding="utf-8"))
        direct_items = [item for item in document["items"] if item["source_class"] == "DIRECT_QUEST"]
        lou = next(item for item in direct_items if item["object"] == "DA_LouAxe")
        executioner = next(item for item in direct_items if item["object"] == "DA_ExecutionSword")
        lou["direct_grants"], executioner["direct_grants"] = executioner["direct_grants"], lou["direct_grants"]
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "bequeathed_formae.json").write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(BequeathedFormaCatalogError, "DA_LouAxe must map"):
                BequeathedFormaCatalogStore(data_dir).load()

    # Check stale world shop source metadata
    def test_stale_world_and_shop_source_metadata_fails_closed(self) -> None:
        current = CatalogStore(ROOT / "data" / "catalogs").load("current")
        world_item = self.catalog.world_sources[0]
        world_source = world_item.world_source
        canonical_world_location = next(
            location for location in current.world_loot
            if location.package_path == world_source.package_path and location.export_index == world_source.export_index
        )
        with self.assertRaisesRegex(BequeathedFormaCatalogError, "exactly the researched vanilla source"):
            self.catalog.validate_sources((*current.world_loot, replace(canonical_world_location, export_index=9999)), current.shops)

        shop_item = self.catalog.shop_sources[0]
        canonical_product = next(product for product in current.shops if product.item_object == shop_item.object)
        with self.assertRaisesRegex(BequeathedFormaCatalogError, "exactly the researched vanilla source"):
            self.catalog.validate_sources(current.world_loot, (*current.shops, canonical_product))

        wrong_world = tuple(
            replace(location, protected=True)
            if location.package_path == world_source.package_path
            and location.export_index == world_source.export_index
            else location
            for location in current.world_loot
        )
        with self.assertRaisesRegex(BequeathedFormaCatalogError, "identity or protection"):
            self.catalog.validate_sources(wrong_world, current.shops)
        wrong_shops = tuple(
            replace(product, price=product.price + 1)
            if product.item_object == shop_item.object
            else product
            for product in current.shops
        )
        with self.assertRaisesRegex(BequeathedFormaCatalogError, "Shop source payload"):
            self.catalog.validate_sources(current.world_loot, wrong_shops)

    # Check loader reject buddy assets duplicate
    def test_loader_rejects_buddy_assets_and_duplicate_grant_targets(self) -> None:
        document = json.loads((self.data_dir / "bequeathed_formae.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            path = data_dir / "bequeathed_formae.json"
            modified = json.loads(json.dumps(document))
            modified["items"][0]["object"] = "DA_Buddy_Bow"
            modified["items"][0]["asset_path"] = "/Game/Blueprint/Article/Gadget/StandAlone/DA_Buddy_Bow"
            path.write_text(json.dumps(modified), encoding="utf-8")
            with self.assertRaises(BequeathedFormaCatalogError):
                BequeathedFormaCatalogStore(data_dir).load()

            modified = json.loads(json.dumps(document))
            direct_items = [item for item in modified["items"] if item["source_class"] == "DIRECT_QUEST"]
            direct_items[1]["direct_grants"][0]["quest_asset"] = direct_items[0]["direct_grants"][0]["quest_asset"]
            direct_items[1]["direct_grants"][0]["relative_file"] = direct_items[0]["direct_grants"][0]["relative_file"]
            direct_items[1]["direct_grants"][0]["json_export_index"] = direct_items[0]["direct_grants"][0]["json_export_index"]
            direct_items[1]["direct_grants"][0]["ui_export_number"] = direct_items[0]["direct_grants"][0]["ui_export_number"]
            path.write_text(json.dumps(modified), encoding="utf-8")
            with self.assertRaisesRegex(BequeathedFormaCatalogError, "physical quest grant locations must be unique"):
                BequeathedFormaCatalogStore(data_dir).load()


class BequeathedFormaPlannerTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.catalog = BequeathedFormaCatalogStore(ROOT / "data" / "bFormae" / "current").load()

    # Check axe quest is removed singleton
    def test_axe_quest_is_removed_from_the_singleton_forma_pool(self) -> None:
        plan = build_bequeathed_formae_plan(self.catalog, "test-seed-17")
        self.assertEqual(plan, build_bequeathed_formae_plan(self.catalog, "test-seed-17"))
        self.assertEqual(plan.assignments, ())
        self.assertEqual(plan.changed_rewards, 0)
        self.assertTrue(plan.reward_multiset_preserved)

    # Check planner does not consume global
    def test_planner_does_not_consume_global_random_and_disabled_has_no_assignments(self) -> None:
        random.seed(733)
        before = random.getstate()
        build_bequeathed_formae_plan(self.catalog, "isolated-formae-seed")
        self.assertEqual(random.getstate(), before)
        disabled = build_bequeathed_formae_plan(self.catalog, "isolated-formae-seed", enabled=False)
        self.assertEqual(disabled.assignments, ())
        self.assertTrue(disabled.reward_multiset_preserved)


if __name__ == "__main__":
    unittest.main()
