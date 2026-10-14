from __future__ import annotations

import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.game.catalogs import CatalogStore, RewardPayload, WorldLootLocation
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.randomizer.defensive_gadgets import (
    build_defensive_gadget_npc_plan,
    defensive_gadget_world_candidates,
)
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan
from tests._content_class_fixtures import classified_world_fixture


ROOT = Path(__file__).resolve().parents[1]


# Build a test location record
def _location(index: int, reward: RewardPayload) -> WorldLootLocation:
    return WorldLootLocation(
        package_path=f"/Game/Map/MainWorld/MainWorld/_Generated_/GadgetTest_{index}",
        source_file=f"GadgetTest_{index}.umap",
        export_index=index + 1,
        export_name=f"BP_PickupItem_C_{index}",
        actor_class="BP_PickupItem_C",
        actor_category="GROUND_PICKUP",
        save_id=f"gadget-test-{index}",
        protected=False,
        protection_reasons=(),
        rewards=(reward,),
    )


class DefensiveGadgetTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.catalog = DefensiveGadgetCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")

    # Check canonical catalog shape has warp
    def test_canonical_catalog_shape_has_warp_only_in_the_gadget_npc_pool(self) -> None:
        self.assertEqual(len(self.catalog.items), 17)
        self.assertEqual(Counter(item.content_class for item in self.catalog.items), {"BASE": 16, "DLC": 1})
        self.assertEqual(len(self.catalog.grants), 1)
        self.assertEqual(self.catalog.grants[0].slot_id, "warp_lise_grant")
        self.assertEqual(self.catalog.grants[0].export_name, "QSTask_GainItem_0")
        self.assertEqual(self.catalog.pending_grants, ())
        self.assertEqual(self.catalog.world_reward_count, 1)
        self.assertTrue(self.catalog.world_reward_notification)

    # Check world loot catalog classifies physical
    def test_world_loot_catalog_classifies_physical_gadget_sources_without_injecting(self) -> None:
        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        planner_fixture = classified_world_fixture(catalogs.world_loot)
        plan = build_world_loot_randomization_plan(
            planner_fixture,
            "defensive-gadget-world-seed",
            unsafe_packages=catalogs.unsafe_packages,
            gadget_candidates=defensive_gadget_world_candidates(self.catalog),
        )
        # Every currently eligible physical location is a destination. Mirror
        # map copies are represented as their own physical assignments, while
        # catalog identities without movable source locations remain unowned.
        self.assertEqual(len(plan.assignments), sum(not item.protected for item in planner_fixture))
        self.assertEqual(plan.gadget_candidate_count, 10)
        self.assertEqual(plan.gadget_injected_count, 0)
        self.assertEqual(plan.gadget_duplicate_sources, 0)
        self.assertEqual(plan.gadget_displaced_payloads, 0)
        source_gadgets = Counter(
            item.object
            for location in planner_fixture
            if location.mirror_of_package_path is None and not location.protected
            for reward in location.rewards
            if (item := self.catalog.resolve(reward.article_path)) is not None
        )
        gadget_payloads = [
            (assignment, reward)
            for assignment in plan.assignments
            if assignment.destination.mirror_of_package_path is None
            for reward in assignment.assigned_rewards
            if self.catalog.resolve(reward.article_path) is not None
        ]
        self.assertEqual(len(gadget_payloads), sum(source_gadgets.values()))
        self.assertEqual(
            Counter(self.catalog.resolve(reward.article_path).object for _, reward in gadget_payloads),
            source_gadgets,
        )
        injected = [
            item for item in plan.assignments
            if item.source is None and item.destination.mirror_of_package_path is None
        ]
        self.assertEqual(len(injected), 0)
        # A canonical reward keeps the metadata of its destination; some newly
        # audited pickup slots are multi-count or have a different notification.
        self.assertTrue(all(
            (reward.count, reward.notification)
            == (assignment.destination.rewards[0].count, assignment.destination.rewards[0].notification)
            for assignment, reward in gadget_payloads
        ))
        self.assertEqual(plan, build_world_loot_randomization_plan(
            planner_fixture,
            "defensive-gadget-world-seed",
            unsafe_packages=catalogs.unsafe_packages,
            gadget_candidates=defensive_gadget_world_candidates(self.catalog),
        ))

    # Check aliases resolve identity but each
    def test_aliases_resolve_to_identity_but_each_physical_source_occurrence_is_kept(self) -> None:
        candidates = defensive_gadget_world_candidates(self.catalog)
        aliased_gadget = next(item for item in self.catalog.items if item.aliases and item.content_class == "BASE")
        alias = aliased_gadget.aliases[0]
        second_gadget = next(item for item in self.catalog.items if item.object != aliased_gadget.object)
        locations = [
            _location(0, RewardPayload(alias, 1, True)),
            _location(1, RewardPayload(aliased_gadget.asset_path, 1, True)),
            _location(2, second_gadget.reward),
        ]
        locations.extend(
            _location(index, RewardPayload(f"/Game/SomeItem/DA_Ordinary_{index}", 1, True))
            for index in range(3, 18)
        )
        plan = build_world_loot_randomization_plan(locations, "dedup-gadget-seed", gadget_candidates=candidates)
        identities = [
            gadget.object
            for assignment in plan.assignments
            for reward in assignment.assigned_rewards
            if (gadget := self.catalog.resolve(reward.article_path)) is not None
        ]
        self.assertEqual(Counter(identities), Counter({aliased_gadget.object: 2, second_gadget.object: 1}))
        self.assertEqual(plan.gadget_candidate_count, 3)
        self.assertEqual(plan.gadget_duplicate_sources, 0)
        self.assertEqual(plan.gadget_injected_count, 0)
        self.assertEqual(plan.gadget_displaced_payloads, 0)
        self.assertEqual(len(plan.assignments), len(locations))

    # Check NPC pool respects mix use
    def test_npc_pool_respects_mix_and_uses_its_own_deterministic_plan(self) -> None:
        base = build_defensive_gadget_npc_plan(self.catalog, "npc-gadget-seed")
        base_repeat = build_defensive_gadget_npc_plan(self.catalog, "npc-gadget-seed")
        mixed = build_defensive_gadget_npc_plan(self.catalog, "npc-gadget-seed", mix_postgame_dlc_with_base=True)
        self.assertEqual(base, base_repeat)
        self.assertEqual(base.candidate_count, 16)
        self.assertEqual(mixed.candidate_count, 17)
        self.assertNotIn("DA_DefGadget_ZenonGreatShield", {item.assigned.object for item in base.assignments})
        self.assertEqual(base.pending_grants, ())
        self.assertEqual(base.assignments, build_defensive_gadget_npc_plan(self.catalog, "npc-gadget-seed").assignments)
        self.assertTrue(all(item.assigned.content_class == "BASE" for item in base.assignments))

    # Check NPC pool use all available
    def test_npc_pool_uses_all_available_grants_without_content_class_gate(self) -> None:
        plan = build_defensive_gadget_npc_plan(
            self.catalog, "npc-available-seed", mix_postgame_dlc_with_base=True,
        )
        self.assertEqual(len(plan.assignments), len(self.catalog.grants))

    # Check NPC pool exclude initial inventory
    def test_npc_pool_excludes_an_initial_inventory_reservation_but_keeps_vanilla_resolution(self) -> None:
        vanilla = self.catalog.resolve(self.catalog.grants[0].original_asset_path)
        self.assertIsNotNone(vanilla)
        plan = build_defensive_gadget_npc_plan(
            self.catalog,
            "initial-gadget-reservation",
            reserved_reward_identities=(vanilla.asset_path,),
        )
        self.assertEqual(plan.candidate_count, 15)
        self.assertNotIn(vanilla.asset_path.casefold(), {item.assigned.asset_path.casefold() for item in plan.assignments})
        self.assertEqual(plan.assignments[0].original.asset_path, vanilla.asset_path)

    # Check NPC planner allows self maps
    def test_npc_planner_allows_self_maps_without_forcing_a_reroll(self) -> None:
        plans = [build_defensive_gadget_npc_plan(self.catalog, seed) for seed in range(100)]
        self.assertTrue(any(plan.self_maps == 1 for plan in plans))

    # Check gadget alias multi reward bundle
    def test_gadget_alias_in_multi_reward_bundle_fails_closed(self) -> None:
        gadget = next(item for item in self.catalog.items if item.aliases and item.content_class == "BASE")
        location = replace(
            _location(0, gadget.reward),
            rewards=(gadget.reward, RewardPayload("/Game/SomeItem/DA_BundlePartner", 1, True)),
        )
        singleton = _location(1, RewardPayload("/Game/SomeItem/DA_Ordinary", 1, True))
        aliased_candidate = tuple(
            candidate for candidate in defensive_gadget_world_candidates(self.catalog)
            if candidate.canonical_identity == gadget.object
        )
        with self.assertRaisesRegex(ValueError, "multi-reward location"):
            build_world_loot_randomization_plan(
                [location, singleton], "gadget-multi-bundle", gadget_candidates=aliased_candidate,
            )

    # Check new option default off old
    def test_new_option_defaults_off_and_old_settings_still_load(self) -> None:
        self.assertTrue(GenerationOptions().randomize_defensive_gadget_npc_rewards)
        self.assertTrue(GenerationOptions.from_dict({
            "randomize_world_loot": True,
            "randomize_defensive_gadget_npc_rewards": False,
        }).randomize_defensive_gadget_npc_rewards)
        self.assertTrue(GenerationOptions.from_dict({"randomize_world_loot": False}).randomize_defensive_gadget_npc_rewards)


if __name__ == "__main__":
    unittest.main()
