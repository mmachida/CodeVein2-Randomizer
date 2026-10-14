from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cv2randomizer.generation.spoiler import (
    SpoilerEntry,
    spoiler_assignment_summary,
    write_spoiler,
)
from cv2randomizer.game.item_display_names import spoiler_item_category, spoiler_item_display_name
from cv2randomizer.game.shop_tables import ShopTableProduct
from cv2randomizer.generation.pipeline import _shop_spoiler_entry, _world_spoiler_location
from cv2randomizer.randomizer.shops import ShopAssignment
from cv2randomizer.game.catalogs import WorldLootLocation


# Find shop product
def _shop_product(row: str, index: int, path_leaf: str) -> ShopTableProduct:
    path = f"/Game/Blueprint/Article/Weapon/{path_leaf}"
    return ShopTableProduct(
        family="NORMAL",
        shop_row=row,
        product_index=index,
        article_path=path,
        item_object=path_leaf,
        article_path_offset=0,
        article_path_bytes=b"",
        price=10,
        price_offset=0,
        price_bytes=b"\x0a\x00\x00\x00",
        uses_stock=True,
        uses_stock_offset=0,
        uses_stock_bytes=b"\x01",
        stock=1,
        stock_offset=0,
        stock_bytes=b"\x01\x00\x00\x00",
        important=False,
        evaluation_formula="",
        evaluation_raw=b"",
        raw_bytes=b"",
        article_path_number=2,
        item_object_number=2,
    )


class SpoilerTextTests(unittest.TestCase):
    # Check group line order is independent
    def test_group_and_line_order_is_independent_of_discovery_order(self) -> None:
        entries = (
            SpoilerEntry("Shops", "DA_OldB", "DA_NewB", "Z row"),
            SpoilerEntry("World Loot", "DA_OldA", "DA_NewA", "B chest"),
            SpoilerEntry("Shops", "DA_OldA", "DA_NewA", "A row"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.txt"
            second = Path(temporary) / "second.txt"
            write_spoiler(first, entries, seed=42, metadata={"Assignment Hash": "abc", "Algorithm": "v1"})
            write_spoiler(second, reversed(entries), seed=42, metadata={"Assignment Hash": "abc", "Algorithm": "v1"})
            self.assertEqual(first.read_bytes(), second.read_bytes())

    # Check spoiler use stable location identity
    def test_spoiler_uses_stable_location_identity_before_display_labels(self) -> None:
        entries = (
            SpoilerEntry(
                "World Loot", "Old A", "New A", "Zebra chest",
                stable_sort_key="/game/map/map_a#1:Pickup",
            ),
            SpoilerEntry(
                "World Loot", "Old B", "New B", "Apple chest",
                stable_sort_key="/game/map/map_b#1:Pickup",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Spoiler.txt"
            write_spoiler(path, entries, seed=42)
            lines = path.read_text(encoding="utf-8").splitlines()
            zebra_index = lines.index("Zebra chest: Old A -> New A")
            apple_index = lines.index("Apple chest: Old B -> New B")
            self.assertLess(zebra_index, apple_index)

    # Check groups changed items omits identity
    def test_groups_changed_items_and_omits_identity_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Spoiler.txt"
            write_spoiler(path, (
                SpoilerEntry("World Loot", "/Game/Article/DA_Loot", "/Game/Article/DA_Sword", "Old chest"),
                SpoilerEntry("World Loot", "/Game/Article/DA_Keep", "/game/article/da_keep", "Same pickup"),
                SpoilerEntry(
                    "World Loot", "/Game/A/DA_Alias", "/Game/B/DA_Alias", "Alias pickup",
                    randomized_label="da_alias",
                ),
                SpoilerEntry(
                    "Shops", "/Game/Article/DA_Old", "/Game/Article/DA_New",
                    "Mia slot 2", randomized_label="DA_New",
                ),
            ), seed="486692209")
            content = path.read_text(encoding="utf-8")

            self.assertEqual(content.splitlines()[0], "Seed: 486692209")
            self.assertIn("=== World Loot ===", content)
            self.assertIn("Old chest: Loot -> Sword", content)
            self.assertIn("=== Shops ===", content)
            self.assertIn("Mia slot 2: Old -> New", content)
            self.assertNotIn("DA_Loot", content)
            self.assertNotIn("DA_Old", content)
            self.assertNotIn("Same pickup", content)
            self.assertIn("Alias pickup: Alias -> Da Alias", content)
            self.assertNotIn("/Game/", content)
            self.assertNotIn("=== Materials ===", content)

    # Check display names use bundled exact
    def test_display_names_use_bundled_exact_names_and_friendly_fallback(self) -> None:
        self.assertEqual(
            spoiler_item_display_name(
                "/Game/Blueprint/Article/Item/Material/DA_Item_JadwigaCoin_1"
            ),
            "Uncanny Stone",
        )
        self.assertEqual(
            spoiler_item_display_name("DA_Item_MatLevelUpWeaponB_04_1"),
            "Tender Light Stone 4",
        )
        self.assertEqual(
            spoiler_item_display_name("/Game/Article/DA_Item_ExampleMaterial x1"),
            "Example Material",
        )
        self.assertEqual(
            spoiler_item_display_name("/Game/Article/DA_Item_ExampleMaterial x2"),
            "Example Material x2",
        )
        self.assertEqual(
            spoiler_item_display_name(
                "/Game/Blueprint/Article/Item/Important/DA_Item_Important_Recipe_ResUpStall_1"
            ),
            "Recipe: Stall Vaccine",
        )

    # Check precomputed item categories route cross
    def test_precomputed_item_categories_route_cross_pool_rewards(self) -> None:
        self.assertEqual(
            spoiler_item_category("/Game/Blueprint/Article/Booster/Status/DA_Booster_MND_1"),
            "Boosters",
        )
        self.assertEqual(
            spoiler_item_category("/Game/Blueprint/Article/Booster/DLC/DA_Booster_Future_99"),
            "Boosters",
        )
        self.assertEqual(
            spoiler_item_category("/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Warp_2"),
            "Defensive Gadgets",
        )
        self.assertEqual(
            spoiler_item_category("/Game/Blueprint/Article/Jail/DA_Jail_Hounds"),
            "Jails",
        )
        self.assertIsNone(spoiler_item_category("/Game/Blueprint/Article/Item/DA_Unknown"))

    # Check unnamed world quest destinations use
    def test_unnamed_world_and_quest_destinations_use_compact_references(self) -> None:
        world_pickup = WorldLootLocation(
            package_path="/Game/Map/MainWorld/MainWorld/_Generated_/CELL123",
            source_file="CELL123.umap",
            export_index=8,
            export_name="BP_PickupItem_C_UAID_123",
            actor_class="BP_PickupItem_C",
            actor_category="GROUND_PICKUP",
            save_id=None,
            protected=False,
            protection_reasons=(),
            rewards=(),
        )
        josee_reward = replace(
            world_pickup,
            package_path="/Game/Quest/MainEpisode/Josee/QST_MainEpisode_Josee_3",
            export_index=3,
        )

        self.assertEqual(_world_spoiler_location(world_pickup), "Main World pickup #8")
        self.assertEqual(_world_spoiler_location(josee_reward), "Quest Josee, reward #3")

        dlc_pickup = replace(
            world_pickup,
            package_path="/Game/Map/MainWorld/DlcWorld/_Generated_/87L36U46PQVVEVW14ZD0AS1LX",
            export_index=28,
        )
        self.assertEqual(_world_spoiler_location(dlc_pickup), "DLC World pickup #28")

        healing_pickup = replace(
            world_pickup,
            package_path="/Game/Map/MainWorld/MainWorld/_Generated_/069OWQSMIDR362TF0Z7IRNEE7",
            export_index=46,
            display_name=(
                "Golden Seed — "
                "/Game/Map/MainWorld/MainWorld/_Generated_/069OWQSMIDR362TF0Z7IRNEE7#46"
            ),
        )
        self.assertEqual(_world_spoiler_location(healing_pickup), "Golden Seed")

    # Check spoiler use game item names
    def test_spoiler_uses_game_item_names_in_player_facing_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Spoiler.txt"
            write_spoiler(path, (
                SpoilerEntry(
                    "World Loot",
                    "/Game/Blueprint/Article/Item/Material/DA_Item_JadwigaCoin_1",
                    "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpWeaponB_04",
                    "MagMell",
                ),
            ), seed="human-readable")
            content = path.read_text(encoding="utf-8")

        self.assertIn("MagMell: Uncanny Stone -> Tender Light Stone 4", content)
        self.assertNotIn("DA_Item_", content)

    # Check spoiler omits unchanged assignments even
    def test_spoiler_omits_unchanged_assignments_even_when_summary_counts_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Spoiler.txt"
            write_spoiler(path, (
                SpoilerEntry("World Loot", "DA_Old", "DA_Old", "Ordinary pickup"),
            ), seed="full-world-audit")
            content = path.read_text(encoding="utf-8")

        self.assertNotIn("Ordinary pickup", content)
        self.assertEqual(content, "Seed: full-world-audit\n\nNo randomized replacements.\n")

    # Check empty changes have no empty
    def test_empty_changes_have_no_empty_section_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Spoiler.txt"
            write_spoiler(path, (
                SpoilerEntry("Shops", "DA_Keep", "da_keep"),
            ), seed=1234)
            self.assertEqual(path.read_text(encoding="utf-8"), "Seed: 1234\n\nNo randomized replacements.\n")

    # Check header lists summary versions settings
    def test_header_lists_summary_versions_settings_then_seed(self) -> None:
        entries = (
            SpoilerEntry("World Loot", "/Game/Item/DA_Same", "/game/item/da_same"),
            SpoilerEntry("World Loot", "/Game/Item/DA_Old", "/Game/Item/DA_New"),
            SpoilerEntry("NPC Traders", "/Game/Item/DA_Keep", "/game/item/da_keep"),
        )
        summary = spoiler_assignment_summary(entries)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Spoiler.txt"
            write_spoiler(
                path,
                entries,
                seed="same-seed",
                created_date="2026-10-07",
                summary_lines=summary,
                metadata={"App Version": "v0.9.1", "Game Version": "fixture-build"},
                randomizer_settings=("Enabled Initial Inventory", "Disabled Blood Codes"),
            )
            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(lines[0], "Created: 2026-10-07")
        self.assertEqual(
            lines[1],
            "Total Randomized: 3 / Unchanged: 2 (same item after randomized)",
        )
        self.assertEqual(lines[2], "App Version: v0.9.1")
        self.assertEqual(lines[3], "Game Version: fixture-build")
        self.assertEqual(lines[4], "Randomizer Settings:")
        self.assertEqual(lines[5], "Seed: same-seed")
        self.assertEqual(lines[6:8], ["Enabled Initial Inventory", "Disabled Blood Codes"])
        self.assertLess(lines.index("Randomizer Settings:"), lines.index("=== World Loot ==="))

    # Check summary count support one line
    def test_summary_count_supports_one_line_for_many_global_pickups(self) -> None:
        entries = (
            SpoilerEntry("Ingredients", "DA_IngredientA", "DA_IngredientB", "All ground pickups", summary_count=120),
            SpoilerEntry("World Loot", "DA_Old", "DA_Old", "Unchanged chest"),
        )

        self.assertEqual(
            spoiler_assignment_summary(entries),
            ("Total Randomized: 121 / Unchanged: 1 (same item after randomized)",),
        )

    # Check shop spoiler resolve FName numbers
    def test_shop_spoiler_resolves_fname_numbers_and_reports_source_slot(self) -> None:
        destination = _shop_product("Shop_MM_Secret1", 0, "DA_SingleSword_IronPipe")
        source = _shop_product("Shop_MM_Secret1", 1, "DA_Hammer_Concrete")
        entry = _shop_spoiler_entry(ShopAssignment(destination, source))
        self.assertEqual(entry.system, "Shops")

        self.assertTrue(entry.vanilla.endswith("DA_SingleSword_IronPipe_1.DA_SingleSword_IronPipe_1"))
        self.assertTrue(entry.randomized.endswith("DA_Hammer_Concrete_1.DA_Hammer_Concrete_1"))
        self.assertEqual(entry.randomized_label, "DA_Hammer_Concrete")
        self.assertEqual(entry.destination, "MagMell shop, slot 1")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Spoiler.txt"
            write_spoiler(path, (entry,), seed="shop-exact-fname")
            content = path.read_text(encoding="utf-8")
            self.assertIn(
                "MagMell shop, slot 1: Single Sword Iron Pipe -> Hammer Concrete",
                content,
            )
            self.assertNotIn("slot 2", content)

    # Check searching item shows both source
    def test_searching_for_an_item_shows_both_its_source_and_destination(self) -> None:
        entries = (
            SpoilerEntry("World Loot", "DA_Jail_Hounds", "DA_GB_UNQ07_SustainAtkUp", "Old chest"),
            SpoilerEntry(
                "NPC Traders", "DA_Item_Important_Recipe_AttrUpFOR_1", "DA_Jail_Hounds",
                "NPC trader row Character.Type.Buddy.Noah slot 10",
                randomized_label="DA_Jail_Hounds",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Spoiler.txt"
            write_spoiler(path, entries, seed="find-hounds")
            lines = path.read_text(encoding="utf-8").splitlines()

        hounds_lines = [line for line in lines if "Hounds" in line]
        self.assertEqual(
            set(hounds_lines),
            {
                "Old chest: Jail Hounds -> GB UNQ07 Sustain Atk Up",
                "NPC trader row Character.Type.Buddy.Noah slot 10: "
                "Important Recipe Attr Up FOR 1 -> Jail Hounds",
            },
        )

    # Check NPC trader spoiler keep full
    def test_npc_trader_spoiler_keeps_full_destination_row_identity(self) -> None:
        destination = _shop_product("Character.Type.Buddy.Lou", 1, "DA_Old")
        source = _shop_product("Character.Type.Buddy.Noah", 3, "DA_New")
        destination = replace(destination, family="TRADE")
        source = replace(source, family="TRADE")
        entry = _shop_spoiler_entry(ShopAssignment(destination, source))
        self.assertEqual(entry.system, "NPC Traders")
        self.assertEqual(entry.destination, "Lou NPC Trader, slot 2")
        self.assertEqual(entry.randomized_label, "DA_New")
        self.assertEqual(entry.vanilla_label, "DA_Old")


if __name__ == "__main__":
    unittest.main()
