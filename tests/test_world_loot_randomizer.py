from __future__ import annotations

import unittest
from collections import Counter
from dataclasses import replace

from cv2randomizer.game.catalogs import CatalogStore, RewardPayload, WorldLootLocation
from cv2randomizer.randomizer.world_loot import (
    WorldLootRewardCandidate,
    build_world_loot_randomization_plan,
    world_loot_slot,
)


# Build a test location record
def _location(
    index: int,
    *,
    category: str = "GROUND_PICKUP",
    protected: bool = False,
    reasons: tuple[str, ...] = (),
    rewards: tuple[RewardPayload, ...] | None = None,
) -> WorldLootLocation:
    payload = rewards or (
        RewardPayload(f"/Game/Blueprint/Article/Item/Support/DA_Item_{index}", index + 1, index % 2 == 0),
    )
    return WorldLootLocation(
        package_path=f"/Game/Map/MainWorld/MainWorld/_Generated_/Map_{index // 5}",
        source_file=f"Map_{index // 5}.umap",
        export_index=index + 1,
        export_name=f"BP_PickupItem_C_UAID_{index}",
        actor_class="BP_PickupItem_C",
        actor_category=category,
        save_id=f"save-{index}",
        protected=protected,
        protection_reasons=reasons,
        rewards=payload,
    )


class WorldLootRandomizerTests(unittest.TestCase):
    # Check unowned catalog candidates do not
    def test_unowned_catalog_candidates_do_not_inflate_world_source_pool(self) -> None:
        locations = [_location(index) for index in range(4)]
        candidates = tuple(
            WorldLootRewardCandidate(
                candidate_id=f"canonical:test:{index}",
                rewards=(RewardPayload(f"/Game/Item/Candidate{index}", 1, True),),
                candidate_category="test",
                canonical_identity=f"/Game/Item/Candidate{index}",
            )
            for index in range(5)
        )

        forward = build_world_loot_randomization_plan(
            locations, 123, catalog_candidates=candidates,
        )
        reversed_input = build_world_loot_randomization_plan(
            reversed(locations), 123, catalog_candidates=tuple(reversed(candidates)),
        )

        # Find assignment signature
        def assignment_signature(plan):
            return tuple(sorted(
                (world_loot_slot(item.destination), tuple(reward.article_path for reward in item.assigned_rewards))
                for item in plan.assignments
            ))

        self.assertEqual(assignment_signature(forward), assignment_signature(reversed_input))
        self.assertEqual(forward.candidate_stats("test").canonical_candidates, 0)
        self.assertEqual(forward.candidate_stats("test").unowned_candidates_skipped, 5)
        self.assertEqual(
            Counter(reward.article_path for item in forward.assignments for reward in item.assigned_rewards),
            Counter(reward.article_path for location in locations for reward in location.rewards),
        )

    # Check repeated article occurrences keep distinct
    def test_repeated_article_occurrences_keep_distinct_source_ids_and_multiplicity(self) -> None:
        repeated = RewardPayload("/Game/Blueprint/Article/Item/DA_Item_Unique", 1, True)
        locations = (
            _location(0, rewards=(repeated,)),
            _location(1, rewards=(repeated,)),
        )

        plan = build_world_loot_randomization_plan(locations, "repeat-multiplicity")

        self.assertEqual(len(plan.assignments), 2)
        self.assertEqual(len(plan.source_occurrence_ids), 2)
        self.assertEqual(len(set(plan.source_occurrence_ids)), 2)
        self.assertEqual(
            Counter(reward.article_path for item in plan.assignments for reward in item.assigned_rewards),
            Counter({repeated.article_path: 2}),
        )

    # Check combines ground pickups chests without
    def test_combines_ground_pickups_and_chests_without_fixed_locations(self) -> None:
        locations = [
            _location(0, category="GROUND_PICKUP"),
            _location(1, category="TREASURE_BOX"),
            _location(2, category="GROUND_PICKUP"),
            _location(3, category="TREASURE_BOX"),
        ]

        plan = build_world_loot_randomization_plan(locations, 0)

        self.assertEqual(len(plan.assignments), len(locations))
        self.assertEqual(
            {world_loot_slot(a.source) for a in plan.assignments},
            {world_loot_slot(location) for location in locations},
        )
        self.assertTrue(
            all(world_loot_slot(a.destination) != world_loot_slot(a.source) for a in plan.assignments)
        )
        self.assertTrue(
            any(a.destination.actor_category != a.source.actor_category for a in plan.assignments)
        )

    # Check mirror use canonical assignment without
    def test_mirror_uses_canonical_assignment_without_becoming_a_pool_source(self) -> None:
        canonical = _location(0)
        second = _location(1)
        mirror = replace(
            canonical,
            package_path="/Game/Map/MainWorld/DlcWorld/_Generated_/Mirror",
            source_file="Mirror.umap",
            export_index=90,
            export_name="BP_PickupItem_C_UAID_Mirror",
            mirror_of_package_path=canonical.package_path,
            mirror_of_export_index=canonical.export_index,
        )

        plan = build_world_loot_randomization_plan((canonical, second, mirror), 217)
        assignments = {world_loot_slot(item.destination): item for item in plan.assignments}
        canonical_assignment = assignments[world_loot_slot(canonical)]
        mirror_assignment = assignments[world_loot_slot(mirror)]

        self.assertEqual(plan.mirrored_locations, (mirror,))
        self.assertEqual(
            tuple(reward.article_path for reward in mirror_assignment.assigned_rewards),
            tuple(reward.article_path for reward in canonical_assignment.assigned_rewards),
        )
        self.assertEqual(
            tuple((reward.count, reward.notification) for reward in mirror_assignment.assigned_rewards),
            tuple((reward.count, reward.notification) for reward in mirror.rewards),
        )
        self.assertNotEqual(world_loot_slot(mirror_assignment.source), world_loot_slot(mirror))
        self.assertEqual(
            {world_loot_slot(item.source) for item in plan.assignments},
            {world_loot_slot(canonical), world_loot_slot(second)},
        )

    # Check move article identities preserve destination
    def test_moves_article_identities_and_preserves_destination_metadata(self) -> None:
        single = [
            _location(i, rewards=(RewardPayload(f"/Game/Item/DA_{i}", i + 2, i % 2 == 1),))
            for i in range(4)
        ]
        multi = [
            _location(
                10 + i,
                category="TREASURE_BOX" if i % 2 else "GROUND_PICKUP",
                rewards=(
                    RewardPayload(f"/Game/Item/DA_{i}_A", i + 1, True),
                    RewardPayload(f"/Game/Item/DA_{i}_B", i + 3, False),
                ),
            )
            for i in range(3)
        ]
        locations = single + multi
        plan = build_world_loot_randomization_plan(locations, 82)

        original_articles_by_arity = {
            arity: sorted(
                tuple(reward.article_path for reward in location.rewards)
                for location in locations if len(location.rewards) == arity
            )
            for arity in (1, 2)
        }
        assigned_articles_by_arity = {
            arity: sorted(
                tuple(reward.article_path for reward in assignment.assigned_rewards)
                for assignment in plan.assignments if len(assignment.destination.rewards) == arity
            )
            for arity in (1, 2)
        }
        self.assertEqual(assigned_articles_by_arity, original_articles_by_arity)
        self.assertTrue(
            all(len(item.destination.rewards) == len(item.source.rewards) for item in plan.assignments)
        )
        for assignment in plan.assignments:
            self.assertEqual(
                tuple((reward.count, reward.notification) for reward in assignment.assigned_rewards),
                tuple((reward.count, reward.notification) for reward in assignment.destination.rewards),
            )
        self.assertEqual(len(plan.assignments), len(locations))

    # Check x2 pickup keep count notification
    def test_x2_pickup_keeps_its_count_and_notification_after_randomization(self) -> None:
        x2 = _location(20, rewards=(RewardPayload("/Game/Item/DA_X2", 2, False),))
        x1 = _location(21, rewards=(RewardPayload("/Game/Item/DA_X1", 1, True),))

        plan = build_world_loot_randomization_plan((x2, x1), 0)
        assignment = next(item for item in plan.assignments if item.destination == x2)

        self.assertNotEqual(assignment.assigned_rewards[0].article_path, x2.rewards[0].article_path)
        self.assertEqual(assignment.assigned_rewards[0].count, 2)
        self.assertFalse(assignment.assigned_rewards[0].notification)

    # Check exact protection structural rules keep
    def test_exact_protection_and_structural_rules_keep_safe_important_rewards_movable(self) -> None:
        safe = _location(0)
        protected = _location(1, protected=True, reasons=("FLAG_CHANGE_PICKUP",))
        important = _location(
            2,
            rewards=(RewardPayload("/Game/Blueprint/Article/Item/Important/DA_Key", 1, True),),
        )
        scripted = _location(3, category="WORLD_LOOT_UNRESOLVED")
        unsafe = replace(_location(5), source_file="Unsafe.umap")
        plan = build_world_loot_randomization_plan(
            [safe, protected, important, scripted, unsafe],
            15,
            unsafe_packages={"unsafe.umap"},
        )

        self.assertEqual({a.destination for a in plan.assignments}, {safe, important})
        self.assertEqual({entry.location for entry in plan.locked_locations}, {protected, scripted, unsafe})
        self.assertNotIn(important, {entry.location for entry in plan.locked_locations})
        self.assertIn("unsafe package", next(entry.reason for entry in plan.locked_locations if entry.location == unsafe))

    # Check explicit exclusions disabled feature are
    def test_explicit_exclusions_and_disabled_feature_are_reported_without_assignments(self) -> None:
        locations = [_location(i) for i in range(4)]
        excluded = world_loot_slot(locations[0])
        plan = build_world_loot_randomization_plan(locations, 4, excluded_slots={excluded})
        self.assertEqual(len(plan.assignments), 3)
        self.assertEqual(plan.excluded_locations, (locations[0],))

        disabled = build_world_loot_randomization_plan(locations, 4, enabled=False)
        self.assertFalse(disabled.assignments)
        self.assertEqual(set(disabled.disabled_locations), set(locations))

    # Check content class labels do not
    def test_content_class_labels_do_not_disable_existing_locations_or_rewards(self) -> None:
        base_where_dlc_what = replace(
            _location(0),
            rewards=(replace(_location(0).rewards[0], content_class="DLC"),),
        )
        dlc_where_base_what = replace(_location(1), content_class="DLC")
        unknown_where = replace(_location(2), content_class="UNKNOWN")

        plan = build_world_loot_randomization_plan(
            (base_where_dlc_what, dlc_where_base_what, unknown_where),
            3,
        )

        self.assertEqual(len(plan.assignments), 3)
        self.assertEqual(plan.disabled_locations, ())
        self.assertEqual(plan.unavailable_what_locations, ())
        self.assertEqual(plan.eligible_reward_source_count, 3)

    # Check missing reward package removes source
    def test_missing_reward_package_removes_source_without_disabling_world_destination(self) -> None:
        first = _location(0)
        second = _location(1)
        third = _location(2)
        plan = build_world_loot_randomization_plan(
            (first, second, third),
            "optional-world-reward-missing",
            available_reward_packages=(first.rewards[0].article_path, third.rewards[0].article_path),
        )
        self.assertEqual(len(plan.assignments), 2)
        self.assertEqual(plan.eligible_reward_source_count, 2)
        self.assertEqual(len(plan.unmatched_locations), 1)
        self.assertIn(plan.unmatched_locations[0], (first, second, third))
        self.assertEqual(set(plan.unmatched_locations), set((first, second, third)) - {
            assignment.destination for assignment in plan.assignments
        })

    # Check duplicate missing slots fail closed
    def test_duplicate_or_missing_slots_fail_closed(self) -> None:
        first = _location(0)
        with self.assertRaisesRegex(ValueError, "Duplicate World Loot"):
            build_world_loot_randomization_plan([first, first], 1)
        with self.assertRaisesRegex(ValueError, "missing"):
            build_world_loot_randomization_plan([first], 1, excluded_slots={("missing", 5)})

    # Check same seed options repeat exact
    def test_same_seed_and_options_repeat_exact_assignments(self) -> None:
        locations = [_location(i) for i in range(12)]
        first = build_world_loot_randomization_plan(locations, "repeatable-world-seed")
        second = build_world_loot_randomization_plan(locations, "repeatable-world-seed")
        different = build_world_loot_randomization_plan(locations, "other-world-seed")
        signature = lambda plan: tuple((world_loot_slot(a.destination), world_loot_slot(a.source)) for a in plan.assignments)
        self.assertEqual(signature(first), signature(second))
        self.assertNotEqual(signature(first), signature(different))

    # Check unknown class labels do not
    def test_unknown_class_labels_do_not_block_valid_catalog_locations(self) -> None:
        catalogs = CatalogStore().load("current")
        plan = build_world_loot_randomization_plan(
            catalogs.world_loot,
            "catalog-world-seed",
            unsafe_packages=catalogs.unsafe_packages,
        )
        self.assertTrue(plan.assignments)
        self.assertTrue(any(
            location.content_class == "UNKNOWN"
            for location in (assignment.destination for assignment in plan.assignments)
        ))

    # Check magmel crafting vintage wine locations
    def test_magmel_crafting_and_vintage_wine_locations_enter_the_world_pool(self) -> None:
        catalogs = CatalogStore().load("current")
        cases = {
            ("5K52OYKXEGCSACYG5GH7ZLLED.umap", 17): ("DA_Item_MatLevelUpWeaponA_01", 1),
            ("EB1TU8W1GKZYJGYHNDA76XYAH.umap", 26): ("DA_Item_MatLevelUpWeaponA_01", 2),
            ("5K52OYKXEGCSACYG5GH7ZLLED.umap", 18): ("DA_Item_LuxuryWine_1", 1),
        }
        locations = {
            (item.source_file, item.export_index): item for item in catalogs.world_loot
        }
        for slot, (article, count) in cases.items():
            location = locations[slot]
            self.assertEqual(location.actor_category, "GROUND_PICKUP")
            self.assertFalse(location.protected)
            self.assertEqual(location.rewards[0].article_path.rsplit("/", 1)[-1], article)
            self.assertEqual(location.rewards[0].count, count)

        plan = build_world_loot_randomization_plan(
            catalogs.world_loot,
            "magmell-world-loot-regression",
            unsafe_packages=catalogs.unsafe_packages,
        )
        destinations = {
            (item.destination.source_file, item.destination.export_index): item
            for item in plan.assignments
        }
        for slot, (_, count) in cases.items():
            assignment = destinations[slot]
            self.assertEqual(assignment.assigned_rewards[0].count, count)
        self.assertEqual(plan.disabled_locations, ())
        mirrored_assignments = sum(
            assignment.destination.mirror_of_package_path is not None
            for assignment in plan.assignments
        )
        self.assertEqual(
            plan.eligible_reward_source_count + mirrored_assignments,
            len(plan.assignments),
        )
        self.assertEqual(plan.unavailable_what_locations, ())

    # Check storage warehouse keys remain protected
    def test_storage_and_warehouse_keys_remain_protected_world_loot(self) -> None:
        catalogs = CatalogStore().load("current")
        protected_paths = {
            "/Game/Blueprint/Article/Item/DA_Item_Important_StorageKey".casefold(),
            "/Game/Blueprint/Article/Item/DA_Item_Important_WarehouseKey".casefold(),
        }
        locations = tuple(
            location for location in catalogs.world_loot
            if any(reward.article_path.casefold() in protected_paths for reward in location.rewards)
        )
        self.assertEqual(len(locations), 4)
        self.assertEqual(sum(item.mirror_of_package_path is None for item in locations), 2)
        self.assertEqual(sum(item.mirror_of_package_path is not None for item in locations), 2)
        self.assertTrue(all(location.protected for location in locations))
        self.assertTrue(all("KEY_EXCLUDED" in location.protection_reasons for location in locations))

        plan = build_world_loot_randomization_plan(
            catalogs.world_loot,
            "important-key-protection",
            unsafe_packages=catalogs.unsafe_packages,
        )
        locked_by_slot = {world_loot_slot(item.location): item.reason for item in plan.locked_locations}
        self.assertTrue(all(world_loot_slot(location) in locked_by_slot for location in locations))
        self.assertTrue(all("KEY_EXCLUDED" in locked_by_slot[world_loot_slot(location)] for location in locations))
        self.assertFalse(any(
            assignment.source is not None and assignment.source in locations
            for assignment in plan.assignments
        ))
        self.assertFalse(any(
            reward.article_path.casefold() in protected_paths
            for assignment in plan.assignments
            for reward in assignment.assigned_rewards
        ))

    # Check renewable ingredients are locked all
    def test_renewable_ingredients_are_locked_and_all_healing_upgrades_share_world_pool(self) -> None:
        catalogs = CatalogStore().load("current")
        renewable = tuple(
            location for location in catalogs.world_loot
            if location.location_type == "RENEWABLE_INGREDIENT"
        )
        healing = tuple(
            location for location in catalogs.world_loot
            if location.location_type in {
                "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
            }
        )
        inherited_chests = tuple(
            location for location in catalogs.world_loot
            if location.destination_kind == "INSTANCE_ARTICLE_BULK_OVERRIDE"
            and location.actor_class == "BP_it00_002_A_0_C"
        )
        self.assertEqual(len(renewable), 2253)
        self.assertEqual(len({location.rewards[0].article_path for location in renewable}), 47)
        self.assertEqual(len({location.actor_class for location in renewable}), 49)
        self.assertEqual(len({location.package_path for location in renewable}), 301)
        self.assertTrue(all(location.protection_reasons == ("RENEWABLE_INGREDIENT_DISABLED",) for location in renewable))
        self.assertTrue(all(location.structural_class_package for location in renewable))
        self.assertEqual(len(healing), 116)
        self.assertEqual(len(inherited_chests), 3)
        self.assertTrue(all(location.location_type == "NORMAL_WORLD_LOOT" for location in inherited_chests))
        self.assertTrue(all(location.actor_category == "TREASURE_BOX" for location in inherited_chests))
        self.assertTrue(all(location.rewards[0].article_path.endswith("/DA_Item_AmmoRifle") for location in inherited_chests))
        self.assertEqual(catalogs.healing_upgrade_count, 116)
        self.assertEqual(sum(location.location_type.endswith("SEED") for location in healing), 92)
        self.assertEqual(sum(location.location_type.endswith("BEAD") for location in healing), 24)
        self.assertTrue(all(location.physical_instance_count == 1 for location in healing))
        runtime_discovered = {
            (location.package_path.rsplit("/", 1)[-1], location.export_index): location
            for location in healing
            if location.package_path.rsplit("/", 1)[-1] in {
                "DUYRG8IXAYWSVR879I7HM7TF0", "CFIM3HA7UTJ9F5VK2DHC7Q01H",
                "17GRLZPMGQJ8A6AB33I9FIBOH", "18EG8PAIBW1ZW6YIS14CMV1DR",
            }
        }
        self.assertEqual(set(runtime_discovered), {
            ("DUYRG8IXAYWSVR879I7HM7TF0", 8),
            ("CFIM3HA7UTJ9F5VK2DHC7Q01H", 7),
            ("17GRLZPMGQJ8A6AB33I9FIBOH", 18),
            ("18EG8PAIBW1ZW6YIS14CMV1DR", 8),
        })
        self.assertEqual(
            runtime_discovered[("DUYRG8IXAYWSVR879I7HM7TF0", 8)].rewards[0].article_path,
            "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryCountUp_1",
        )
        self.assertEqual(
            runtime_discovered[("CFIM3HA7UTJ9F5VK2DHC7Q01H", 7)].rewards[0].article_path,
            "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryAmountUp_1",
        )

        plan = build_world_loot_randomization_plan(
            catalogs.world_loot,
            "healing-upgrades-physical-locations",
            unsafe_packages=catalogs.unsafe_packages,
        )
        self.assertEqual(sum(item.reason == "RENEWABLE_INGREDIENT_DISABLED" for item in plan.locked_locations), 2253)
        override_assignments = [item for item in plan.assignments if item.destination.instance_override_source is not None]
        assignments = [
            item for item in override_assignments
            if item.destination.location_type in {
                "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
            }
        ]
        inherited_assignments = [item for item in override_assignments if item.destination in inherited_chests]
        self.assertEqual(len(override_assignments), 119)
        self.assertEqual(len(assignments), 116)
        self.assertEqual(len(inherited_assignments), 3)
        self.assertTrue(all(
            item.assigned_rewards[0].article_path != item.destination.rewards[0].article_path
            for item in inherited_assignments
        ))
        self.assertEqual(len({world_loot_slot(item.destination) for item in assignments}), 116)
        self.assertEqual(sum(item.destination.mirror_of_package_path is None for item in assignments), 58)
        healing_slots = {world_loot_slot(location) for location in healing}
        ordinary_into_healing = [
            item for item in plan.assignments
            if world_loot_slot(item.destination) in healing_slots
            and item.source is not None and world_loot_slot(item.source) not in healing_slots
        ]
        healing_into_ordinary = [
            item for item in plan.assignments
            if item.source is not None and world_loot_slot(item.source) in healing_slots
            and world_loot_slot(item.destination) not in healing_slots
        ]
        self.assertTrue(ordinary_into_healing)
        self.assertTrue(healing_into_ordinary)
        for suffix in ("SEED", "BEAD"):
            subset = [item for item in assignments if item.destination.location_type.endswith(suffix)]
            self.assertGreater(len({item.assigned_rewards[0].article_path for item in subset}), 1)
            self.assertTrue(all(
                assigned.count == item.destination.rewards[0].count
                and assigned.notification == item.destination.rewards[0].notification
                for item in subset for assigned in item.assigned_rewards
            ))

        without_healing = build_world_loot_randomization_plan(
            catalogs.world_loot,
            "healing-upgrades-physical-locations",
            unsafe_packages=catalogs.unsafe_packages,
            include_healing=False,
        )
        healing_slots = {world_loot_slot(location) for location in healing}
        self.assertFalse(any(
            world_loot_slot(item.destination) in healing_slots
            or (item.source is not None and world_loot_slot(item.source) in healing_slots)
            for item in without_healing.assignments
        ))
        self.assertEqual(len(without_healing.assignments), len(plan.assignments) - 116)

    # Check material article is excluded renewable
    def test_material_article_is_excluded_by_renewable_location_semantics_only(self) -> None:
        from dataclasses import replace

        catalogs = CatalogStore().load("current")
        renewable = next(
            item for item in catalogs.world_loot
            if item.location_type == "RENEWABLE_INGREDIENT"
            and item.mirror_of_package_path is None
        )
        ordinary = replace(
            renewable,
            package_path=renewable.package_path + "_ordinary",
            source_file=renewable.source_file + ".ordinary",
            export_index=renewable.export_index + 10000,
            export_name="OneTimePickup_C",
            protected=False,
            protection_reasons=(),
            destination_kind="ARTICLE_BULK",
            destination_id=None,
            display_name="One-time pickup with same material Article",
            location_type="NORMAL_WORLD_LOOT",
            structural_class_package=None,
        )
        plan = build_world_loot_randomization_plan((renewable, ordinary), "same-article-different-location")
        self.assertEqual(next(item.reason for item in plan.locked_locations if item.location == renewable), "RENEWABLE_INGREDIENT_DISABLED")
        self.assertIn(ordinary, tuple(item.destination for item in plan.assignments))


if __name__ == "__main__":
    unittest.main()
