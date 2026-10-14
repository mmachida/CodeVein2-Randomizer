from __future__ import annotations

import itertools
import struct
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.core.seed import normalize_seed
from cv2randomizer.game.article_reward_catalog import CanonicalArticleRewardCatalogStore
from cv2randomizer.game.blood_code_catalog import BloodCodeCatalogStore
from cv2randomizer.game.booster_catalog import BoosterCatalogStore
from cv2randomizer.game.booster_reward_pool import build_extractor_booster_pool
from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.game.enemy_drop_catalog import EnemyDropCatalogStore, TABLE_ORDER
from cv2randomizer.game.extractor_catalog import ExtractorCatalogStore
from cv2randomizer.game.field_enemy_catalog import FieldEnemyCatalogStore
from cv2randomizer.game.jail_catalog import JailCatalogStore
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES
from cv2randomizer.game.shop_tables import ShopTableProduct
from cv2randomizer.randomizer.booster_rewards import exchange_extractor_world_sources
from cv2randomizer.randomizer.boosters import build_booster_plan
from cv2randomizer.randomizer.blood_codes import build_blood_code_plan
from cv2randomizer.randomizer.defensive_gadgets import defensive_gadget_world_candidates
from cv2randomizer.randomizer.enemy_drops import (
    EnemyDropRowSnapshot,
    EnemyDropSlotSnapshot,
    EnemyDropTableSnapshot,
    build_enemy_drop_plan,
)
from cv2randomizer.randomizer.extractor import build_extractor_plan
from cv2randomizer.randomizer.initial_inventory import build_initial_inventory_plan
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.generation.reproducibility import build_reproducibility_metadata
from cv2randomizer.game.initial_inventory_catalog import build_available_weapon_pool
from cv2randomizer.randomizer.jails import jail_world_candidates
from cv2randomizer.randomizer.reward_reservations import RewardReservationSet
from cv2randomizer.randomizer.shops import (
    build_global_article_mix_plans,
    build_npc_trader_only_plan,
    build_shop_randomization_plan,
    combine_shop_randomization_plans,
)
from cv2randomizer.randomizer.world_loot import (
    build_world_loot_randomization_plan,
    filter_enemy_reward_world_locations,
    world_loot_slot,
)
from cv2randomizer.randomizer.article_rewards import canonical_article_world_candidates


ROOT = Path(__file__).resolve().parents[1]
_OPTIONS = (
    "randomize_initial_inventory",
    "randomize_blood_codes",
    "randomize_boosters",
    "randomize_extractor_recipes",
    "randomize_shops",
    "mix_shops_with_world",
    "randomize_enemy_drops",
    "randomize_healing",
)


# Parse option tuple
def _option_tuple(**enabled: bool) -> tuple[bool, ...]:
    return tuple(enabled.get(field, False) for field in _OPTIONS)


_REPEATED_HASH_CASES = frozenset((
    _option_tuple(),
    _option_tuple(**dict.fromkeys(_OPTIONS, True)),
    _option_tuple(randomize_shops=True),
    _option_tuple(randomize_shops=True, mix_shops_with_world=True),
    _option_tuple(randomize_enemy_drops=True),
    _option_tuple(randomize_blood_codes=True),
    _option_tuple(randomize_boosters=True),
    _option_tuple(randomize_extractor_recipes=True),
    _option_tuple(randomize_healing=True),
    _option_tuple(randomize_initial_inventory=True),
))


# Find shop product
def _shop_product(family: str, row: str, index: int) -> ShopTableProduct:
    article = f"/Game/Blueprint/Article/Item/DA_Audit_{family}_{index}"
    item = f"DA_Audit_{family}_{index}"
    price = 100 + index
    return ShopTableProduct(
        family=family,
        shop_row=row,
        product_index=index,
        article_path=article,
        item_object=item,
        article_path_offset=0,
        article_path_bytes=bytes([index + 1]) * 20,
        price=price,
        price_offset=20,
        price_bytes=struct.pack("<i", price),
        uses_stock=True,
        uses_stock_offset=24,
        uses_stock_bytes=b"\x01",
        stock=1,
        stock_offset=25,
        stock_bytes=struct.pack("<i", 1),
        important=False,
        evaluation_formula="",
        evaluation_raw=b"",
        raw_bytes=b"audit",
    )


# Read enemy snapshots
def _enemy_snapshots(catalog) -> tuple[EnemyDropTableSnapshot, ...]:
    result = []
    for table_name in TABLE_ORDER:
        rows = []
        for row in (item for item in catalog.rows if item.table == table_name):
            slots = tuple(
                EnemyDropSlotSnapshot(
                    table_name,
                    row.row_id,
                    index,
                    f"weight:{table_name}:{row.row_id}:{index}",
                    f"other:{table_name}:{row.row_id}:{index}",
                    f"reward:{table_name}:{row.row_id}:{index}",
                )
                for index in range(row.expected_slots)
            )
            rows.append(EnemyDropRowSnapshot(table_name, row.row_id, slots))
        result.append(EnemyDropTableSnapshot(table_name, tuple(rows)))
    return tuple(result)


class ConfigurationMatrixTests(unittest.TestCase):
    # Set Up Class
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        cls.blood_codes = BloodCodeCatalogStore(ROOT / "data" / "blood_codes" / "current").load("current")
        cls.boosters = BoosterCatalogStore(ROOT / "data" / "boosters" / "current").load("current")
        cls.extractor = ExtractorCatalogStore(ROOT / "data" / "extractor" / "current").load("current")
        cls.canonical_articles = CanonicalArticleRewardCatalogStore(
            ROOT / "data" / "reward_pools" / "current",
        ).load("current")
        cls.gadgets = DefensiveGadgetCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        cls.jails = JailCatalogStore(ROOT / "data" / "jails" / "current").load()
        cls.enemy_drops = EnemyDropCatalogStore(ROOT / "data" / "enemy_drops").load("current")
        cls.enemy_tables = _enemy_snapshots(cls.enemy_drops)
        cls.field_enemy = FieldEnemyCatalogStore(ROOT / "data" / "field_enemy").load(
            "current", important_item_exclusions=cls.catalogs.important_item_exclusions,
        )
        shop_table_paths = tuple(
            f"/Game/Blueprint/GameSystem/Shop/{table}"
            for table in SHOP_TABLE_NAMES.values()
        )
        cls.inventory = PackageInventory.create((
            *shop_table_paths,
            *(location.package_path for location in cls.catalogs.world_loot),
            *(reward.article_path for location in cls.catalogs.world_loot for reward in location.rewards),
            *(product.original_product_path for product in cls.catalogs.shops),
            *(item.asset_path for item in cls.gadgets.items),
            *(slot.original_asset_path for slot in cls.gadgets.grants),
            *(item.asset_path for item in cls.jails.items),
            *(slot.booster_article_path for slot in cls.boosters.slots),
            *(slot.original_article_path for slot in cls.extractor.slots),
            *(item.asset_path for item in cls.canonical_articles.items),
        ))
        cls.fixtures = (
            _shop_product("NORMAL", "Human", 0),
            _shop_product("NORMAL", "Human", 1),
            _shop_product("TRADE", "Character.Type.Buddy.Coco", 0),
            _shop_product("TRADE", "Character.Type.Buddy.Louis", 1),
        )

    # Check all 256 raw configurations run
    def test_all_256_raw_configurations_run_real_planners_without_disabled_leaks(self) -> None:
        checked = 0
        for raw_values in itertools.product((False, True), repeat=len(_OPTIONS)):
            raw_values = tuple(raw_values)
            raw = dict(zip(_OPTIONS, raw_values, strict=True))
            options = GenerationOptions(**raw)
            restored = GenerationOptions.from_dict(options.to_dict())
            seed = "options-matrix-regression"
            checked += 1

            expected_mix = raw["randomize_shops"] and raw["mix_shops_with_world"]
            self.assertEqual(options.mix_shops_with_world, expected_mix, raw)
            for field in _OPTIONS:
                expected_value = expected_mix if field == "mix_shops_with_world" else raw[field]
                self.assertEqual(getattr(restored, field), expected_value, raw)
            self.assertEqual(options.npc_traders_mode, "mixed" if expected_mix else "only", raw)
            self.assertTrue(options.randomize_world_loot)
            self.assertTrue(options.randomize_bequeathed_formae_quest_rewards)
            self.assertEqual(options.randomize_npc_traders, raw["randomize_shops"])

            reservations = RewardReservationSet()
            extractor_plan = None
            active_enemy_world = filter_enemy_reward_world_locations(
                (*self.catalogs.world_loot, *self.field_enemy.locations),
                enabled=raw["randomize_enemy_drops"],
            )
            source_world_plan = build_world_loot_randomization_plan(
                active_enemy_world,
                seed,
                include_healing=raw["randomize_healing"],
                unsafe_packages=self.catalogs.unsafe_packages,
                supported_quest_grant_destinations=(
                    self.field_enemy.destination_ids if raw["randomize_enemy_drops"] else ()
                ),
            )
            self.assertGreater(len(source_world_plan.assignments), 0, raw)
            destination_slots = tuple(world_loot_slot(item.destination) for item in source_world_plan.assignments)
            self.assertEqual(len(destination_slots), len(set(destination_slots)), raw)
            self.assertTrue(all(not item.destination.protected for item in source_world_plan.assignments), raw)
            self.assertTrue(all(
                item.destination.location_type != "RENEWABLE_INGREDIENT"
                for item in source_world_plan.assignments
            ), raw)
            healing_count = sum(
                item.destination.location_type.startswith("HEALING_UPGRADE_")
                for item in source_world_plan.assignments
            )
            self.assertEqual(healing_count, 116 if raw["randomize_healing"] else 0, raw)
            one_time_ids = {
                item.destination.destination_id
                for item in source_world_plan.assignments
                if item.destination.destination_id
                and item.destination.destination_id.casefold().startswith(
                    ("one_time_enemy_", "harmony_moon_", "qst_fieldenemy_"),
                )
            }
            self.assertEqual(len(one_time_ids), 44 if raw["randomize_enemy_drops"] else 0, raw)

            blood_plan = build_blood_code_plan(
                self.blood_codes, seed, mix_postgame_dlc_with_base=True,
                enabled=raw["randomize_blood_codes"],
            )
            self.assertEqual(len(blood_plan.assignments), 79 if raw["randomize_blood_codes"] else 0, raw)

            booster_plan = build_booster_plan(
                self.boosters, seed, mix_postgame_dlc_with_base=True,
                enabled=raw["randomize_boosters"],
            )
            self.assertEqual(len(booster_plan.assignments), 102 if raw["randomize_boosters"] else 0, raw)

            enemy_plan = None
            if raw["randomize_enemy_drops"]:
                enemy_plan = build_enemy_drop_plan(
                    self.enemy_tables,
                    self.enemy_drops,
                    seed,
                    mix_postgame_dlc_with_base=True,
                )
                self.assertEqual(enemy_plan.eligible_slots, 1311, raw)
                self.assertTrue(enemy_plan.excluded_rows_preserved, raw)
            else:
                self.assertIsNone(enemy_plan, raw)

            extractor_pool = None
            if raw["randomize_extractor_recipes"]:
                extractor_pool = build_extractor_booster_pool(
                    extractor=self.extractor,
                    world_plan=source_world_plan,
                )
                self.assertEqual(len({item.article_path.casefold() for item in extractor_pool}), len(extractor_pool), raw)
                self.assertFalse(any("mastery" in origin for item in extractor_pool for origin in item.origins), raw)
                extractor_plan = build_extractor_plan(
                    self.extractor,
                    extractor_pool,
                    seed,
                    mix_postgame_dlc_with_base=True,
                )
                self.assertEqual(len(extractor_plan.assignments), 43, raw)
            else:
                self.assertEqual(reservations.paths_for("booster"), (), raw)
                self.assertIsNone(extractor_plan, raw)

            inventory_catalogs = replace(
                self.catalogs,
                shops=self.catalogs.shops if raw["randomize_shops"] else (),
            )
            weapon_pool = build_available_weapon_pool(
                inventory_catalogs,
                self.inventory,
                unsafe_packages=self.catalogs.unsafe_packages,
            )
            initial_plan = None
            initial_reservation_snapshot = {
                category: reservations.paths_for(category)
                for category in ("booster", "weapon", "defensive_gadget", "jail")
            }
            if raw["randomize_initial_inventory"]:
                initial_plan = build_initial_inventory_plan(
                    seed,
                    "current",
                    weapons=weapon_pool,
                    defensive_gadgets=self.gadgets,
                    jails=self.jails,
                    reservations=reservations,
                )
                self.assertEqual(len(initial_plan.assignments), 3, raw)
            else:
                self.assertIsNone(initial_plan, raw)

            gadget_catalog = self.gadgets
            jail_catalog = self.jails
            world_reserved_paths: tuple[str, ...] = ()
            world_candidate_pool = (
                *defensive_gadget_world_candidates(gadget_catalog),
                *jail_world_candidates(jail_catalog, world_loot=active_enemy_world),
                *canonical_article_world_candidates(self.canonical_articles, include_boosters=False),
            )
            world_plan = build_world_loot_randomization_plan(
                active_enemy_world,
                seed,
                include_healing=raw["randomize_healing"],
                unsafe_packages=self.catalogs.unsafe_packages,
                reserved_reward_identities=world_reserved_paths,
                supported_quest_grant_destinations=(
                    self.field_enemy.destination_ids if raw["randomize_enemy_drops"] else ()
                ),
                catalog_candidates=world_candidate_pool,
            )
            if extractor_plan is not None:
                world_plan = exchange_extractor_world_sources(world_plan, extractor_plan)
            final_slots = tuple(world_loot_slot(item.destination) for item in world_plan.assignments)
            self.assertEqual(len(final_slots), len(set(final_slots)), raw)
            canonical_assignments = tuple(
                item for item in world_plan.assignments
                if item.destination.mirror_of_package_path is None
            )
            source_occurrences = tuple(
                item.source_candidate.candidate_id
                for item in canonical_assignments
                if item.source_candidate is not None
            )
            self.assertEqual(len(source_occurrences), len(canonical_assignments), raw)
            self.assertEqual(len(source_occurrences), len(set(source_occurrences)), raw)
            self.assertTrue(all(not item.destination.protected for item in world_plan.assignments), raw)
            final_one_time_ids = {
                item.destination.destination_id
                for item in world_plan.assignments
                if item.destination.destination_id
                and item.destination.destination_id.casefold().startswith(
                    ("one_time_enemy_", "harmony_moon_", "qst_fieldenemy_"),
                )
            }
            expected_one_time_ids = 44 if raw["randomize_enemy_drops"] else 0
            self.assertEqual(len(final_one_time_ids), expected_one_time_ids, raw)

            shop_plan = None
            if raw["randomize_shops"]:
                normal = self.fixtures[:2]
                traders = self.fixtures[2:]
                if expected_mix:
                    _mixed_world, shop_plan = build_global_article_mix_plans(
                        world_plan,
                        (*normal, *traders),
                        seed,
                        enabled_families=("NORMAL", "TRADE"),
                        mix_postgame_dlc_with_base=True,
                        reserved_reward_identities=reservations.asset_paths,
                    )
                    self.assertTrue(shop_plan.mix_shops_with_world, raw)
                    self.assertEqual(len(shop_plan.assignments), 4, raw)
                else:
                    shop_plan = combine_shop_randomization_plans(
                        build_shop_randomization_plan(normal, seed, enabled_families=("NORMAL",)),
                        build_npc_trader_only_plan(traders, seed),
                    )
                    self.assertFalse(shop_plan.mix_shops_with_world, raw)
                    self.assertEqual(len(shop_plan.assignments), 4, raw)
                shop_slots = tuple(
                    (assignment.destination.family, assignment.destination.shop_row,
                     assignment.destination.product_index)
                    for assignment in shop_plan.assignments
                )
                self.assertEqual(len(shop_slots), len(set(shop_slots)), raw)
            else:
                self.assertIsNone(shop_plan, raw)

            if raw_values in _REPEATED_HASH_CASES:
                repeated_blood = build_blood_code_plan(
                    self.blood_codes, seed, mix_postgame_dlc_with_base=True,
                    enabled=raw["randomize_blood_codes"],
                )
                repeated_booster = build_booster_plan(
                    self.boosters, seed, mix_postgame_dlc_with_base=True,
                    enabled=raw["randomize_boosters"],
                )
                repeated_enemy = (
                    build_enemy_drop_plan(
                        self.enemy_tables, self.enemy_drops, seed,
                        mix_postgame_dlc_with_base=True,
                    )
                    if raw["randomize_enemy_drops"] else None
                )
                repeated_extractor = (
                    build_extractor_plan(
                        self.extractor, extractor_pool, seed,
                        mix_postgame_dlc_with_base=True,
                    )
                    if extractor_pool is not None else None
                )
                repeated_initial = None
                if raw["randomize_initial_inventory"]:
                    repeated_reservations = RewardReservationSet()
                    for category, paths in initial_reservation_snapshot.items():
                        repeated_reservations.reserve_many(category, paths)
                    repeated_initial = build_initial_inventory_plan(
                        seed, "current", weapons=weapon_pool,
                        defensive_gadgets=self.gadgets, jails=self.jails,
                        reservations=repeated_reservations,
                    )
                repeated_world = build_world_loot_randomization_plan(
                    active_enemy_world,
                    seed,
                    include_healing=raw["randomize_healing"],
                    unsafe_packages=self.catalogs.unsafe_packages,
                    reserved_reward_identities=world_reserved_paths,
                    supported_quest_grant_destinations=(
                        self.field_enemy.destination_ids if raw["randomize_enemy_drops"] else ()
                    ),
                    catalog_candidates=world_candidate_pool,
                )
                if repeated_extractor is not None:
                    repeated_world = exchange_extractor_world_sources(repeated_world, repeated_extractor)
                repeated_shop = None
                if raw["randomize_shops"]:
                    normal = self.fixtures[:2]
                    traders = self.fixtures[2:]
                    if expected_mix:
                        _mixed_world, repeated_shop = build_global_article_mix_plans(
                            repeated_world,
                            (*normal, *traders),
                            seed,
                            enabled_families=("NORMAL", "TRADE"),
                            mix_postgame_dlc_with_base=True,
                            reserved_reward_identities=world_reserved_paths,
                        )
                    else:
                        repeated_shop = combine_shop_randomization_plans(
                            build_shop_randomization_plan(normal, seed, enabled_families=("NORMAL",)),
                            build_npc_trader_only_plan(traders, seed),
                        )

                # Read reproducibility
                def reproducibility(world, shops, enemy, blood, boosters, extractor, initial):
                    return build_reproducibility_metadata(
                        seed=normalize_seed(seed),
                        game_version="configuration-matrix-fixture",
                        catalog_revision="current",
                        options=options,
                        content_availability=None,
                        source_cache_metadata=None,
                        world_plan=world,
                        shop_plan=shops or SimpleNamespace(assignments=(), npc_traders=None),
                        enemy_drop_plan=enemy,
                        blood_code_plan=blood,
                        booster_plan=boosters,
                        extractor_plan=extractor,
                        initial_inventory_plan=initial,
                    )

                first_metadata = reproducibility(
                    world_plan, shop_plan, enemy_plan, blood_plan, booster_plan,
                    extractor_plan, initial_plan,
                )
                second_metadata = reproducibility(
                    repeated_world, repeated_shop, repeated_enemy, repeated_blood,
                    repeated_booster, repeated_extractor, repeated_initial,
                )
                self.assertEqual(world_plan, repeated_world, raw)
                self.assertEqual(shop_plan, repeated_shop, raw)
                self.assertEqual(blood_plan, repeated_blood, raw)
                self.assertEqual(booster_plan, repeated_booster, raw)
                self.assertEqual(enemy_plan, repeated_enemy, raw)
                self.assertEqual(extractor_plan, repeated_extractor, raw)
                self.assertEqual(initial_plan, repeated_initial, raw)
                self.assertEqual(first_metadata.assignment_manifest_sha256, second_metadata.assignment_manifest_sha256, raw)

        self.assertEqual(checked, 256)


if __name__ == "__main__":
    unittest.main()
