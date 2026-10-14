from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path

from cv2randomizer.game.booster_reward_pool import BoosterRewardCandidate, build_extractor_booster_pool
from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.extractor_catalog import ExtractorCatalogStore
from cv2randomizer.randomizer.booster_rewards import exchange_extractor_world_sources
from cv2randomizer.randomizer.extractor import ExtractorPlanError, build_extractor_plan
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan


ROOT = Path(__file__).resolve().parents[1]


class ExtractorRandomizerTests(unittest.TestCase):
    # Set Up Class
    @classmethod
    def setUpClass(cls) -> None:
        cls.extractor = ExtractorCatalogStore().load("current")
        cls.runtime = CatalogStore().load("current")
        cls.source_candidates = tuple(
            BoosterRewardCandidate(slot.original_article_path, slot.content_class, ("test-extractor-source",))
            for slot in cls.extractor.slots
        )

    # Check pool use recipe physical world
    def test_pool_uses_recipe_and_physical_world_occurrences_not_mastery(self) -> None:
        self.assertEqual(Counter(table.content_class for table in self.extractor.tables), {"BASE": 1, "DLC": 1})
        self.assertEqual(Counter(slot.content_class for slot in self.extractor.slots), {"BASE": 36, "DLC": 7})
        self.assertEqual(len(self.extractor.slots), 43)
        locations = tuple(
            item for item in self.runtime.world_loot
            if item.mirror_of_package_path is None
            and any(reward.article_path.casefold().startswith("/game/blueprint/article/booster/")
                    for reward in item.rewards)
        )
        world_plan = build_world_loot_randomization_plan(
            locations, "extractor-physical-booster-pool",
            unsafe_packages=self.runtime.unsafe_packages,
        )
        pool = build_extractor_booster_pool(
            extractor=self.extractor,
            world_plan=world_plan,
        )
        identities = [item.article_path.casefold() for item in pool]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertTrue(all(item.article_path.startswith("/Game/Blueprint/Article/Booster/") for item in pool))
        self.assertEqual(len(pool), 53)  # 43 recipe outputs plus 10 physical World grants
        self.assertEqual(sum(origin.startswith("extractor-recipe:") for item in pool for origin in item.origins), 43)
        self.assertEqual(sum(origin.startswith("world-loot-occurrence:") for item in pool for origin in item.origins), 10)
        recipe_paths = tuple(item.original_article_path for item in self.extractor.slots)
        recipe_only = build_extractor_booster_pool(
            extractor=self.extractor,
            world_plan=world_plan,
            available_reward_packages=recipe_paths,
        )
        self.assertEqual(len(recipe_only), 43)
        self.assertTrue(all(origin.startswith("extractor-recipe:") for item in recipe_only for origin in item.origins))

    # Check available recipe tables candidate assets
    def test_only_available_recipe_tables_and_candidate_assets_are_planned(self) -> None:
        base_table = next(table for table in self.extractor.tables if table.content_class == "BASE")
        available_catalog = type(self.extractor)(self.extractor.revision, (base_table,))
        candidates = tuple(item for item in self.source_candidates if item.content_class == "BASE")
        plan = build_extractor_plan(
            available_catalog, candidates, "extractor-optional-absent",
            mix_postgame_dlc_with_base=True,
        )
        self.assertEqual(len(plan.assignments), 36)
        self.assertTrue(all(item.destination.content_class == "BASE" for item in plan.assignments))
        self.assertEqual({item.article_path for item in plan.remaining_candidates}, set())

    # Check mix off preserve class pools
    def test_mix_off_preserves_class_pools_and_mix_on_uses_one_no_replacement_pool(self) -> None:
        separate = build_extractor_plan(
            self.extractor, self.source_candidates, "extractor-pools",
            mix_postgame_dlc_with_base=False,
        )
        self.assertEqual(len(separate.assignments), 43)
        self.assertTrue(all(item.destination.content_class == item.assigned_reward.content_class for item in separate.assignments))
        self.assertEqual({item.pool for item in separate.pool_stats}, {"BASE", "DLC"})

        mixed = build_extractor_plan(
            self.extractor, self.source_candidates, "extractor-pools",
            mix_postgame_dlc_with_base=True,
        )
        self.assertEqual(len(mixed.assignments), 43)
        self.assertEqual({item.pool for item in mixed.pool_stats}, {"ALL"})
        assigned_paths = [item.assigned_reward.article_path.casefold() for item in mixed.assignments]
        self.assertEqual(len(assigned_paths), len(set(assigned_paths)))
        self.assertEqual(set(assigned_paths), {item.article_path.casefold() for item in self.source_candidates})

    # Check unallocated candidates partition class seed
    def test_unallocated_candidates_partition_by_class_and_seed_is_reproducible(self) -> None:
        candidates = (
            *self.source_candidates,
            BoosterRewardCandidate("/Game/Blueprint/Article/Booster/Test/DA_Booster_Extra_Base", "BASE", ("test",)),
            BoosterRewardCandidate("/Game/Blueprint/Article/Booster/Test/DA_Booster_Extra_DLC", "DLC", ("test",)),
        )
        first = build_extractor_plan(
            self.extractor, candidates, "extractor-partition",
            mix_postgame_dlc_with_base=False,
        )
        self.assertEqual(first, build_extractor_plan(
            self.extractor, candidates, "extractor-partition",
            mix_postgame_dlc_with_base=False,
        ))
        self.assertEqual(len(first.remaining_candidates), 2)
        self.assertEqual(Counter(item.content_class for item in first.remaining_candidates), {"BASE": 1, "DLC": 1})
        combined = [item.assigned_reward.article_path.casefold() for item in first.assignments]
        combined.extend(item.article_path.casefold() for item in first.remaining_candidates)
        self.assertEqual(Counter(combined), Counter(item.article_path.casefold() for item in candidates))

    # Check duplicate canonical identities fail closed
    def test_duplicate_canonical_identities_fail_closed(self) -> None:
        duplicate = (*self.source_candidates, BoosterRewardCandidate(
            self.source_candidates[0].article_path.upper(), "BASE", ("duplicate",),
        ))
        with self.assertRaisesRegex(ExtractorPlanError, "duplicate Article identities"):
            build_extractor_plan(
                self.extractor, duplicate, 1,
                mix_postgame_dlc_with_base=False,
            )

    # Check direct dex booster transfers into
    def test_direct_dex_booster_transfers_into_extractor_without_duplicating_occurrence(self) -> None:
        locations = tuple(
            item for item in self.runtime.world_loot
            if item.mirror_of_package_path is None
            and any(reward.article_path.casefold().startswith("/game/blueprint/article/booster/")
                    for reward in item.rewards)
        )
        world_plan = build_world_loot_randomization_plan(
            locations, "extractor-direct-dex-world",
            unsafe_packages=self.runtime.unsafe_packages,
        )
        pool = build_extractor_booster_pool(
            extractor=self.extractor,
            world_plan=world_plan,
        )
        dex_path = "/Game/Blueprint/Article/Booster/Basic/DA_Booster_Dex_1"
        self.assertIn(dex_path.casefold(), {item.article_path.casefold() for item in pool})
        selected = None
        for seed in range(128):
            candidate_plan = build_extractor_plan(
                self.extractor, pool, seed,
                mix_postgame_dlc_with_base=True,
            )
            if any(item.assigned_reward.article_path.casefold() == dex_path.casefold()
                   for item in candidate_plan.assignments):
                selected = candidate_plan
                break
        self.assertIsNotNone(selected, "the direct NPC Dex Booster should be eligible for an Extractor recipe")
        plan = selected
        original_world_paths = [
            reward.article_path
            for assignment in world_plan.assignments
            for reward in assignment.source_candidate.rewards
        ]
        original_recipe_paths = [slot.original_article_path for slot in self.extractor.slots]
        updated_world = exchange_extractor_world_sources(world_plan, plan)
        final_world_paths = [
            reward.article_path
            for assignment in updated_world.assignments
            for reward in assignment.assigned_rewards
        ]
        final_recipe_paths = [item.assigned_reward.article_path for item in plan.assignments]
        self.assertEqual(
            Counter(path.casefold() for path in (*original_world_paths, *original_recipe_paths)),
            Counter(path.casefold() for path in (*final_world_paths, *final_recipe_paths)),
        )
        self.assertEqual(sum(path.casefold() == dex_path.casefold() for path in final_world_paths + final_recipe_paths), 1)


if __name__ == "__main__":
    unittest.main()
