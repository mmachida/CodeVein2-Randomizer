from __future__ import annotations

import struct
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.game.shop_tables import ShopTableProduct
from cv2randomizer.game.catalogs import CatalogStore, RewardPayload, WorldLootLocation
from cv2randomizer.game.fname_identity import FNameIdentity
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.generation.pipeline import _enabled_shop_families, _shop_protection_diagnostics
from cv2randomizer.randomizer.shops import (
    ArticleRewardIdentity,
    _identity_from_world,
    build_global_article_mix_plans,
    build_npc_trader_only_plan,
    build_shop_randomization_plan,
    is_npc_trader_row,
    shop_catalog_slot_lock_reason,
    shop_slot_lock_reason,
)
from cv2randomizer.randomizer.world_loot import WorldLootAssignment, build_world_loot_randomization_plan


# Read product
def _product(
    family: str,
    row: str,
    index: int,
    *,
    important: bool = False,
    protected: bool = False,
    uses_stock: bool | None = True,
    stock: int | None = 1,
) -> ShopTableProduct:
    item_folder = "Important/" if important else ""
    path = f"/Game/Blueprint/Article/Item/{item_folder}DA_{family}_{row}_{index}"
    item = f"DA_{family}_{row}_{index}"
    path_bytes = bytes([index + 1]) * 20
    uses_bytes = None if uses_stock is None else bytes([int(uses_stock)])
    stock_bytes = None if stock is None else struct.pack("<i", stock)
    return ShopTableProduct(
        family=family,
        shop_row=row,
        product_index=index,
        article_path=path,
        item_object=item,
        article_path_offset=0,
        article_path_bytes=path_bytes,
        price=100 + index,
        price_offset=20,
        price_bytes=struct.pack("<i", 100 + index),
        uses_stock=uses_stock,
        uses_stock_offset=24 if uses_bytes is not None else None,
        uses_stock_bytes=uses_bytes,
        stock=stock,
        stock_offset=25 if stock_bytes is not None else None,
        stock_bytes=stock_bytes,
        important=important,
        evaluation_formula="",
        evaluation_raw=b"",
        raw_bytes=b"",
        protected=protected,
        protection_reason="catalog exclusion" if protected else None,
    )


# Catalog table product
def _catalog_table_product(product) -> ShopTableProduct:
    placeholder = _product(
        product.family,
        product.shop_row,
        product.product_index,
        important="/item/important/" in product.original_product_path.casefold(),
        protected=product.protected,
        uses_stock=product.uses_stock,
        stock=product.stock,
    )
    return replace(
        placeholder,
        article_path=product.original_product_path,
        item_object=product.item_object,
        price=product.price,
        price_bytes=struct.pack("<i", product.price),
        protection_reason=product.protection_reason,
    )


class ShopRandomizerTests(unittest.TestCase):
    # Check one time enemy reward crosses
    def test_one_time_enemy_reward_crosses_both_directions_with_shop_world_mix(self) -> None:
        catalogs = CatalogStore(Path(__file__).resolve().parents[1] / "data" / "catalogs").load("current")
        boss = next(item for item in catalogs.world_loot if item.destination_id == "one_time_enemy_mm_medium08")
        ordinary_world = next(
            item for item in catalogs.world_loot
            if item.destination_kind == "ARTICLE_BULK" and not item.protected
            and len(item.rewards) == 1 and item.package_path != boss.package_path
        )
        shop = _product("NORMAL", "OneTimeRewardCrossing", 0)
        seed = "one-time-cross-2"
        world = build_world_loot_randomization_plan(
            (boss, ordinary_world), seed, unsafe_packages=catalogs.unsafe_packages,
        )
        mixed_world, shops = build_global_article_mix_plans(
            world, (shop,), seed, enabled_families=("NORMAL",),
        )

        boss_assignment = next(item for item in mixed_world.assignments if item.destination == boss)
        shop_assignment = next(item for item in shops.assignments if item.destination == shop)
        self.assertEqual(shop_assignment.source.canonical_path, boss.rewards[0].article_path)
        self.assertEqual(
            boss_assignment.source_candidate.candidate_id,
            "global-article:shop:NORMAL:OneTimeRewardCrossing:0",
        )

    # Check blood bead article remain shop
    def test_blood_bead_article_remains_shop_eligible_while_physical_slot_gets_other_article(self) -> None:
        catalogs = CatalogStore(Path(__file__).resolve().parents[1] / "data" / "catalogs").load("current")
        blood_bead = next(
            item for item in catalogs.world_loot
            if item.location_type == "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD"
            and item.mirror_of_package_path is None
        )
        shop = replace(
            _product("NORMAL", "HealingArticleEligibility", 0),
            article_path="/Game/Blueprint/Article/Item/Support/DA_HealingArticleEligibility",
            item_object="DA_HealingArticleEligibility",
        )
        seed = "healing-what-where-regression"
        world = build_world_loot_randomization_plan((blood_bead,), seed)
        mixed_world, shops = build_global_article_mix_plans(
            world, (shop,), seed, enabled_families=("NORMAL",),
        )

        physical_assignment = next(item for item in mixed_world.assignments if item.destination == blood_bead)
        shop_assignment = next(item for item in shops.assignments if item.destination == shop)

        self.assertEqual(shop_assignment.source.canonical_path, blood_bead.rewards[0].article_path)
        self.assertEqual(physical_assignment.assigned_rewards[0].article_path, shop.article_path)
        self.assertNotEqual(
            physical_assignment.assigned_rewards[0].article_path,
            blood_bead.rewards[0].article_path,
        )

    # Check numbered trader rewards keep duplicate
    def test_numbered_trader_rewards_keep_duplicate_locations_and_affinity_in_global_pool(self) -> None:
        base = "/Game/Blueprint/Article/Item/Support/DA_Item_AttrUpMND"
        inventory = PackageInventory.create((base + "_1", base + "_2"))
        traders = tuple(replace(
            _product("TRADE", "Character.Type.Buddy." + npc, 0),
            article_path=base, item_object="DA_Item_AttrUpMND",
            article_path_number=2, item_object_number=2,
            evaluation_formula=f"Affinity.{npc} >= 3", evaluation_raw=npc.encode(),
        ) for npc in ("Lou", "Noah"))
        self.assertFalse(inventory.contains(base))
        available = tuple(p for p in traders if inventory.contains(p.article_package_path))
        self.assertEqual(len(available), 2)
        world = build_world_loot_randomization_plan((), "numbered-traders")
        _, plan = build_global_article_mix_plans(world, available, "numbered-traders", enabled_families=("TRADE",))
        self.assertEqual(len(plan.assignments), 2)
        self.assertEqual(plan.global_candidate_count, 2)
        self.assertEqual({a.destination.shop_row for a in plan.assignments}, {p.shop_row for p in traders})
        for a in plan.assignments:
            self.assertIn(a.destination, traders)
            self.assertEqual(a.source.article_path_number, 2)
            self.assertEqual(a.source.item_object_number, 2)

    # Prepare the test fixture
    def setUp(self) -> None:
        self.products = (
            *(_product("NORMAL", "Shop_A", index) for index in range(8)),
            _product("NORMAL", "Shop_A", 8, important=True),
            _product("NORMAL", "Shop_A", 9, uses_stock=None, stock=1),
            _product("NORMAL", "Shop_A", 10, protected=True),
            *(_product("TRADE", "Shop_B", index) for index in range(4)),
        )

    # Parse mapping
    @staticmethod
    def _mapping(plan):
        return {
            (item.destination.family, item.destination.shop_row, item.destination.product_index): (
                item.source.family, item.source.shop_row, item.source.product_index
            )
            for item in plan.assignments
        }

    # Check same seed produces same derangement
    def test_same_seed_produces_same_derangement(self) -> None:
        first = build_shop_randomization_plan(self.products, "fixed-seed")
        second = build_shop_randomization_plan(self.products, "fixed-seed")

        self.assertEqual(self._mapping(first), self._mapping(second))
        self.assertTrue(first.assignments)
        self.assertTrue(all(item.destination != item.source for item in first.assignments))

    # Check sources are permuted within each
    def test_sources_are_permuted_only_within_each_shop_family(self) -> None:
        plan = build_shop_randomization_plan(self.products, 314159)
        by_family = {}
        for assignment in plan.assignments:
            self.assertEqual(assignment.destination.family, assignment.source.family)
            by_family.setdefault(assignment.destination.family, []).append(assignment)
        for assignments in by_family.values():
            self.assertEqual(
                {(item.destination.shop_row, item.destination.product_index) for item in assignments},
                {(item.source.shop_row, item.source.product_index) for item in assignments},
            )

    # Check shop content class labels do
    def test_shop_content_class_labels_do_not_gate_available_table_rows(self) -> None:
        products = (
            _product("NORMAL", "Base", 0),
            replace(_product("NORMAL", "Base", 1), what_content_class="DLC"),
            _product("DLC", "Dlc", 0),
            replace(_product("DLC", "Dlc", 1), what_content_class="BASE"),
            replace(_product("TRADE_DLC", "DlcTrade", 0), what_content_class="BASE"),
            replace(_product("NORMAL", "Unknown", 0), what_content_class="UNKNOWN"),
        )

        plan = build_shop_randomization_plan(
            products,
            "where-what-metadata",
            enabled_families=("NORMAL", "DLC", "TRADE_DLC"),
        )
        participating = {
            (item.destination.family, item.destination.shop_row, item.destination.product_index)
            for item in plan.assignments
        }
        self.assertEqual(len(plan.disabled_slots), 0)
        self.assertIn(("NORMAL", "Base", 1), participating)
        self.assertIn(("NORMAL", "Base", 0), participating)
        self.assertIn(("DLC", "Dlc", 0), participating)
        self.assertIn(("DLC", "Dlc", 1), participating)
        self.assertIn(("TRADE_DLC", "DlcTrade", 0), participating)
        self.assertIn(("NORMAL", "Unknown", 0), participating)

    # Check global mix combines semantic base
    def test_global_mix_combines_only_semantic_base_dlc_shop_pairs(self) -> None:
        products = (
            *(_product("NORMAL", "Normal", index) for index in range(4)),
            *(_product("DLC", "DlcNormal", index) for index in range(3)),
            *(_product("LUXURY", "Luxury", index) for index in range(3)),
            *(_product("TRADE", "Trade", index) for index in range(4)),
            *(_product("TRADE_DLC", "TradeDlc", index) for index in range(3)),
        )
        off = build_shop_randomization_plan(products, "family-mix-test")
        self.assertTrue(all(item.destination.family == item.source.family for item in off.assignments))
        mixed = build_shop_randomization_plan(
            products, "family-mix-test", mix_postgame_dlc_with_base=True,
        )
        self.assertEqual(mixed, build_shop_randomization_plan(
            products, "family-mix-test", mix_postgame_dlc_with_base=True,
        ))
        self.assertTrue(mixed.mix_postgame_dlc_with_base)
        family_pair = {"NORMAL": "DLC", "DLC": "NORMAL", "TRADE": "TRADE_DLC", "TRADE_DLC": "TRADE"}
        for item in mixed.assignments:
            destination_family = item.destination.family
            self.assertIn(item.source.family, {destination_family, family_pair.get(destination_family)})
        for families in (("NORMAL", "DLC"), ("TRADE", "TRADE_DLC"), ("LUXURY",)):
            assignments = [item for item in mixed.assignments if item.destination.family in families]
            identity = lambda product: (product.family, product.shop_row, product.product_index)
            self.assertEqual(
                Counter(identity(item.destination) for item in assignments),
                Counter(identity(item.source) for item in assignments),
            )
        self.assertTrue(all(
            item.source.family == "LUXURY"
            for item in mixed.assignments if item.destination.family == "LUXURY"
        ))

    # Check shop selection automatically enables present
    def test_shop_selection_automatically_enables_present_counterpart_families(self) -> None:
        catalogs = CatalogStore().load("current")
        self.assertEqual(
            _enabled_shop_families(GenerationOptions(), catalogs.shops),
            ("NORMAL", "DLC", "LUXURY"),
        )
        self.assertEqual(
            _enabled_shop_families(GenerationOptions(shops_normal=False, shops_trade=True), catalogs.shops),
            ("NORMAL", "DLC", "LUXURY"),
        )
        self.assertEqual(
            _enabled_shop_families(
                GenerationOptions(shops_normal=False, shops_luxury=False, shops_trade=True),
                (_product("TRADE", "Shop_Ordinary", 0),),
            ),
            ("TRADE",),
        )
        self.assertEqual(
            _enabled_shop_families(GenerationOptions(randomize_shops=False), catalogs.shops),
            (),
        )

    # Check catalog discovers regressed buddy trader
    def test_catalog_discovers_only_the_regressed_buddy_trader_inventory(self) -> None:
        catalog = CatalogStore().load("current")
        traders = tuple(
            product for product in catalog.shops
            if is_npc_trader_row(product.family, product.shop_row)
        )
        counts = Counter((product.family, product.shop_row) for product in traders)
        expected = {
            "Craig": 33, "Holly": 35, "Iris": 33, "Jadwiga": 35, "Josee": 32,
            "Lavinia": 30, "Lise": 31, "Lou": 31, "Lyle": 33, "Noah": 33,
            "Valentin": 33, "Zenon": 33, "Liv": 31,
        }
        actual = {row.rsplit(".", 1)[-1]: slots for (_family, row), slots in counts.items()}
        self.assertEqual(actual, expected)
        self.assertEqual(sum(count for (family, _row), count in counts.items() if family == "TRADE"), 392)
        self.assertEqual(sum(count for (family, _row), count in counts.items() if family == "TRADE_DLC"), 31)
        self.assertEqual(len(traders), 423)

    # Check NPC trader use one deterministic
    def test_npc_trader_only_uses_one_deterministic_cross_companion_pool(self) -> None:
        products = tuple(
            replace(_product("TRADE", f"Character.Type.Buddy.{name}", index),
                    evaluation_formula=f"Friendship.{name} >= {index}",
                    evaluation_raw=f"raw-{name}-{index}".encode())
            for name in ("Lou", "Zenon", "Craig", "Liv")
            for index in range(4)
        ) + tuple(_product("TRADE_DLC", "Character.Type.Buddy.Liv", index) for index in range(4))
        first = build_npc_trader_only_plan(products, "trader-seed")
        second = build_npc_trader_only_plan(products, "trader-seed")
        identity = lambda product: (product.family, product.shop_row, product.product_index)
        self.assertEqual(
            [(identity(item.destination), identity(item.source)) for item in first.assignments],
            [(identity(item.destination), identity(item.source)) for item in second.assignments],
        )
        self.assertEqual(
            Counter(identity(item.destination) for item in first.assignments),
            Counter(identity(item.source) for item in first.assignments),
        )
        self.assertGreater(
            sum(item.destination.shop_row != item.source.shop_row for item in first.assignments), 0,
        )
        self.assertGreater(
            sum(item.destination.family != item.source.family for item in first.assignments), 0,
        )
        self.assertTrue(first.requires_name_map_preparation)
        originals = {identity(item): item for item in products}
        self.assertTrue(all(
            item.destination.evaluation_raw == originals[identity(item.destination)].evaluation_raw
            for item in first.assignments
        ))

    # Check NPC trader reservations protection apply
    def test_npc_trader_reservations_and_protection_apply_without_important_path_heuristics(self) -> None:
        reserved = _product("TRADE", "Character.Type.Buddy.Lou", 0, important=True)
        protected = _product("TRADE", "Character.Type.Buddy.Zenon", 0, protected=True)
        movable = (
            _product("TRADE", "Character.Type.Buddy.Lou", 1),
            _product("TRADE", "Character.Type.Buddy.Zenon", 1),
        )
        plan = build_npc_trader_only_plan(
            (reserved, protected, *movable), "trader-reservations",
            reserved_reward_identities=(reserved.article_path,),
        )
        self.assertIn(reserved, plan.locked_slots)
        self.assertIn(protected, plan.locked_slots)
        self.assertNotIn(reserved, {item.source for item in plan.assignments})
        self.assertTrue(all(item.source in movable for item in plan.assignments))

    # Check NPC trader infinite unknown stock
    def test_npc_trader_infinite_and_unknown_stock_slots_are_excluded(self) -> None:
        infinite = replace(
            _product("TRADE", "Character.Type.Buddy.Lou", 0, uses_stock=None, stock=None),
            catalog_uses_stock=False,
            catalog_stock=3,
        )
        finite = _product("TRADE", "Character.Type.Buddy.Lou", 1, stock=5)
        unknown = _product(
            "TRADE", "Character.Type.Buddy.Lou", 3, uses_stock=None, stock=None,
        )
        protected_infinite = _product(
            "TRADE", "Character.Type.Buddy.Lou", 2,
            uses_stock=None, stock=None, protected=True,
        )
        plan = build_npc_trader_only_plan(
            (infinite, finite, protected_infinite, unknown), "trader-infinite-destination",
        )
        destinations = {item.destination for item in plan.assignments}
        sources = {item.source for item in plan.assignments}
        self.assertEqual(destinations, {finite})
        self.assertEqual(sources, {finite})
        self.assertIn(infinite, plan.locked_slots)
        self.assertIn(unknown, plan.unvalidated_slots)
        self.assertIn(protected_infinite, plan.locked_slots)
        self.assertEqual(shop_slot_lock_reason(infinite), "INFINITE_SHOP_STOCK")
        self.assertEqual(shop_slot_lock_reason(unknown), "UNKNOWN_SHOP_STOCK_SEMANTICS")

    # Check normal important items join shop
    def test_normal_important_items_join_shop_pool_and_unvalidated_stock_stays_locked(self) -> None:
        plan = build_shop_randomization_plan(self.products, 1)
        assigned = {assignment.destination for assignment in plan.assignments}

        important = next(item for item in self.products if item.important)
        masked = next(item for item in self.products if not item.has_explicit_stock_payload)
        self.assertIn(important, assigned)
        self.assertNotIn(masked, assigned)
        catalog_locked = next(item for item in self.products if item.protected)
        self.assertNotIn(catalog_locked, assigned)
        self.assertNotIn(important, plan.locked_slots)
        self.assertIn(catalog_locked, plan.locked_slots)
        self.assertIn(masked, plan.unvalidated_slots)

    # Check infinite unknown slots are excluded
    def test_infinite_and_unknown_slots_are_excluded_from_shop_pool_and_diagnostics(self) -> None:
        infinite = replace(
            _product("NORMAL", "Shop_A", 20, uses_stock=None, stock=None),
            catalog_uses_stock=False,
            catalog_stock=0,
        )
        unknown = _product("NORMAL", "Shop_A", 21, uses_stock=None, stock=None)
        finite = _product("NORMAL", "Shop_A", 22, stock=3)
        plan = build_shop_randomization_plan((infinite, unknown, finite), "stock-locks")

        self.assertEqual({item.destination for item in plan.assignments}, {finite})
        self.assertEqual({item.source for item in plan.assignments}, {finite})
        self.assertIn(infinite, plan.locked_slots)
        self.assertIn(unknown, plan.unvalidated_slots)
        self.assertEqual(shop_slot_lock_reason(infinite), "INFINITE_SHOP_STOCK")
        self.assertEqual(shop_slot_lock_reason(unknown), "UNKNOWN_SHOP_STOCK_SEMANTICS")
        reasons = {item["reason"] for item in _shop_protection_diagnostics(plan)}
        self.assertTrue({"INFINITE_SHOP_STOCK", "UNKNOWN_SHOP_STOCK_SEMANTICS"} <= reasons)

    # Check zero masked stock default resolve
    def test_zero_masked_stock_defaults_resolve_infinite_but_not_finite(self) -> None:
        masked_infinite = replace(
            _product("NORMAL", "Shop_A", 30, uses_stock=None, stock=1),
            uses_stock_mask_bit=(100, 0x04),
            catalog_uses_stock=True,
            catalog_stock=0,
        )
        masked_quantity = replace(
            _product("NORMAL", "Shop_A", 31, uses_stock=True, stock=None),
            stock_mask_bit=(101, 0x08),
            catalog_uses_stock=True,
            catalog_stock=0,
        )
        finite_a = _product("NORMAL", "Shop_A", 32, stock=4)
        finite_b = _product("NORMAL", "Shop_A", 33, stock=2)

        self.assertFalse(masked_infinite.logical_uses_stock)
        self.assertFalse(masked_infinite.destination_uses_stock)
        self.assertEqual(masked_infinite.logical_stock, 1)
        self.assertIsNone(masked_infinite.uses_stock_bytes)
        self.assertEqual(
            shop_slot_lock_reason(masked_infinite), "INFINITE_SHOP_STOCK",
        )
        self.assertTrue(masked_quantity.logical_uses_stock)
        self.assertEqual(masked_quantity.logical_stock, 0)
        self.assertEqual(
            shop_slot_lock_reason(masked_quantity), "UNKNOWN_SHOP_STOCK_SEMANTICS",
        )

        plan = build_shop_randomization_plan(
            (masked_infinite, masked_quantity, finite_a, finite_b),
            "zero-mask-stock-defaults",
        )
        self.assertEqual(
            {item.destination for item in plan.assignments}, {finite_a, finite_b},
        )
        self.assertEqual(
            {item.source for item in plan.assignments}, {finite_a, finite_b},
        )
        self.assertIn(masked_infinite, plan.locked_slots)
        self.assertIn(masked_quantity, plan.unvalidated_slots)

    # Check global article pool deterministically swaps
    def test_global_article_pool_deterministically_swaps_shop_and_world_rewards(self) -> None:
        world_locations = (
            _world_location(1, "/Game/Blueprint/Article/Weapon/DA_WorldSword", 2, False),
            _world_location(2, "/Game/Blueprint/Article/Item/DA_WorldMedicine", 5, True),
        )
        world_plan = build_world_loot_randomization_plan(world_locations, "cross-0")
        shops = (
            replace(
                _product("NORMAL", "Shop_A", 0, important=True),
                article_path="/Game/Blueprint/Article/Item/Important/DA_CookingRecipe",
                item_object="DA_CookingRecipe",
            ),
            _product("NORMAL", "Shop_A", 1),
            replace(
                _product("NORMAL", "Shop_A", 2, uses_stock=None, stock=None),
                catalog_uses_stock=False,
                catalog_stock=0,
            ),
            _product("NORMAL", "Shop_A", 3, uses_stock=None, stock=None),
        )
        mixed_world, shop_plan = build_global_article_mix_plans(
            world_plan, shops, "cross-0", enabled_families=("NORMAL",),
        )
        repeated_world, repeated_shop = build_global_article_mix_plans(
            world_plan, shops, "cross-0", enabled_families=("NORMAL",),
        )
        self.assertEqual(mixed_world.assignments, repeated_world.assignments)
        self.assertEqual(shop_plan.assignments, repeated_shop.assignments)
        self.assertEqual(shop_plan.global_candidate_count, 4)
        self.assertTrue(shop_plan.mix_shops_with_world)
        self.assertTrue(all(item.article_only for item in shop_plan.assignments))
        infinite_shop = next(item for item in shops if item.destination_uses_stock is False)
        self.assertNotIn(infinite_shop, {item.destination for item in shop_plan.assignments})
        self.assertNotIn(infinite_shop, {
            item.source.source_product for item in shop_plan.assignments
            if isinstance(item.source, ArticleRewardIdentity)
        })
        self.assertIn(infinite_shop, shop_plan.locked_slots)
        unknown_shop = next(item for item in shops if shop_slot_lock_reason(item) == "UNKNOWN_SHOP_STOCK_SEMANTICS")
        self.assertIn(unknown_shop, shop_plan.unvalidated_slots)
        self.assertNotIn(unknown_shop, {item.destination for item in shop_plan.assignments})
        self.assertNotIn(unknown_shop, {
            item.source.source_product for item in shop_plan.assignments
            if isinstance(item.source, ArticleRewardIdentity)
        })
        self.assertTrue(all(
            item.assigned_rewards[0].count == item.destination.rewards[0].count
            and item.assigned_rewards[0].notification == item.destination.rewards[0].notification
            for item in mixed_world.assignments
        ))
        self.assertGreater(shop_plan.global_world_from_shop, 0)
        self.assertGreater(shop_plan.global_shop_from_world, 0)
        self.assertEqual(shop_plan.global_world_from_shop, shop_plan.global_shop_from_world)
        self.assertIn(
            "/Game/Blueprint/Article/Item/Important/DA_CookingRecipe",
            {reward.article_path for assignment in mixed_world.assignments for reward in assignment.assigned_rewards},
        )
        self.assertIn(
            "/Game/Blueprint/Article/Weapon/DA_WorldSword",
            {assignment.source.canonical_path for assignment in shop_plan.assignments},
        )
        for assignment in shop_plan.assignments:
            self.assertEqual(assignment.destination.price, 100 + assignment.destination.product_index)
            self.assertIs(assignment.destination.uses_stock, True)
            self.assertGreater(assignment.destination.stock, 0)

    # Check world suffix digits remain literal
    def test_world_suffix_digits_remain_literal_fname_names(self) -> None:
        path = "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpJailA_07"
        location = _world_location(30, path, 1, True)

        identity = _identity_from_world(WorldLootAssignment(location, location))

        self.assertEqual(identity.canonical_path, path)
        self.assertEqual(identity.article_path, path)
        self.assertEqual(identity.article_path_number, 0)
        self.assertEqual(identity.item_object, "DA_Item_MatLevelUpJailA_07")
        self.assertEqual(identity.item_object_number, 0)

    # Check shop world keep serialized FName
    def test_shop_to_world_keeps_serialized_fname_numbers(self) -> None:
        base_path = "/Game/Blueprint/Article/Item/Support/DA_SerializedReward"
        shop_source = replace(
            _product("NORMAL", "Shop_A", 0),
            article_path=base_path,
            item_object="DA_SerializedReward",
            article_path_number=8,
            item_object_number=8,
        )
        world = (
            _world_location(31, "/Game/Blueprint/Article/Item/DA_WorldA", 1, True),
            _world_location(32, "/Game/Blueprint/Article/Item/DA_WorldB", 1, True),
        )
        # Algorithm v3 uses its own fixed PRNG stream; this seed exercises the
        # shop-to-world branch while preserving the serialization assertion.
        seed = "serialized-world-mix-v3-2"
        world_plan = build_world_loot_randomization_plan(world, seed)

        mixed_world, _ = build_global_article_mix_plans(
            world_plan,
            (shop_source, _product("NORMAL", "Shop_A", 1)),
            seed,
            enabled_families=("NORMAL",),
        )

        from_shop = [
            reward
            for assignment in mixed_world.assignments
            for reward in assignment.assigned_rewards
            if assignment.source_candidate is not None
            and assignment.source_candidate.candidate_id == "global-article:shop:NORMAL:Shop_A:0"
        ]
        self.assertEqual(len(from_shop), 1)
        self.assertEqual(from_shop[0].article_path, f"{base_path}_7")
        self.assertEqual(from_shop[0].soft_object_path_identity.package_name, FNameIdentity(base_path, 8))
        self.assertEqual(
            from_shop[0].soft_object_path_identity.asset_name,
            FNameIdentity("DA_SerializedReward", 8),
        )
    # Check mixed trader slots join existing
    def test_mixed_trader_slots_join_the_existing_global_world_and_shop_article_pool(self) -> None:
        world_locations = (
            _world_location(11, "/Game/Blueprint/Article/Weapon/DA_GlobalSword", 1, False),
            _world_location(12, "/Game/Blueprint/Article/Item/DA_GlobalMedicine", 4, True),
        )
        seed = "trader-global-mix-v3-0"
        world_plan = build_world_loot_randomization_plan(world_locations, seed)
        traders = tuple(
            _product("TRADE", f"Character.Type.Buddy.{row}", index)
            for row in ("Lou", "Zenon", "Craig") for index in range(2)
        )
        shops = tuple(_product("NORMAL", "Shop_Jadwiga", index) for index in range(3))
        mixed_world, shop_plan = build_global_article_mix_plans(
            world_plan, (*shops, *traders), seed,
            enabled_families=("NORMAL", "TRADE"),
        )
        trader_assignments = tuple(
            item for item in shop_plan.assignments
            if is_npc_trader_row(item.destination.family, item.destination.shop_row)
        )
        self.assertEqual(len(trader_assignments), len(traders))
        self.assertTrue(all(item.article_only for item in trader_assignments))
        self.assertEqual(shop_plan.global_shop_locations, len(shops) + len(traders))
        self.assertTrue(all(item.source.origin_kind in {"WORLD", "SHOP"} for item in trader_assignments))
        self.assertEqual(
            [(item.destination, item.source) for item in shop_plan.assignments],
            [(item.destination, item.source) for item in build_global_article_mix_plans(
                world_plan, (*shops, *traders), seed,
                enabled_families=("NORMAL", "TRADE"),
            )[1].assignments],
        )
        self.assertEqual(len(mixed_world.assignments), len(world_plan.assignments))
        self.assertEqual(shop_plan.global_candidate_count, len(shops) + len(traders) + len(world_locations))
        self.assertIn(
            "/Game/Blueprint/Article/Weapon/DA_GlobalSword",
            {assignment.source.canonical_path for assignment in trader_assignments},
        )

    # Check extractor consumed boosters keep vanilla
    def test_extractor_consumed_boosters_keep_their_vanilla_source_slots(self) -> None:
        consumed = "/Game/Blueprint/Article/Booster/Basic/DA_Booster_Strength_1"
        world_locations = (
            _world_location(1, consumed, 1, True),
            _world_location(2, "/Game/Blueprint/Article/Weapon/DA_WorldSword", 1, True),
        )
        world_plan = build_world_loot_randomization_plan(
            world_locations, "extractor-priority", reserved_reward_identities=(consumed,),
        )
        shop_products = (
            _product("NORMAL", "Shop_A", 0),
            replace(
                _product("NORMAL", "Shop_A", 1),
                article_path=consumed.rsplit("_1", 1)[0],
                item_object="DA_Booster_Strength",
                article_path_number=2,
                item_object_number=2,
            ),
            _product("NORMAL", "Shop_A", 2),
        )
        mixed_world, shop_plan = build_global_article_mix_plans(
            world_plan,
            shop_products,
            "extractor-priority",
            enabled_families=("NORMAL",),
            reserved_reward_identities=(consumed,),
        )
        self.assertIn(world_locations[0], {item.location for item in world_plan.locked_locations})
        self.assertEqual(len(world_plan.assignments), 1)
        self.assertEqual(shop_plan.extractor_reserved_slots, (shop_products[1],))
        assigned = [reward.article_path for item in mixed_world.assignments for reward in item.assigned_rewards]
        assigned.extend(item.source.canonical_path for item in shop_plan.assignments)
        self.assertNotIn(consumed.casefold(), {path.casefold() for path in assigned})

    # Check explicitly available sources enter shop
    def test_only_explicitly_available_sources_enter_shop_world_pool(self) -> None:
        world = (
            _world_location(1, "/Game/Blueprint/Article/Item/DA_BaseWorld", 1, True),
            _world_location(
                2, "/Game/Blueprint/Article/Item/DA_DlcWorld", 1, True,
                content_class="DLC",
            ),
        )
        shops = (
            _product("NORMAL", "BaseShop", 0),
            _product("DLC", "DlcShop", 0),
        )

        base_world = build_world_loot_randomization_plan(
            world[:1], "shop-world-optional-source-absent",
        )
        base_world, base_shops = build_global_article_mix_plans(
            base_world,
            shops,
            "shop-world-optional-source-absent",
            enabled_families=("NORMAL",),
        )
        self.assertEqual(len(base_world.assignments), 1)
        self.assertEqual(base_world.disabled_locations, ())
        self.assertEqual([item.family for item in base_shops.disabled_slots], ["DLC"])
        base_assigned_paths = {
            reward.article_path
            for assignment in base_world.assignments
            for reward in assignment.assigned_rewards
        } | {
            assignment.source.canonical_path
            for assignment in base_shops.assignments
        }
        self.assertIn("/Game/Blueprint/Article/Item/DA_BaseWorld", base_assigned_paths)
        self.assertNotIn("/Game/Blueprint/Article/Item/DA_DLC_DlcShop_0", base_assigned_paths)

        dlc_world = build_world_loot_randomization_plan(world, "shop-world-all-present")
        dlc_world, dlc_shops = build_global_article_mix_plans(
            dlc_world,
            shops,
            "shop-world-all-present",
            enabled_families=("NORMAL", "DLC"),
        )
        self.assertEqual(len(dlc_world.assignments), 2)
        self.assertEqual(len(dlc_shops.assignments), 2)
        dlc_assigned_paths = {
            reward.article_path
            for assignment in dlc_world.assignments
            for reward in assignment.assigned_rewards
        } | {
            assignment.source.canonical_path
            for assignment in dlc_shops.assignments
        }
        self.assertIn("/Game/Blueprint/Article/Item/DA_DlcWorld", dlc_assigned_paths)
        self.assertIn("/Game/Blueprint/Article/Item/DA_DLC_DlcShop_0", dlc_assigned_paths)

    # Check disabled families remain untouched
    def test_disabled_families_remain_untouched(self) -> None:
        plan = build_shop_randomization_plan(self.products, 1, enabled_families=("NORMAL",))

        self.assertTrue(all(item.destination.family == "NORMAL" for item in plan.assignments))
        self.assertEqual(
            {item.family for item in plan.disabled_slots},
            {"TRADE"},
        )

    # Check unknown family excluded slot fail
    def test_unknown_family_and_excluded_slot_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown shop table families"):
            build_shop_randomization_plan(self.products, 1, enabled_families=("OTHER",))
        with self.assertRaisesRegex(ValueError, "not present"):
            build_shop_randomization_plan(self.products, 1, excluded_slots=(("NORMAL", "Missing", 0),))

    # Check explicit exclusions are locked
    def test_explicit_exclusions_are_locked(self) -> None:
        excluded = ("NORMAL", "Shop_A", 0)
        plan = build_shop_randomization_plan(self.products, 1, excluded_slots=(excluded,))
        locked = {(item.family, item.shop_row, item.product_index) for item in plan.locked_slots}
        self.assertIn(excluded, locked)

    # Check global mix exclude test shop
    def test_global_mix_excludes_test_shop_slots_from_sources_and_destinations(self) -> None:
        excluded = ("NORMAL", "Shop_A", 0)
        _, plan = build_global_article_mix_plans(
            build_world_loot_randomization_plan((), "shop-probe-exclusion"),
            self.products,
            "shop-probe-exclusion",
            enabled_families=("NORMAL",),
            excluded_slots=(excluded,),
        )
        assigned_destinations = {
            (item.destination.family, item.destination.shop_row, item.destination.product_index)
            for item in plan.assignments
        }
        assigned_sources = {
            (source.family, source.shop_row, source.product_index)
            for item in plan.assignments
            for source in (
                item.source.source_product
                if isinstance(item.source, ArticleRewardIdentity) else item.source,
            )
            if source is not None
        }
        locked = {(item.family, item.shop_row, item.product_index) for item in plan.locked_slots}
        self.assertNotIn(excluded, assigned_destinations)
        self.assertNotIn(excluded, assigned_sources)
        self.assertIn(excluded, locked)

    # Check magmell tool shop catalog protects
    def test_magmell_tool_shop_catalog_protects_only_the_exact_row(self) -> None:
        catalogs = CatalogStore().load("current")
        tool = tuple(
            product for product in catalogs.shops
            if product.family == "NORMAL" and product.shop_row == "Shop_Jadwiga_Item"
        )
        weapon = tuple(
            product for product in catalogs.shops
            if product.family == "NORMAL" and product.shop_row == "Shop_Jadwiga_Weapon"
        )
        self.assertEqual(len(tool), 56)
        self.assertEqual({item.product_index for item in tool}, set(range(56)))
        self.assertTrue(all(item.protected for item in tool))
        self.assertEqual({item.protection_reason for item in tool}, {"MAGMELL_TOOL_SHOP"})
        self.assertTrue(all(shop_catalog_slot_lock_reason(item) is not None for item in tool))

        objects = [item.item_object for item in tool]
        self.assertEqual(sum(name.startswith("DA_Item_Ammo") for name in objects), 3)
        self.assertEqual(sum(
            name.startswith("DA_Item_MatLevelUpWeapon")
            or name.startswith("DA_Item_DLC_MatLevelUpWeapon")
            for name in objects
        ), 15)
        self.assertEqual(sum(name.startswith("DA_Item_MatEnchantWeapon") for name in objects), 11)
        self.assertEqual(sum(name.startswith("DA_Item_MatEnchantDefGadget") for name in objects), 10)

        self.assertEqual(len(weapon), 32)
        self.assertTrue(all(not item.protected for item in weapon))
        self.assertTrue(all(
            not item.protected for item in catalogs.shops
            if item.shop_row in {"Shop_MM_Normal1", "Shop_MM_Secret1", "Shop_MM_Special1"}
        ))
        self.assertFalse(any(item.protection_reason == "MAGMELL_TOOL_SHOP" for item in weapon))

        cards = tuple(item for item in catalogs.shops if "shopcert" in item.original_product_path.casefold())
        self.assertEqual(len(cards), 16)
        self.assertTrue(all(not item.protected for item in cards))
        world_cards = tuple(
            reward
            for location in catalogs.world_loot
            for reward in location.rewards
            if "shopcert" in reward.article_path.casefold()
        )
        self.assertEqual(
            len({reward.article_path.casefold() for reward in world_cards}),
            2,
        )
        self.assertTrue(all(
            not location.protected
            for location in catalogs.world_loot
            if any("shopcert" in reward.article_path.casefold() for reward in location.rewards)
        ))
        card_locations = tuple(
            location for location in catalogs.world_loot
            if any("shopcert" in reward.article_path.casefold() for reward in location.rewards)
        )
        card_plan = build_world_loot_randomization_plan((
            *card_locations,
            _world_location(901, "/Game/Test/DA_CardPoolControlA", 1, True),
            _world_location(902, "/Game/Test/DA_CardPoolControlB", 1, True),
        ), "magmell-cards-remain-eligible")
        card_source_paths = {
            reward.article_path
            for assignment in card_plan.assignments if assignment.source_candidate is not None
            for reward in assignment.source_candidate.rewards
        }
        card_destination_packages = {
            assignment.destination.package_path for assignment in card_plan.assignments
        }
        self.assertTrue({
            reward.article_path for location in card_locations for reward in location.rewards
            if "shopcert" in reward.article_path.casefold()
        } <= card_source_paths)
        self.assertTrue({location.package_path for location in card_locations} <= card_destination_packages)

    # Check magmell tool shop is outside
    def test_magmell_tool_shop_is_outside_shop_only_sources_and_destinations(self) -> None:
        catalogs = CatalogStore().load("current")
        normal = tuple(
            _catalog_table_product(item) for item in catalogs.shops
            if item.family == "NORMAL"
        )
        tool_slots = {("NORMAL", "Shop_Jadwiga_Item", index) for index in range(56)}
        plan = build_shop_randomization_plan(
            normal, "magmell-tool-protection", enabled_families=("NORMAL",)
        )
        destinations = {
            (item.destination.family, item.destination.shop_row, item.destination.product_index)
            for item in plan.assignments
        }
        sources = {
            (item.source.family, item.source.shop_row, item.source.product_index)
            for item in plan.assignments if isinstance(item.source, ShopTableProduct)
        }
        locked = {
            (item.family, item.shop_row, item.product_index): item.protection_reason
            for item in plan.locked_slots
        }
        self.assertTrue(tool_slots.isdisjoint(destinations))
        self.assertTrue(tool_slots.isdisjoint(sources))
        tool_reasons = {key: shop_slot_lock_reason(item) for item in normal
                        if item.shop_row == "Shop_Jadwiga_Item"
                        for key in [(item.family, item.shop_row, item.product_index)]}
        self.assertEqual(set(tool_reasons), tool_slots)
        self.assertEqual(set(tool_reasons.values()), {"MAGMELL_TOOL_SHOP"})
        self.assertEqual({key for key, reason in locked.items() if key in tool_slots}, tool_slots)
        weapon_movable = {
            (item.family, item.shop_row, item.product_index)
            for item in normal if item.shop_row == "Shop_Jadwiga_Weapon"
            and shop_slot_lock_reason(item) is None
        }
        self.assertEqual(len(weapon_movable), 30)
        self.assertTrue(weapon_movable <= destinations)
        self.assertTrue(weapon_movable <= sources)
        self.assertNotIn("MAGMELL_TOOL_SHOP", {
            item.protection_reason for item in normal if item.shop_row == "Shop_Jadwiga_Weapon"
        })
        self.assertIn(("NORMAL", "Shop_MM_Normal1", 0), destinations)

        card_slots = {
            (item.family, item.shop_row, item.product_index)
            for item in catalogs.shops if "shopcert" in item.original_product_path.casefold()
        }
        self.assertTrue(card_slots <= destinations)
        self.assertTrue(card_slots <= sources)
        diagnostics = _shop_protection_diagnostics(plan)
        tool_diagnostic = next(item for item in diagnostics if item["reason"] == "MAGMELL_TOOL_SHOP")
        self.assertEqual(tool_diagnostic["slot_count"], 56)

    # Check magmell tool shop is absent
    def test_magmell_tool_shop_is_absent_from_global_shop_world_pool(self) -> None:
        catalogs = CatalogStore().load("current")
        products = tuple(
            _catalog_table_product(item) for item in catalogs.shops
            if item.family == "NORMAL"
            and item.shop_row in {"Shop_Jadwiga_Item", "Shop_Jadwiga_Weapon"}
        )
        tool_paths = {
            item.article_path for item in products if item.shop_row == "Shop_Jadwiga_Item"
        }
        self.assertTrue(tool_paths.isdisjoint({
            item.article_path for item in products if item.shop_row == "Shop_Jadwiga_Weapon"
        }))
        world_locations = (
            _world_location(801, "/Game/Blueprint/Article/Item/DA_ToolProtectionWorldA", 1, True),
            _world_location(802, "/Game/Blueprint/Article/Item/DA_ToolProtectionWorldB", 1, True),
        )
        world_plan = build_world_loot_randomization_plan(world_locations, "magmell-tool-global")
        mixed_world, shop_plan = build_global_article_mix_plans(
            world_plan,
            products,
            "magmell-tool-global",
            enabled_families=("NORMAL",),
        )
        tool_slots = {("NORMAL", "Shop_Jadwiga_Item", index) for index in range(56)}
        destinations = {
            (item.destination.family, item.destination.shop_row, item.destination.product_index)
            for item in shop_plan.assignments
        }
        source_ids = {item.source.source_id for item in shop_plan.assignments}
        self.assertTrue(tool_slots.isdisjoint(destinations))
        self.assertFalse(any("Shop_Jadwiga_Item" in source_id for source_id in source_ids))
        expected_weapon_slots = {
            (item.family, item.shop_row, item.product_index)
            for item in products if item.shop_row == "Shop_Jadwiga_Weapon"
            and shop_slot_lock_reason(item) is None
        }
        self.assertEqual(shop_plan.global_shop_locations, len(expected_weapon_slots))
        self.assertEqual(shop_plan.global_candidate_count, len(expected_weapon_slots) + 2)
        self.assertEqual(destinations, expected_weapon_slots)
        self.assertNotIn("MAGMELL_TOOL_SHOP", {
            item.protection_reason for item in products if item.shop_row == "Shop_Jadwiga_Weapon"
        })
        for assignment in mixed_world.assignments:
            self.assertIsNotNone(assignment.source_candidate)
            self.assertNotIn("Shop_Jadwiga_Item", assignment.source_candidate.candidate_id)
            self.assertNotIn(assignment.assigned_rewards[0].article_path, tool_paths)


# Find world location
def _world_location(
    index: int,
    article_path: str,
    count: int,
    notification: bool,
    *,
    content_class: str = "BASE",
) -> WorldLootLocation:
    return WorldLootLocation(
        package_path=f"/Game/Map/MainWorld/MainWorld/_Generated_/W{index}",
        source_file=f"W{index}.umap",
        export_index=1,
        export_name=f"BP_Pickup_{index}_UAID_{index}",
        actor_class="BP_PickupItem_C",
        actor_category="GROUND_PICKUP",
        save_id=f"save-{index}",
        protected=False,
        protection_reasons=(),
        rewards=(RewardPayload(article_path, count, notification),),
        content_class=content_class,
    )


if __name__ == "__main__":
    unittest.main()
