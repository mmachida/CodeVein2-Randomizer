from __future__ import annotations

import random
import json
import tempfile
import unittest
from dataclasses import replace
from collections import Counter
from pathlib import Path

from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.game.jail_catalog import JailCatalogStore
from cv2randomizer.game.catalogs import is_valid_scripted_reward_only_location
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.world_loot_compatibility import apply_world_loot_export_rebindings
from cv2randomizer.randomizer.defensive_gadgets import defensive_gadget_world_candidates
from cv2randomizer.randomizer.jails import jail_world_candidates
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan, world_loot_slot
from tests._content_class_fixtures import classified_world_fixture


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_OBJECTS = {
    "DA_Jail_Bat", "DA_Jail_Hounds", "DA_Jail_Ivy", "DA_Jail_Ogre", "DA_Jail_Reaper", "DA_Jail_Stinger",
    "DA_Jail_Bat_Pathos", "DA_Jail_Hounds_Pathos", "DA_Jail_Ivy_Pathos", "DA_Jail_Ogre_Pathos",
    "DA_Jail_Reaper_Pathos", "DA_Jail_Stinger_Pathos",
}


class JailCatalogRandomizerTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.catalog = JailCatalogStore(ROOT / "data" / "jails" / "current").load()
        self.world_catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")

    # Check catalog has exact player jail
    def test_catalog_has_exact_player_jail_pool_and_metadata_shape(self) -> None:
        self.assertEqual({item.object for item in self.catalog.items}, EXPECTED_OBJECTS)
        self.assertEqual(len(self.catalog.items), 12)
        self.assertEqual(self.catalog.base_count, 6)
        self.assertEqual(self.catalog.dlc_count, 6)
        self.assertTrue(all(item.world_reward_eligible for item in self.catalog.items))
        self.assertEqual(len({item.object.casefold() for item in self.catalog.items}), 12)
        self.assertEqual(len({item.asset_path.casefold() for item in self.catalog.items}), 12)
        self.assertFalse(any(item.object.startswith("DA_Buddy_Jail_") for item in self.catalog.items))
        self.assertNotIn("DA_Jail_Death", {item.object for item in self.catalog.items})
        self.assertIn("DA_Jail_Reaper", {item.object for item in self.catalog.items})
        self.assertTrue(all(
            item.asset_path.startswith("/Game/Blueprint/Article/Jail/")
            for item in self.catalog.items if item.content_class == "BASE"
        ))
        self.assertTrue(all(
            item.asset_path.startswith("/Game/Blueprint/Article/Jail/DLC/")
            for item in self.catalog.items if item.content_class == "DLC"
        ))

    # Check vanilla sources match world catalog
    def test_vanilla_sources_match_world_catalog_and_all_jails_are_movable(self) -> None:
        self.catalog.validate_world_sources(self.world_catalogs.world_loot)
        self.assertEqual(self.catalog.world_source_count, 11)
        self.assertEqual(self.catalog.movable_world_source_count, 11)
        self.assertEqual(self.catalog.protected_world_sources, ())
        world_sources = [item for item in self.catalog.items if item.vanilla_source.source_type != "INITIAL_INVENTORY"]
        self.assertTrue(all(
            (item.vanilla_source.count, item.vanilla_source.notification) == (1, True)
            for item in world_sources
        ))
        initial = [item for item in self.catalog.items if item.vanilla_source.source_type == "INITIAL_INVENTORY"]
        self.assertEqual([item.object for item in initial], ["DA_Jail_Ogre"])

    # Accept validated version-specific export indices
    def test_vanilla_jail_sources_follow_profile_export_rebindings(self) -> None:
        bat = next(item for item in self.catalog.items if item.object == "DA_Jail_Bat")
        source = bat.vanilla_source
        assert source.package_path is not None
        assert source.export_index is not None
        assert source.export_name is not None
        rebound_index = source.export_index + 50_000
        hounds = next(item for item in self.catalog.items if item.object == "DA_Jail_Hounds")
        hounds_source = hounds.vanilla_source
        assert hounds_source.package_path is not None
        assert hounds_source.export_index is not None
        assert hounds_source.export_name is not None
        hounds_rebound_index = hounds_source.export_index + 50_000

        rebound_world = apply_world_loot_export_rebindings(
            self.world_catalogs.world_loot,
            {
                "world_loot_export_rebindings": [
                    {
                        "package_path": source.package_path,
                        "catalog_export_index": source.export_index,
                        "resolved_export_index": rebound_index,
                        "export_name": source.export_name,
                    },
                    {
                        "package_path": hounds_source.package_path,
                        "catalog_export_index": hounds_source.export_index,
                        "resolved_export_index": hounds_rebound_index,
                        "export_name": hounds_source.export_name,
                    },
                ],
            },
        )

        rebound_source = next(
            location for location in rebound_world
            if location.package_path.casefold() == source.package_path.casefold()
            and location.export_index == rebound_index
        )
        self.assertEqual(rebound_source.export_name, source.export_name)
        self.catalog.validate_world_sources(rebound_world)
        bat_candidate = next(
            candidate for candidate in jail_world_candidates(self.catalog, world_loot=rebound_world)
            if candidate.canonical_identity.casefold() == bat.asset_path.casefold()
        )
        assert bat_candidate.source_location is not None
        self.assertEqual(bat_candidate.source_location.export_index, rebound_index)
        hounds_mirror = next(
            location for location in rebound_world
            if location.mirror_of_package_path is not None
            and location.mirror_of_package_path.casefold() == hounds_source.package_path.casefold()
            and location.mirror_of_export_index == hounds_rebound_index
        )
        self.assertEqual(hounds_mirror.mirror_of_export_index, hounds_rebound_index)

    # Check available items skip identity source
    def test_available_items_skip_identity_when_its_source_package_is_missing(self) -> None:
        stinger = next(item for item in self.catalog.items if item.object == "DA_Jail_Stinger")
        source_package = stinger.vanilla_source.package_path
        assert source_package is not None

        article_only = self.catalog.available_items(PackageInventory.create((stinger.asset_path,)))
        self.assertNotIn(stinger, article_only)

        complete = self.catalog.available_items(PackageInventory.create((stinger.asset_path, source_package)))
        self.assertIn(stinger, complete)

    # Check world candidates skip jail whose
    def test_world_candidates_skip_only_jail_whose_active_source_slot_is_missing(self) -> None:
        stinger = next(item for item in self.catalog.items if item.object == "DA_Jail_Stinger")
        source = stinger.vanilla_source
        assert source.package_path is not None and source.export_index is not None
        available_world = tuple(
            location for location in self.world_catalogs.world_loot
            if not (
                location.package_path.casefold() == source.package_path.casefold()
                and location.export_index == source.export_index
            )
        )

        candidates = jail_world_candidates(self.catalog, world_loot=available_world)

        self.assertNotIn(stinger.asset_path.casefold(), {
            candidate.canonical_identity.casefold() for candidate in candidates
        })
        self.assertEqual(
            len(candidates),
            len(jail_world_candidates(self.catalog, world_loot=self.world_catalogs.world_loot)) - 1,
        )

    # Check unlinked second world source unique
    def test_unlinked_second_world_source_for_unique_jail_fails_closed(self) -> None:
        hounds = next(item for item in self.catalog.items if item.object == "DA_Jail_Hounds")
        source = hounds.vanilla_source
        assert source.package_path is not None and source.export_index is not None
        canonical = next(
            item for item in self.world_catalogs.world_loot
            if item.package_path.casefold() == source.package_path.casefold()
            and item.export_index == source.export_index
        )
        unlinked_copy = replace(
            canonical,
            export_index=canonical.export_index + 50_000,
            export_name="UnlinkedDuplicateHoundsReward",
            mirror_of_package_path=None,
            mirror_of_export_index=None,
        )
        with self.assertRaisesRegex(ValueError, "Unique Jail DA_Jail_Hounds must have exactly 1 canonical"):
            self.catalog.validate_world_sources((*self.world_catalogs.world_loot, unlinked_copy))

    # Check jail mirror must point exact
    def test_jail_mirror_must_point_to_its_exact_canonical_source(self) -> None:
        hounds = next(item for item in self.catalog.items if item.object == "DA_Jail_Hounds")
        source = hounds.vanilla_source
        assert source.package_path is not None and source.export_index is not None
        mirror = next(
            item for item in self.world_catalogs.world_loot
            if item.mirror_of_package_path is not None
            and item.mirror_of_package_path.casefold() == source.package_path.casefold()
            and item.mirror_of_export_index == source.export_index
        )
        invalid_mirror = replace(mirror, mirror_of_export_index=source.export_index + 1)
        mutated = tuple(
            invalid_mirror if item is mirror else item
            for item in self.world_catalogs.world_loot
        )
        with self.assertRaisesRegex(ValueError, "mirror for DA_Jail_Hounds does not point"):
            self.catalog.validate_world_sources(mutated)

    # Check stale protection metadata fail closed
    def test_stale_protection_metadata_fails_closed(self) -> None:
        hounds = next(item for item in self.catalog.items if item.object == "DA_Jail_Hounds")
        source = hounds.vanilla_source
        slot = (source.package_path.casefold(), source.export_index)
        mutated = tuple(
            replace(location, protected=True, protection_reasons=("stale test lock",))
            if world_loot_slot(location) == slot else location
            for location in self.world_catalogs.world_loot
        )
        with self.assertRaisesRegex(ValueError, "Protection metadata"):
            self.catalog.validate_world_sources(mutated)

    # Check buddy jail identity is rejected
    def test_buddy_jail_identity_is_rejected_by_runtime_loader(self) -> None:
        source_path = ROOT / "data" / "jails" / "current" / "jails.json"
        document = json.loads(source_path.read_text(encoding="utf-8"))
        document["items"][0]["object"] = "DA_Buddy_Jail_Bat"
        with tempfile.TemporaryDirectory() as temporary:
            catalog_path = Path(temporary) / "jails.json"
            catalog_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Non-player Jail"):
                JailCatalogStore(Path(temporary)).load()

    # Check jails join shared world pool
    def test_jails_join_the_shared_world_pool_once_and_balance_candidate_count(self) -> None:
        gadgets = DefensiveGadgetCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        candidates = (
            *defensive_gadget_world_candidates(gadgets),
            *jail_world_candidates(self.catalog, world_loot=self.world_catalogs.world_loot),
        )
        planner_fixture = classified_world_fixture(self.world_catalogs.world_loot)
        global_state = random.getstate()
        first = build_world_loot_randomization_plan(
            planner_fixture,
            "jail-world-pool-seed",
            unsafe_packages=self.world_catalogs.unsafe_packages,
            catalog_candidates=candidates,
        )
        self.assertEqual(random.getstate(), global_state)
        repeated = build_world_loot_randomization_plan(
            planner_fixture,
            "jail-world-pool-seed",
            unsafe_packages=self.world_catalogs.unsafe_packages,
            catalog_candidates=candidates,
        )
        self.assertEqual(first, repeated)
        safe_destinations = [item for item in planner_fixture if not item.protected]
        self.assertEqual(len(first.assignments), len(safe_destinations))
        self.assertEqual(first.gadget_injected_count, 0)
        self.assertEqual(first.jail_candidate_count, 11)
        self.assertEqual(first.jail_injected_count, 0)
        self.assertEqual(first.overlay_displaced_payloads, 0)
        jail_stats = first.candidate_stats("jail")
        self.assertEqual(jail_stats.already_present_count, 11)
        self.assertEqual(
            {path.rsplit("/", 1)[-1] for path in jail_stats.already_present_identities},
            {
                "DA_Jail_Bat", "DA_Jail_Hounds", "DA_Jail_Ivy", "DA_Jail_Reaper",
                "DA_Jail_Bat_Pathos", "DA_Jail_Hounds_Pathos", "DA_Jail_Ivy_Pathos",
                "DA_Jail_Ogre_Pathos", "DA_Jail_Reaper_Pathos", "DA_Jail_Stinger_Pathos",
                "DA_Jail_Stinger",
            },
        )
        self.assertEqual(jail_stats.duplicate_sources_removed, 0)
        self.assertEqual(jail_stats.injected_identities, ())

        assigned_jails = [
            reward.article_path for assignment in first.assignments
            if assignment.destination.mirror_of_package_path is None
            for reward in assignment.assigned_rewards
            if reward.article_path.startswith("/Game/Blueprint/Article/Jail/")
        ]
        movable_jails = {
            item.asset_path for item in self.catalog.items
            if item.vanilla_source.movable_world_loot
        }
        self.assertEqual(Counter(assigned_jails), Counter(movable_jails))
        for assignment in first.assignments:
            for index, reward in enumerate(assignment.assigned_rewards):
                if reward.article_path.startswith("/Game/Blueprint/Article/Jail/"):
                    destination = assignment.destination.rewards[index]
                    self.assertEqual((reward.count, reward.notification), (destination.count, destination.notification))
        external_jails = [
            assignment for assignment in first.assignments
            if assignment.destination.mirror_of_package_path is None
            if assignment.source_candidate is not None
            and assignment.source_candidate.candidate_category == "jail"
            and assignment.source_candidate.source_location is None
        ]
        self.assertEqual(len(external_jails), 0)

    # Check hounds is movable sunken city
    def test_hounds_is_movable_and_sunken_city_stinger_preserves_flag_behavior(self) -> None:
        plan = build_world_loot_randomization_plan(
            self.world_catalogs.world_loot,
            "sunken-city-stinger-seed",
            unsafe_packages=self.world_catalogs.unsafe_packages,
            catalog_candidates=jail_world_candidates(
                self.catalog,
                world_loot=self.world_catalogs.world_loot,
            ),
        )
        by_object = {item.object: item for item in self.catalog.items}
        hounds = by_object["DA_Jail_Hounds"].vanilla_source
        assert hounds.package_path is not None and hounds.export_index is not None
        hounds_slot = (hounds.package_path.casefold(), hounds.export_index)
        hounds_location = next(item for item in self.world_catalogs.world_loot if world_loot_slot(item) == hounds_slot)
        self.assertFalse(hounds_location.protected)
        self.assertIn(hounds_slot, {world_loot_slot(item.destination) for item in plan.assignments})
        hounds_assignment = next(
            item for item in plan.assignments if world_loot_slot(item.destination) == hounds_slot
        )
        self.assertNotEqual(
            hounds_assignment.assigned_rewards[0].article_path,
            hounds.asset_path,
        )

        bat = by_object["DA_Jail_Bat"].vanilla_source
        assert bat.package_path is not None and bat.export_index is not None
        bat_slot = (bat.package_path.casefold(), bat.export_index)
        bat_location = next(item for item in self.world_catalogs.world_loot if world_loot_slot(item) == bat_slot)
        self.assertFalse(bat_location.protected)
        bat_mirrors = [
            item for item in self.world_catalogs.world_loot
            if item.mirror_of_package_path is not None
            and item.mirror_of_package_path.casefold() == bat.package_path.casefold()
            and item.mirror_of_export_index == bat.export_index
        ]
        self.assertEqual(len(bat_mirrors), 1)
        bat_assignment = next(
            item for item in plan.assignments if world_loot_slot(item.destination) == bat_slot
        )
        self.assertNotEqual(bat_assignment.assigned_rewards[0].article_path, bat.asset_path)
        mirror_assignment = next(
            item for item in plan.assignments
            if world_loot_slot(item.destination) == world_loot_slot(bat_mirrors[0])
        )
        self.assertEqual(mirror_assignment.assigned_rewards, bat_assignment.assigned_rewards)

        stinger = by_object["DA_Jail_Stinger"].vanilla_source
        assert stinger.package_path is not None and stinger.export_index is not None
        stinger_slot = (stinger.package_path.casefold(), stinger.export_index)
        stinger_location = next(item for item in self.world_catalogs.world_loot if world_loot_slot(item) == stinger_slot)
        self.assertFalse(stinger_location.protected)
        self.assertEqual(stinger_location.scripted_reward_mode, "ARTICLE_ONLY_PRESERVE_FLAGS")
        self.assertTrue(is_valid_scripted_reward_only_location(stinger_location))
        stinger_assignment = next(
            assignment for assignment in plan.assignments
            if world_loot_slot(assignment.destination) == stinger_slot
        )
        self.assertNotEqual(stinger_assignment.assigned_rewards[0].article_path, by_object["DA_Jail_Stinger"].asset_path)
        mirror_assignment = next(
            assignment for assignment in plan.assignments
            if assignment.destination.mirror_of_package_path == stinger.package_path
            and assignment.destination.mirror_of_export_index == stinger.export_index
        )
        self.assertEqual(mirror_assignment.assigned_rewards, stinger_assignment.assigned_rewards)

    # Check scripted reward mode reject any
    def test_scripted_reward_only_mode_rejects_any_other_export_identity(self) -> None:
        stinger = next(
            item for item in self.world_catalogs.world_loot
            if item.scripted_reward_mode == "ARTICLE_ONLY_PRESERVE_FLAGS"
            and item.mirror_of_package_path is None
            and item.rewards[0].article_path == "/Game/Blueprint/Article/Jail/DA_Jail_Stinger"
        )
        self.assertFalse(is_valid_scripted_reward_only_location(replace(stinger, export_index=stinger.export_index + 1)))


if __name__ == "__main__":
    unittest.main()
