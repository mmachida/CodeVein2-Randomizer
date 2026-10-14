from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.article_reward_catalog import CanonicalArticleRewardCatalogStore
from cv2randomizer.game.raw_article_grant_generation import (
    RawArticleGrantGenerationError,
    RawArticleGrantSerializer,
)
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.game.jail_catalog import JailCatalogStore
from cv2randomizer.randomizer.defensive_gadgets import defensive_gadget_world_candidates
from cv2randomizer.randomizer.jails import jail_world_candidates
from cv2randomizer.randomizer.article_rewards import canonical_article_world_candidates
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan
from tests._content_class_fixtures import classified_world_fixture


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = (
    ROOT / "workspace" / "cache" / "defensive-gadget-research" / "Content" / "Cinematics"
    / "CinematicScene" / "Scenario" / "SmallScenario" / "Blueprint" / "Common" / "Test"
    / "JadwigaServant"
)
SOURCES = {
    "jadwiga_servant_lightning_barrage_reward": SOURCE_ROOT / "BP_SmallScenario_JadwigaServant1.uasset",
    "jadwiga_servant_booster_dex_reward": SOURCE_ROOT / "BP_SmallScenario_JadwigaServant2.uasset",
    "jadwiga_servant_initial_parry_reward": SOURCE_ROOT / "BP_SmallScenario_JadwigaServant3.uasset",
}
MAPPINGS = ROOT / "workspace" / "research_assets" / "CV2.usmap"
ARTICLE_CASES = (
    "/Game/Blueprint/Article/Gadget/StandAlone/DA_Bow",
    "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Warp_2",
    "/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight_1",
)


class DirectWorldLootRewardPoolTests(unittest.TestCase):
    # Check direct rewards hot spring bundles
    def test_direct_rewards_and_hot_spring_bundles_are_generic_world_loot_destinations(self) -> None:
        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        gadget_catalog = DefensiveGadgetCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        jail_catalog = JailCatalogStore(ROOT / "data" / "jails" / "current").load()
        article_catalog = CanonicalArticleRewardCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        plan = build_world_loot_randomization_plan(
            classified_world_fixture(catalogs.world_loot),
            "jadwiga-generic-world-seed",
            unsafe_packages=catalogs.unsafe_packages,
            catalog_candidates=(
                *defensive_gadget_world_candidates(gadget_catalog),
                *jail_world_candidates(jail_catalog),
                *canonical_article_world_candidates(article_catalog),
            ),
        )
        raw_direct = [
            assignment for assignment in plan.assignments
            if assignment.destination.destination_kind == "RAW_ARTICLE_GRANT"
        ]
        quest_direct = [
            assignment for assignment in plan.assignments
            if assignment.destination.destination_kind == "QUEST_ARTICLE_GRANT"
        ]
        self.assertEqual(
            len(plan.assignments),
            sum(not location.protected for location in catalogs.world_loot),
        )
        self.assertEqual({item.destination.destination_id for item in raw_direct}, set(SOURCES))
        self.assertEqual(len(raw_direct), 3)
        self.assertEqual(len(quest_direct), 77)
        self.assertEqual(
            {item.destination.destination_id for item in quest_direct},
            {
                item.destination_id for item in catalogs.world_loot
                if item.destination_id is not None
                and item.destination_kind == "QUEST_ARTICLE_GRANT"
            },
        )
        self.assertEqual(plan.booster_candidate_count, 3)
        self.assertEqual(plan.booster_injected_count, 0)
        self.assertEqual(plan.booster_duplicate_sources, 0)
        self.assertTrue(all(len(item.assigned_rewards) == 1 for item in raw_direct))
        self.assertEqual(sum(len(item.assigned_rewards) == 1 for item in quest_direct), 63)
        self.assertEqual(sum(len(item.assigned_rewards) == 2 for item in quest_direct), 14)
        hot_spring_assignments = [
            item for item in quest_direct if item.destination.destination_id.startswith("hot_spring_")
        ]
        self.assertEqual(len(hot_spring_assignments), 10)
        self.assertEqual(sum(len(item.assigned_rewards) for item in hot_spring_assignments), 19)
        self.assertTrue(all(
            not item.assigned_rewards[0].article_path.startswith("/Game/Blueprint/Article/Gadget/Defence/")
            for item in raw_direct
        ))
        self.assertEqual(
            {item.destination.display_name for item in raw_direct},
            {
                "Jadwiga Servant - Lightning Barrage Reward",
                "Jadwiga Servant - Booster Dex Reward",
                "Jadwiga Servant - Initial Parry Reward",
            },
        )
        singleton_world_rewards = {
            location.rewards[0].article_path
            for location in catalogs.world_loot
            if not location.protected and len(location.rewards) == 1
        }
        self.assertTrue(all(item.assigned_rewards[0].article_path in singleton_world_rewards for item in raw_direct))
        self.assertTrue(all(
            item.assigned_rewards[0].article_path.startswith("/Game/Blueprint/Article/")
            for item in quest_direct
        ))

    # Check malformed raw destination fail closed
    def test_malformed_raw_destination_fails_closed_in_planner(self) -> None:
        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        raw_slot = next(location for location in catalogs.world_loot if location.destination_id)
        from dataclasses import replace

        bad_slot = replace(raw_slot, raw_article_layout=None)
        plan = build_world_loot_randomization_plan((bad_slot,), "bad-jadwiga-layout")
        self.assertEqual(plan.assignments, ())
        self.assertEqual(plan.locked_locations[0].reason, "unvalidated direct Article grant destination")

    # Check malformed structured quest destination fail
    def test_malformed_structured_quest_destination_fails_closed_in_planner(self) -> None:
        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        quest_slot = next(location for location in catalogs.world_loot if location.destination_kind == "QUEST_ARTICLE_GRANT")
        from dataclasses import replace

        bad_slot = replace(quest_slot, quest_article_layout=None)
        plan = build_world_loot_randomization_plan((bad_slot,), "bad-quest-article-layout")
        self.assertEqual(plan.assignments, ())
        self.assertEqual(plan.locked_locations[0].reason, "unvalidated structured quest Article grant destination")


@unittest.skipUnless(shutil.which("dotnet"), "dotnet SDK is not available")
class DirectRawSerializerFixtureTests(unittest.TestCase):
    # Check all slot definitions share literal
    def test_all_slot_definitions_share_literal_name_and_zero_number_serializer(self) -> None:
        result = subprocess.run(
            [
                "dotnet", "run", "--project", str(ROOT / "tools" / "uassetapi-worldloot" / "UAssetAPIWorldLoot.csproj"),
                "--no-restore", "--", "test-raw-article-grant-fixtures",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 9)
        self.assertEqual({row["destinationId"] for row in rows}, set(SOURCES))
        vanilla_numbers = {
            "jadwiga_servant_lightning_barrage_reward": 0,
            "jadwiga_servant_booster_dex_reward": 2,
            "jadwiga_servant_initial_parry_reward": 2,
        }
        for destination_id in SOURCES:
            slot_rows = [row for row in rows if row["destinationId"] == destination_id]
            self.assertEqual([row["requestedPath"] for row in slot_rows], list(ARTICLE_CASES))
            for row in slot_rows:
                self.assertEqual(row["vanillaNumber"], vanilla_numbers[destination_id])
                requested = row["requestedPath"]
                object_name = requested.rsplit("/", 1)[-1]
                self.assertEqual(row["nameMapPath"], requested)
                self.assertEqual(row["nameMapObject"], object_name)
                self.assertEqual((row["pathNumber"], row["objectNumber"]), (0, 0))
                self.assertTrue(row["unrelatedRawBytesPreserved"])


@unittest.skipUnless(all(path.is_file() for path in SOURCES.values()) and MAPPINGS.is_file(), "local direct reward research assets and CV2.usmap are not available")
class DirectRawSerializerRealAssetTests(unittest.TestCase):
    # Check all slots accept generic article
    def test_all_slots_accept_generic_article_paths_and_preserve_their_sources(self) -> None:
        slots = {
            location.destination_id: location
            for location in CatalogStore(ROOT / "data" / "catalogs").load("current").world_loot
            if location.destination_id in SOURCES
        }
        serializer = RawArticleGrantSerializer()
        with tempfile.TemporaryDirectory(prefix="cv2-direct-raw-article-") as temporary:
            root = Path(temporary).resolve()
            for destination_id, source in SOURCES.items():
                slot = slots[destination_id]
                source_members = tuple(
                    path for path in (source, source.with_suffix(".uexp"), source.with_suffix(".ubulk"), source.with_suffix(".uptnl"))
                    if path.is_file()
                )
                original_hashes = {path.suffix: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_members}
                relative = Path(*slot.package_path.split("/")[2:]).with_suffix(".uasset")
                source_copy = root / "source" / destination_id / "Content" / relative
                source_copy.parent.mkdir(parents=True)
                for original in source_members:
                    shutil.copyfile(original, source_copy.with_suffix(original.suffix))
                before = serializer.inspect(
                    work_root=root,
                    mappings_path=MAPPINGS,
                    slot=slot,
                    input_file=source_copy,
                )
                self.assertEqual(before.export_index, 2)
                self.assertEqual(before.serial_size, 55)
                self.assertEqual(before.article_path, slot.raw_article_layout.expected_vanilla_article_path)
                self.assertEqual((before.path_name_index, before.object_name_index), (0, 27))
                expected_vanilla_number = slot.raw_article_layout.expected_vanilla_number
                self.assertEqual((before.path_number, before.object_number), (expected_vanilla_number, expected_vanilla_number))

                for index, article_path in enumerate(ARTICLE_CASES):
                    output = root / "staging" / destination_id / str(index) / "Content" / relative
                    output.parent.mkdir(parents=True)
                    patched, files = serializer.patch(
                        work_root=root,
                        mappings_path=MAPPINGS,
                        slot=slot,
                        input_file=source_copy,
                        output_file=output,
                        assigned_article_path=article_path,
                    )
                    self.assertTrue(output.is_file())
                    self.assertTrue(output.with_suffix(".uexp").is_file())
                    self.assertEqual(patched.article_path, article_path)
                    self.assertEqual(patched.path_name_map_value, article_path)
                    self.assertEqual(patched.object_name_map_value, article_path.rsplit("/", 1)[-1])
                    self.assertEqual((patched.path_number, patched.object_number), (0, 0))
                    self.assertTrue(all(path.is_file() for path in files))
                self.assertEqual(
                    {path.suffix: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_members},
                    original_hashes,
                )

    # Check each slot reject source pair
    def test_each_slot_rejects_a_source_pair_with_a_changed_fingerprint(self) -> None:
        slots = {
            location.destination_id: location
            for location in CatalogStore(ROOT / "data" / "catalogs").load("current").world_loot
            if location.destination_id in SOURCES
        }
        serializer = RawArticleGrantSerializer()
        with tempfile.TemporaryDirectory(prefix="cv2-direct-fingerprint-") as temporary:
            root = Path(temporary).resolve()
            for destination_id, source in SOURCES.items():
                slot = slots[destination_id]
                relative = Path(*slot.package_path.split("/")[2:]).with_suffix(".uasset")
                source_copy = root / destination_id / "Content" / relative
                source_copy.parent.mkdir(parents=True)
                shutil.copyfile(source, source_copy)
                shutil.copyfile(source.with_suffix(".uexp"), source_copy.with_suffix(".uexp"))
                with source_copy.open("ab") as handle:
                    handle.write(b"changed")
                with self.assertRaises(RawArticleGrantGenerationError):
                    serializer.inspect(
                        work_root=root,
                        mappings_path=MAPPINGS,
                        slot=slot,
                        input_file=source_copy,
                    )


if __name__ == "__main__":
    unittest.main()
