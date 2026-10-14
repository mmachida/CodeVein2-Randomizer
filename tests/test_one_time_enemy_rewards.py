from __future__ import annotations

import csv
import hashlib
import json
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from cv2randomizer.game.catalogs import CatalogStore, RewardPayload
from cv2randomizer.game.enemy_drop_catalog import EnemyDropCatalogStore
from cv2randomizer.game.field_enemy_catalog import FieldEnemyCatalogStore
from cv2randomizer.randomizer.world_loot import (
    build_world_loot_randomization_plan,
    filter_enemy_reward_world_locations,
)


ROOT = Path(__file__).resolve().parents[1]
ADRENALINE_ID = "one_time_enemy_mm_medium08"
ADRENALINE_ARTICLE = "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportAttack/DA_GB_CV19_AtkUpFixed"


class OneTimeEnemyRewardTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        self.audit_path = ROOT / "data" / "enemy_drops" / "one_time_enemy_rewards.json"
        self.audit = json.loads(self.audit_path.read_text(encoding="utf-8"))
        self.slots = {
            item.destination_id: item
            for item in self.catalogs.world_loot
            if item.destination_id and item.destination_id.startswith("one_time_enemy_")
        }

    # Check all confirmed one time enemy
    def test_all_confirmed_one_time_enemy_rewards_are_world_locations(self) -> None:
        self.assertEqual(len(self.slots), 15)
        self.assertEqual(
            {item["location_id"] for item in self.audit["reward_slots"]},
            set(self.slots),
        )
        for record in self.audit["reward_slots"]:
            slot = self.slots[record["location_id"]]
            self.assertEqual(slot.destination_kind, "QUEST_ARTICLE_GRANT")
            self.assertEqual(slot.quest_article_layout.expected_article_path, record["vanilla_article"])
            self.assertEqual(slot.quest_article_layout.count, record["count"])
            self.assertIs(slot.quest_article_layout.notification, True)
            self.assertFalse(slot.protected)
            self.assertEqual(record["respawn_classification"], "ONE_TIME_ENEMY")
            self.assertIn("QSNode_Completed", record["respawn_evidence"]["completion_graph_signals"])
            self.assertIn("QSTask_SetStoryFlagByBoolean", record["respawn_evidence"]["completion_graph_signals"])
            self.assertTrue(record["eligible_world_source"])
            self.assertTrue(record["eligible_world_destination"])
            self.assertTrue(record["planner_present"])

    # Check enemy checkbox gates all audited
    def test_enemy_checkbox_gates_all_audited_one_time_enemy_world_locations(self) -> None:
        field_enemy = FieldEnemyCatalogStore(ROOT / "data" / "field_enemy").load(
            "current", important_item_exclusions=self.catalogs.important_item_exclusions,
        )
        all_enemy_locations = (*self.catalogs.world_loot, *field_enemy.locations)
        enabled_locations = filter_enemy_reward_world_locations(all_enemy_locations, enabled=True)
        disabled_locations = filter_enemy_reward_world_locations(all_enemy_locations, enabled=False)
        enabled_ids = {location.destination_id for location in enabled_locations}
        disabled_ids = {location.destination_id for location in disabled_locations}

        self.assertEqual(len(self.slots), 15)
        self.assertEqual(len(field_enemy.locations), 24)
        self.assertEqual(sum(
            location.destination_id.startswith("harmony_moon_")
            for location in self.catalogs.world_loot if location.destination_id
        ), 5)
        self.assertTrue(set(self.slots).issubset(enabled_ids))
        self.assertTrue(field_enemy.destination_ids.issubset(enabled_ids))
        self.assertTrue(all(
            location.destination_id not in disabled_ids
            for location in all_enemy_locations
            if location.destination_id and location.destination_id.casefold().startswith(
                ("one_time_enemy_", "harmony_moon_", "qst_fieldenemy_")
            )
        ))
        self.assertTrue(any(
            location.destination_id in disabled_ids
            for location in self.catalogs.world_loot
            if location.destination_id == "jadwiga_servant_booster_dex_reward"
        ))

    # Check adrenaline formae is exact alexis
    def test_adrenaline_formae_is_the_exact_alexis_reward_and_randomizes(self) -> None:
        slot = self.slots[ADRENALINE_ID]
        record = next(item for item in self.audit["reward_slots"] if item["location_id"] == ADRENALINE_ID)
        self.assertEqual(record["enemy_name"], "Alexis the Heartless Bodyguard")
        self.assertEqual(record["map"], "East Bay Underground Ruins")
        self.assertEqual(record["vanilla_article"], ADRENALINE_ARTICLE)
        self.assertEqual(
            record["texture_asset"],
            "/Game/UI/Common/Textures/Gadget/Weapon/T_UI_SGadget_StrikeRiser",
        )
        self.assertEqual(slot.quest_article_layout.export_name, "QSTask_GainItem_0")

        plan = build_world_loot_randomization_plan(
            self.catalogs.world_loot,
            "one-time-enemy-adrenaline-regression",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        assignment = next(item for item in plan.assignments if item.destination.destination_id == ADRENALINE_ID)
        self.assertNotEqual(assignment.assigned_rewards[0].article_path, ADRENALINE_ARTICLE)
        self.assertEqual(assignment.destination.rewards[0].count, assignment.assigned_rewards[0].count)
        self.assertEqual(assignment.destination.rewards[0].notification, assignment.assigned_rewards[0].notification)

    # Check jadwiga dex booster direct grant
    def test_jadwiga_dex_booster_direct_grant_randomizes_in_world_pool(self) -> None:
        location = next(
            item for item in self.catalogs.world_loot
            if item.destination_id == "jadwiga_servant_booster_dex_reward"
        )
        vanilla_article = "/Game/Blueprint/Article/Booster/Basic/DA_Booster_Dex_1"
        self.assertEqual(location.destination_kind, "RAW_ARTICLE_GRANT")
        self.assertFalse(location.protected)
        self.assertEqual(location.rewards[0].article_path, vanilla_article)

        plan = build_world_loot_randomization_plan(
            self.catalogs.world_loot,
            "booster-dex-eligibility",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        assignment = next(
            item for item in plan.assignments
            if item.destination.destination_id == location.destination_id
        )
        self.assertNotEqual(assignment.assigned_rewards[0].article_path, vanilla_article)
        assigned_articles = Counter(
            reward.article_path.casefold()
            for item in plan.assignments
            if item.destination.mirror_of_package_path is None
            for reward in item.assigned_rewards
        )
        self.assertEqual(
            assigned_articles,
            Counter(path.casefold() for path in plan.source_article_paths),
        )

    # Check enemy defeat DLC articles are
    def test_enemy_defeat_dlc_articles_are_independent_world_loot_locations(self) -> None:
        expected = {
            "one_time_enemy_dlc_e90153_female_knight": (
                "/Game/Blueprint/Article/Gadget/DLC/WeaponAddOn/Support/DA_GB_UNQ09_IchorRevoveryOnJustGuard",
                "/Game/Quest/DLC/QST_MainEpisode_DLC_e90153",
            ),
            "one_time_enemy_dlc_e90193_zenon_servant": (
                "/Game/Blueprint/Article/Gadget/DLC/WeaponAddOn/DA_SpinFireSlam",
                "/Game/Quest/DLC/QST_MainEpisode_DLC_e90193",
            ),
        }
        for location_id, (article, package) in expected.items():
            slot = self.slots[location_id]
            record = next(item for item in self.audit["reward_slots"] if item["location_id"] == location_id)
            self.assertEqual(slot.package_path, package)
            self.assertEqual(slot.quest_article_layout.expected_article_path, article)
            self.assertEqual(slot.quest_article_layout.count, 1)
            self.assertIs(slot.quest_article_layout.notification, True)
            self.assertEqual(record["respawn_evidence"]["reachable_graph_path"], [
                "QSNode_Event_6", "QSNode_Slot_23", "QSTask_GainItem_0",
            ])
            self.assertEqual(record["literal_fname"]["package_name"]["number"], 0)
            self.assertEqual(record["literal_fname"]["asset_name"]["number"], 0)
            self.assertTrue(record["eligible_world_source"])
            self.assertTrue(record["eligible_world_destination"])

        graph_audit = self.audit["quest_graph_audit"]
        self.assertEqual(graph_audit["classification_counts"]["key_quest_or_progression_excluded"], 22)
        self.assertEqual(graph_audit["classification_counts"]["blood_code_routed_to_blood_code_pool"], 8)
        self.assertEqual(graph_audit["classification_counts"]["repeatable_multiplayer_rewards_excluded"], 6)
        self.assertEqual(len(graph_audit["protected_key_quest_progression_slots"]), 22)
        self.assertEqual(len(graph_audit["blood_code_slots_kept_in_dedicated_pool"]), 8)

    # Check key slot stays protected while
    def test_key_slot_stays_protected_while_ordinary_enemy_reward_remains_eligible(self) -> None:
        boss = self.slots[ADRENALINE_ID]
        key = replace(
            boss,
            export_index=boss.export_index + 1000,
            destination_id="diagnostic_key_reward",
            display_name="Diagnostic protected enemy key reward",
            protected=True,
            protection_reasons=("KEY_ITEM",),
            rewards=(RewardPayload("/Game/Blueprint/Article/Item/DA_Dungeon_FOC_RBMedium11_Key01", 1, True),),
        )
        other = next(
            item for item in self.catalogs.world_loot
            if item.destination_kind == "ARTICLE_BULK" and not item.protected and len(item.rewards) == 1
        )
        plan = build_world_loot_randomization_plan(
            (boss, key, other), "boss-key-and-ordinary-reward", unsafe_packages=self.catalogs.unsafe_packages,
        )
        self.assertIn(key, tuple(item.location for item in plan.locked_locations))
        self.assertNotIn(key, tuple(item.destination for item in plan.assignments))
        self.assertIn(boss, tuple(item.destination for item in plan.assignments))
        self.assertNotIn(key.rewards[0].article_path, tuple(
            reward.article_path for item in plan.assignments for reward in item.assigned_rewards
        ))

    # Check respawnable enemy drop pool remain
    def test_respawnable_enemy_drop_pool_remains_separate_and_key_audit_is_complete(self) -> None:
        enemy_drops = EnemyDropCatalogStore(ROOT / "data" / "enemy_drops").load("current")
        self.assertEqual(len(enemy_drops.rows), 233)
        report = self.audit["audit_scope"]
        self.assertEqual(report["field_enemy_existing_eligible_locations"], 24)
        self.assertEqual(report["field_enemy_existing_protected_key_or_progression_locations"], 10)
        self.assertEqual(report["field_enemy_quests_without_article_reward"], 7)
        self.assertEqual(report["enemy_defeat_graphs_parsed"], 76)
        self.assertEqual(report["unresolved_generic_enemy_drop_rows_left_unchanged"], 32)
        self.assertNotIn("unexplained_missing_in_scanned_quest_groups", report)
        self.assertEqual(len(self.audit["protected_existing_field_enemy_rewards"]), 10)

        manifest = json.loads((ROOT / "data" / "enemy_drops" / "manifest.json").read_text(encoding="utf-8"))
        audit_entry = manifest["one_time_enemy_rewards"]
        csv_path = ROOT / "data" / "enemy_drops" / audit_entry["csv_file"]
        self.assertEqual(hashlib.sha256(self.audit_path.read_bytes()).hexdigest(), audit_entry["json_sha256"])
        self.assertEqual(hashlib.sha256(csv_path.read_bytes()).hexdigest(), audit_entry["csv_sha256"])
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(len(tuple(csv.DictReader(handle))), 15)


if __name__ == "__main__":
    unittest.main()
