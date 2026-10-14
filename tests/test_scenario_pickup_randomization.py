from __future__ import annotations

import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from cv2randomizer.game.catalogs import (
    CatalogStore,
    SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO,
    is_valid_scripted_reward_only_location,
)
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan


ROOT = Path(__file__).resolve().parents[1]
REPORT_ARTICLES = {
    "/Game/Blueprint/Article/Item/DA_Report_A",
    "/Game/Blueprint/Article/Item/DA_Report_B",
    "/Game/Blueprint/Article/Item/DA_Report_C",
}


class ScenarioPickupRandomizationTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        self.locations = tuple(
            item for item in self.catalogs.world_loot
            if item.rewards[0].article_path in REPORT_ARTICLES
        )

    # Check exact report pickups are randomized
    def test_exact_report_pickups_are_randomized_and_mirrors_stay_one_occurrence(self) -> None:
        self.assertEqual(len(self.locations), 6)
        self.assertEqual(sum(item.mirror_of_package_path is None for item in self.locations), 3)
        self.assertTrue(all(
            item.scripted_reward_mode == SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO
            and not item.protected
            and not item.protection_reasons
            and is_valid_scripted_reward_only_location(item)
            for item in self.locations
        ))

        plan = build_world_loot_randomization_plan(
            self.catalogs.world_loot,
            "scenario-report-one-for-one",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        assigned_destinations = {
            (item.destination.package_path.casefold(), item.destination.export_index)
            for item in plan.assignments
        }
        self.assertTrue(all(
            (item.package_path.casefold(), item.export_index) in assigned_destinations
            for item in self.locations
        ))

        canonical_assigned_articles = Counter(
            reward.article_path.casefold()
            for item in plan.assignments
            if item.destination.mirror_of_package_path is None
            for reward in item.assigned_rewards
        )
        self.assertEqual(
            canonical_assigned_articles,
            Counter(path.casefold() for path in plan.source_article_paths),
        )

    # Check scenario exception fail closed identity
    def test_scenario_exception_fails_closed_when_identity_or_actor_changes(self) -> None:
        canonical = next(item for item in self.locations if item.mirror_of_package_path is None)
        self.assertFalse(is_valid_scripted_reward_only_location(replace(canonical, export_index=99)))
        self.assertFalse(is_valid_scripted_reward_only_location(replace(canonical, save_id="different")))
        self.assertFalse(is_valid_scripted_reward_only_location(replace(canonical, actor_class="BP_Other_C")))
        self.assertFalse(is_valid_scripted_reward_only_location(replace(canonical, protected=True)))


if __name__ == "__main__":
    unittest.main()
