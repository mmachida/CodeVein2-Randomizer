from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cv2randomizer.core.seed import normalize_seed
from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.game.initial_inventory_catalog import build_available_weapon_pool
from cv2randomizer.game.initial_inventory_generation import (
    InitialInventoryGenerationError,
    InitialInventorySerializer,
    _validate_inspection_for_plan,
)
from cv2randomizer.game.jail_catalog import JailCatalogStore
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES
from cv2randomizer.randomizer.defensive_gadgets import defensive_gadget_world_candidates
from cv2randomizer.randomizer.initial_inventory import (
    INITIAL_INVENTORY_RNG_DOMAINS,
    InitialInventoryAssignment,
    InitialInventoryPlan,
    build_initial_inventory_plan,
)
from cv2randomizer.randomizer.jails import jail_world_candidates
from cv2randomizer.randomizer.reward_reservations import RewardReservationSet
from cv2randomizer.generation.pipeline import _without_reserved_rewards


ROOT = Path(__file__).resolve().parents[1]
VANILLA_WEAPON = "/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_Future_Lesser_1"
VANILLA_GADGET = "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_LesserShield_1"
VANILLA_JAIL = "/Game/Blueprint/Article/Jail/DA_Jail_Ogre"
SOURCE_ASSET = ROOT / "workspace" / "research_assets" / "initial_inventory" / "Content" / "Blueprint" / "Article" / "DA_InitialInventory_Player.uasset"
MAPPING_FILE = ROOT / "workspace" / "research_assets" / "CV2.usmap"


# Build package inventory
def _package_inventory(catalogs, gadgets, jails) -> PackageInventory:
    packages = set()
    for location in catalogs.world_loot:
        packages.add(location.package_path)
        packages.update(item.article_path for item in location.rewards)
    for product in catalogs.shops:
        table_name = SHOP_TABLE_NAMES.get(product.family.upper())
        if table_name is not None:
            packages.add(f"/Game/Blueprint/GameSystem/Shop/{table_name}")
        packages.add(product.original_product_path)
    packages.update(item.asset_path for item in gadgets.items)
    packages.update(item.asset_path for item in jails.items)
    return PackageInventory.create(packages)


class InitialInventoryTests(unittest.TestCase):
    # Set Up Class
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        cls.gadgets = DefensiveGadgetCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        cls.jails = JailCatalogStore(ROOT / "data" / "jails" / "current").load()
        cls.inventory = _package_inventory(cls.catalogs, cls.gadgets, cls.jails)
        cls.weapons = build_available_weapon_pool(
            cls.catalogs, cls.inventory, unsafe_packages=cls.catalogs.unsafe_packages,
        )

    # Build plan
    def _plan(self, seed: str, *, gadgets=None, jails=None, reservations=None) -> InitialInventoryPlan:
        return build_initial_inventory_plan(
            seed,
            "current",
            weapons=self.weapons,
            defensive_gadgets=gadgets or self.gadgets,
            jails=jails or self.jails,
            reservations=reservations,
        )

    # Check candidate pools come existing available
    def test_candidate_pools_come_from_existing_available_catalogs(self) -> None:
        self.assertTrue(self.weapons)
        self.assertTrue(all(item.asset_path.startswith("/Game/Blueprint/Article/Weapon/") for item in self.weapons))
        self.assertTrue(all(self.inventory.contains(item.asset_path) for item in self.weapons))
        self.assertTrue(all(origin.startswith(("world:", "shop:")) for item in self.weapons for origin in item.origins))
        self.assertTrue(any(item.world_reward_eligible for item in self.gadgets.items))
        self.assertTrue(any(item.world_reward_eligible for item in self.jails.items))

    # Check planning is deterministic use independent
    def test_planning_is_deterministic_uses_independent_domains_and_three_slots(self) -> None:
        first = self._plan("initial-inventory-repeatable-seed")
        repeated = self._plan("initial-inventory-repeatable-seed")
        self.assertEqual(first, repeated)
        self.assertEqual(first.rng_domains, INITIAL_INVENTORY_RNG_DOMAINS)
        self.assertEqual(len(first.assignments), 3)
        self.assertEqual(
            tuple(item.slot_id for item in first.assignments),
            ("starting_weapon", "starting_defensive_gadget", "starting_jail"),
        )
        self.assertEqual(
            tuple(item.vanilla_article_path for item in first.assignments),
            (VANILLA_WEAPON, VANILLA_GADGET, VANILLA_JAIL),
        )
        varying = {
            tuple(item.assigned_article_path for item in self._plan(f"initial-inventory-seed-{index}").assignments)
            for index in range(12)
        }
        self.assertGreater(len(varying), 1)

    # Check shared reservations are category scoped
    def test_shared_reservations_are_category_scoped_and_removed_from_remaining_catalogs(self) -> None:
        reservations = RewardReservationSet()
        reservations.reserve("unrelated", self.weapons[0].asset_path)
        reservations.reserve("weapon", self.weapons[1].asset_path)
        reservations.reserve("defensive_gadget", self.gadgets.items[0].asset_path)
        reservations.reserve("jail", self.jails.items[0].asset_path)
        plan = self._plan("reserved-inventory-seed", reservations=reservations)
        selected = {item.category: item.assigned_article_path for item in plan.assignments}
        self.assertNotEqual(selected["weapon"].casefold(), self.weapons[1].asset_path.casefold())
        self.assertNotEqual(selected["defensive_gadget"].casefold(), self.gadgets.items[0].asset_path.casefold())
        self.assertNotEqual(selected["jail"].casefold(), self.jails.items[0].asset_path.casefold())
        self.assertEqual(reservations.contains("weapon", self.weapons[0].asset_path), False)
        self.assertTrue(reservations.contains("unrelated", self.weapons[0].asset_path))

        chosen_jail = selected["jail"]
        reservations.reserve_many("weapon", (selected["weapon"],))
        reservations.reserve_many("defensive_gadget", (selected["defensive_gadget"],))
        reservations.reserve_many("jail", (chosen_jail,))
        remaining_gadgets = _without_reserved_rewards(self.gadgets, reservations, "defensive_gadget")
        remaining_jails = _without_reserved_rewards(self.jails, reservations, "jail")
        self.assertNotIn(selected["defensive_gadget"].casefold(), {item.asset_path.casefold() for item in remaining_gadgets.items})
        self.assertNotIn(chosen_jail.casefold(), {item.asset_path.casefold() for item in remaining_jails.items})
        self.assertNotIn(chosen_jail.casefold(), {item.canonical_identity.casefold() for item in jail_world_candidates(remaining_jails)})
        self.assertEqual(len(defensive_gadget_world_candidates(remaining_gadgets)), len(remaining_gadgets.items))

    # Check missing optional gadget pathos assets
    def test_missing_optional_gadget_or_pathos_assets_does_not_empty_base_pools(self) -> None:
        base_gadgets = replace(self.gadgets, items=tuple(item for item in self.gadgets.items if item.content_class == "BASE"))
        base_jails = replace(self.jails, items=tuple(item for item in self.jails.items if item.content_class == "BASE"))
        plan = self._plan("base-only-starting-pools", gadgets=base_gadgets, jails=base_jails)
        assigned = {item.category: item.assigned_article_path for item in plan.assignments}
        gadget_by_path = {item.asset_path: item for item in base_gadgets.items}
        jail_by_path = {item.asset_path: item for item in base_jails.items}
        self.assertEqual(gadget_by_path[assigned["defensive_gadget"]].content_class, "BASE")
        self.assertEqual(jail_by_path[assigned["jail"]].content_class, "BASE")
        self.assertGreater(dict(plan.candidate_counts)["defensive_gadget"], 0)
        self.assertGreater(dict(plan.candidate_counts)["jail"], 0)

    # Check real asset source identity fail
    @unittest.skipUnless(SOURCE_ASSET.is_file() and SOURCE_ASSET.with_suffix(".uexp").is_file() and MAPPING_FILE.is_file(),
                         "local validated Initial Inventory research extract is not present")
    def test_real_asset_source_identity_fail_closed_and_serialization_round_trip(self) -> None:
        gadget = next(item for item in self.gadgets.items if item.asset_path != VANILLA_GADGET)
        jail = next(item for item in self.jails.items if item.asset_path != VANILLA_JAIL)
        weapon = next(item for item in self.weapons if item.asset_path != VANILLA_WEAPON)
        plan = InitialInventoryPlan(
            normalize_seed("initial-inventory-uasset-round-trip"),
            "current",
            (
                InitialInventoryAssignment("starting_weapon", "Starting Weapon", "weapon", VANILLA_WEAPON, weapon.asset_path),
                InitialInventoryAssignment("starting_defensive_gadget", "Starting Defensive Gadget", "defensive_gadget", VANILLA_GADGET, gadget.asset_path),
                InitialInventoryAssignment("starting_jail", "Starting Jail", "jail", VANILLA_JAIL, jail.asset_path),
            ),
            (("weapon", len(self.weapons)), ("defensive_gadget", len(self.gadgets.items)), ("jail", len(self.jails.items))),
        )
        serializer = InitialInventorySerializer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "Content" / "Blueprint" / "Article" / SOURCE_ASSET.name
            source.parent.mkdir(parents=True)
            shutil.copy2(SOURCE_ASSET, source)
            shutil.copy2(SOURCE_ASSET.with_suffix(".uexp"), source.with_suffix(".uexp"))
            mappings = root / "CV2.usmap"
            shutil.copy2(MAPPING_FILE, mappings)
            inspection = serializer.inspect(work_root=root, mappings_path=mappings, input_file=source)
            _validate_inspection_for_plan(inspection, plan)
            slots = {item.slot_id: item for item in inspection.slots}
            self.assertEqual(slots["starting_weapon"].article_path, VANILLA_WEAPON)
            self.assertEqual(slots["starting_defensive_gadget"].article_path, VANILLA_GADGET)
            self.assertEqual(slots["starting_jail"].article_path, VANILLA_JAIL)
            self.assertEqual(len(inspection.preserved), 4)

            invalid = replace(plan, assignments=(replace(plan.assignments[0], vanilla_article_path="/Game/Blueprint/Article/Weapon/Unexpected"), *plan.assignments[1:]))
            rejected_output = root / "staging" / "invalid.uasset"
            with self.assertRaises(InitialInventoryGenerationError):
                serializer.patch(work_root=root, mappings_path=mappings, input_file=source,
                                 output_file=rejected_output, plan=invalid)
            self.assertFalse(rejected_output.exists())

            output = root / "staging" / "Content" / "Blueprint" / "Article" / SOURCE_ASSET.name
            reopened, output_files, patched, was_reopened = serializer.patch(
                work_root=root, mappings_path=mappings, input_file=source, output_file=output, plan=plan,
                motorcycle_unlocked=True,
            )
            _validate_inspection_for_plan(reopened, plan, verify_assignments=True)
            self.assertEqual(patched, 3)
            self.assertEqual(reopened.article_path_count, 8)
            self.assertTrue(was_reopened)
            self.assertEqual({path.suffix for path in output_files}, {".uasset", ".uexp"})
            lou = next(item for item in reopened.preserved if item.slot_id == "DA_BloodCode_Lou_01")
            self.assertEqual(lou.article_path, "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01")

    # Check motorcycle forma can be added
    @unittest.skipUnless(
        SOURCE_ASSET.is_file()
        and SOURCE_ASSET.with_suffix(".uexp").is_file()
        and MAPPING_FILE.is_file(),
        "requires local extracted game assets",
    )
    def test_motorcycle_forma_can_be_added_without_randomizing_starting_slots(self) -> None:
        serializer = InitialInventorySerializer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "Content" / "Blueprint" / "Article" / SOURCE_ASSET.name
            source.parent.mkdir(parents=True)
            shutil.copy2(SOURCE_ASSET, source)
            shutil.copy2(SOURCE_ASSET.with_suffix(".uexp"), source.with_suffix(".uexp"))
            mappings = root / "CV2.usmap"
            shutil.copy2(MAPPING_FILE, mappings)
            output = root / "staging" / "Content" / "Blueprint" / "Article" / SOURCE_ASSET.name
            inspection, output_files, patched, reopened = serializer.patch(
                work_root=root,
                mappings_path=mappings,
                input_file=source,
                output_file=output,
                plan=None,
                motorcycle_unlocked=True,
            )
            self.assertEqual(patched, 0)
            self.assertTrue(reopened)
            self.assertEqual(inspection.article_path_count, 8)
            self.assertEqual(len(inspection.slots), 3)
            self.assertEqual(len(inspection.preserved), 4)
            self.assertEqual({path.suffix for path in output_files}, {".uasset", ".uexp"})


if __name__ == "__main__":
    unittest.main()
