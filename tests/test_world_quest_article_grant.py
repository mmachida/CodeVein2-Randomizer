from __future__ import annotations

import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.catalogs import CatalogStore, RewardPayload, WorldLootLocation
from cv2randomizer.game.article_reward_catalog import CanonicalArticleRewardCatalogStore
from cv2randomizer.game.raw_article_grant_generation import (
    RawArticleGrantSerializer,
    apply_world_loot_direct_grants,
)
from cv2randomizer.game.blood_code_generation import BloodCodeSerializer
from cv2randomizer.randomizer.world_loot import (
    WorldLootAssignment,
    WorldLootRandomizationPlan,
    WorldLootRewardCandidate,
    build_world_loot_randomization_plan,
)
from cv2randomizer.randomizer.article_rewards import canonical_article_world_candidates
from tests._content_class_fixtures import classified_world_fixture


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "/Game/Quest/DLC/QST_MainEpisode_DLC_e90360"
SOURCE = (
    ROOT / "workspace" / "blood-codes-boosters-local-test"
    / "20260927T003340Z-bc-booster-local-test-20260926" / "source" / "Content"
    / "Quest" / "DLC" / "QST_MainEpisode_DLC_e90360.uasset"
)
MAPPINGS = ROOT / "workspace" / "research_assets" / "CV2.usmap"
MAGMELL_SOURCE = (
    ROOT / "workspace" / "magmell-slot2-diagnostic" / "input" / "HotSpringMagmel.uasset"
)
HARMONY_IDS = (
    "harmony_moon_holly_reward",
    "harmony_moon_josee_reward",
    "harmony_moon_lyle_reward",
    "harmony_moon_noah_reward",
    "harmony_moon_zenon_reward",
)
HARMONY_NAMES = ("Holly", "Josee", "Lyle", "Noah_02", "Zenon")
HARMONY_CONTENT = ROOT / "workspace" / "cache" / "harmony-moon-source" / "Content"
HARMONY_SOURCES = {
    f"harmony_moon_{name.removesuffix('_02').casefold()}_reward": (
        HARMONY_CONTENT / "Quest" / "LastEpisode_HarmonyMoon" / f"QST_LastEpisode_HarmonyMoon_Memory_{name}.uasset"
    )
    for name in HARMONY_NAMES
}
ASSIGNED_ARTICLES = (
    "/Game/Blueprint/Article/Gadget/StandAlone/DA_Bow",
    "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Warp_2",
    "/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight_1",
)
JOSEE_TEST_ARTICLES = (
    "/Game/Blueprint/Article/Gadget/StandAlone/DA_Bow",
    "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Warp_1",
    "/Game/Blueprint/Article/Booster/Other/DA_Booster_NoiseReduceWalk_1",
)
IRIS_SAMPLE_ID = "iris_character_episode_initial_reward"
IRIS_PAIR_IDS = ("iris_reward_1", "iris_reward_2")
IRIS_BOOSTER_PATHS = (
    "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIrisSample_1",
    "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIris_1",
    "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIris_2",
)
IRIS_CONTENT = ROOT / "workspace" / "cache" / "iris-source" / "Content" / "Quest" / "CharacterEpisode" / "CharacterEpisode_Iris"
IRIS_SOURCES = {
    "iris_character_episode_initial_reward": IRIS_CONTENT / "QST_CharacterEpisode_Iris_c00010.uasset",
    "iris_reward_1": IRIS_CONTENT / "QST_CharacterEpisode_Iris_c00070.uasset",
}
IRIS_PAIR_ARTICLES = (
    "/Game/Blueprint/Article/Gadget/StandAlone/DA_IaiKatana",
    "/Game/Blueprint/Article/Gadget/StandAlone/DA_IdrisWing",
)


class _Extractor:
    # Extract requested game assets
    def extract_assets(self, _game_root, packages, **_kwargs):
        if tuple(packages) != (PACKAGE,):
            raise AssertionError(f"unexpected quest package request: {packages!r}")
        return (ExtractedAsset(PACKAGE, (SOURCE, SOURCE.with_suffix(".uexp"))),)


class _PackageMapExtractor:
    # Initialize this object
    def __init__(self, sources: dict[str, Path]) -> None:
        self.sources = {package.casefold(): path for package, path in sources.items()}

    # Extract requested game assets
    def extract_assets(self, _game_root, packages, **_kwargs):
        assets = []
        for package in packages:
            try:
                source = self.sources[package.casefold()]
            except KeyError as error:
                raise AssertionError(f"unexpected quest package request: {package!r}") from error
            assets.append(ExtractedAsset(package, (source, source.with_suffix(".uexp"))))
        return tuple(assets)


class WorldQuestArticleGrantTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        self.slot = next(
            item for item in self.catalogs.world_loot
            if item.destination_id == "dlc_e90360_gift_heal_point_up_reward"
        )

    # Check catalogued gainitem participates generic world
    def test_catalogued_gainitem_participates_in_the_generic_world_loot_plan(self) -> None:
        plan = build_world_loot_randomization_plan(
            classified_world_fixture(self.catalogs.world_loot),
            "gift-heal-point-world-loot-seed",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        matches = [
            assignment for assignment in plan.assignments
            if assignment.destination.destination_id == self.slot.destination_id
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(len(matches[0].assigned_rewards), 1)
        self.assertEqual(self.slot.export_index, 83)
        self.assertEqual(self.slot.quest_article_layout.expected_package_name_number, 2)
        self.assertEqual(self.slot.quest_article_layout.expected_asset_name_number, 2)

    # Check alternate quest reward exports share
    def test_alternate_quest_reward_exports_share_one_world_loot_assignment(self) -> None:
        expected_mirrors = {
            "quest_reward_qst_characterepisode_noah_c01030_24": "quest_reward_qst_characterepisode_noah_c01020_19",
            "quest_reward_qst_characterepisode_noah_c01040_17": "quest_reward_qst_characterepisode_noah_c01020_19",
            "quest_reward_qst_characterepisode_noah_c01030_25": "quest_reward_qst_characterepisode_noah_c01020_20",
            "quest_reward_qst_characterepisode_noah_c01030_26": "quest_reward_qst_characterepisode_noah_c01020_20",
            "quest_reward_qst_characterepisode_noah_c01040_18": "quest_reward_qst_characterepisode_noah_c01020_20",
            "quest_reward_qst_characterepisode_noah_c01030_27": "quest_reward_qst_characterepisode_noah_c01020_21",
            "quest_reward_qst_characterepisode_noah_c01030_28": "quest_reward_qst_characterepisode_noah_c01020_21",
            "quest_reward_qst_characterepisode_noah_c01040_19": "quest_reward_qst_characterepisode_noah_c01020_21",
            "quest_reward_qst_mainepisode_lyle_e00118_valley_45": "quest_reward_qst_mainepisode_lyle_e00118_cave_45",
        }
        quest_locations = {
            item.destination_id: item for item in self.catalogs.world_loot
            if item.destination_kind == "QUEST_ARTICLE_GRANT"
        }
        actual_mirrors = {
            item.destination_id: next(
                canonical.destination_id for canonical in self.catalogs.world_loot
                if canonical.package_path.casefold() == item.mirror_of_package_path.casefold()
                and canonical.export_index == item.mirror_of_export_index
            )
            for item in quest_locations.values()
            if item.mirror_of_package_path is not None
        }
        self.assertEqual(actual_mirrors, expected_mirrors)

        expa_rewards = tuple(
            item for item in quest_locations.values()
            if any(reward.article_path.endswith("/DA_Item_ExpA_17") for reward in item.rewards)
        )
        self.assertEqual(len(expa_rewards), 2)
        self.assertTrue(all(item.mirror_of_package_path is None for item in expa_rewards))

        plan = build_world_loot_randomization_plan(
            classified_world_fixture(self.catalogs.world_loot),
            "alternate-quest-reward-mirrors",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        assignments = {item.destination.destination_id: item for item in plan.assignments}
        quest_source_ids = {
            item.source_candidate.source_location.destination_id
            for item in plan.assignments
            if item.source_candidate is not None
            and item.source_candidate.source_location is not None
            and item.source_candidate.source_location.destination_kind == "QUEST_ARTICLE_GRANT"
        }
        self.assertTrue(set(expected_mirrors).isdisjoint(quest_source_ids))
        for mirror_id, canonical_id in expected_mirrors.items():
            mirror_assignment = assignments[mirror_id]
            canonical_assignment = assignments[canonical_id]
            self.assertEqual(mirror_assignment.source_candidate, canonical_assignment.source_candidate)
            self.assertEqual(mirror_assignment.assigned_rewards, canonical_assignment.assigned_rewards)

    # Check every hot spring recipe bundle
    def test_every_hot_spring_recipe_bundle_joins_world_loot(self) -> None:
        slots = tuple(
            item for item in self.catalogs.world_loot
            if (item.destination_id or "").startswith("hot_spring_")
        )
        self.assertEqual(len(slots), 10)
        self.assertEqual(sum(len(item.rewards) for item in slots), 19)
        self.assertEqual(sum(item.content_class == "DLC" for item in slots), 1)
        self.assertTrue(all(
            item.destination_kind == "QUEST_ARTICLE_GRANT"
            and item.actor_class == "QSTask_GainItem_0"
            and len(item.rewards) == len(item.quest_grant_layouts)
            and tuple(layout.reward_index for layout in item.quest_grant_layouts) == tuple(range(len(item.rewards)))
            for item in slots
        ))

        plan = build_world_loot_randomization_plan(
            classified_world_fixture(self.catalogs.world_loot),
            "hot-spring-recipes-world-loot-seed",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        assignments = {
            item.destination.destination_id: item
            for item in plan.assignments
            if (item.destination.destination_id or "").startswith("hot_spring_")
        }
        self.assertEqual(set(assignments), {item.destination_id for item in slots})
        self.assertEqual(sum(len(item.assigned_rewards) for item in assignments.values()), 19)
        self.assertTrue(all(len(item.assigned_rewards) == len(item.destination.rewards) for item in assignments.values()))
        recipe_prefix = "/Game/Blueprint/Article/Item/Important/DA_Item_Important_Recipe_".casefold()
        randomized_recipes = [
            article.casefold() for article in plan.source_article_paths
            if article.casefold().startswith(recipe_prefix)
        ]
        self.assertEqual(len(randomized_recipes), len(set(randomized_recipes)))
        repeat_recipe_fillers = [
            item for item in plan.assignments
            if item.destination.mirror_of_package_path is None
            and item.source_candidate is not None
            and item.source_candidate.candidate_id.startswith("hot-spring-repeat-recipe-filler:")
        ]
        self.assertEqual(len(repeat_recipe_fillers), 6)
        self.assertTrue(all(
            not reward.article_path.casefold().startswith(recipe_prefix)
            for item in repeat_recipe_fillers for reward in item.assigned_rewards
        ))
        spring_source_assignments = [
            item for item in plan.assignments
            if item.destination.mirror_of_package_path is None
            and item.source_candidate is not None
            and item.source_candidate.source_location is not None
            and len(item.source_candidate.source_location.rewards) == 2
            and (item.source_candidate.source_location.destination_id or "").startswith("hot_spring_")
        ]
        self.assertEqual(len(spring_source_assignments), 12)
        self.assertTrue(all(len(item.destination.rewards) == 1 for item in spring_source_assignments))

        from dataclasses import replace

        malformed = replace(slots[0], quest_article_layouts=slots[0].quest_article_layouts[:1])
        rejected = build_world_loot_randomization_plan((malformed,), "bad-hot-spring-layout")
        self.assertEqual(rejected.assignments, ())
        self.assertEqual(rejected.locked_locations[0].reason, "unvalidated structured quest Article grant destination")

    # Check mag mell hot spring enables
    @unittest.skipUnless(
        MAGMELL_SOURCE.is_file() and MAGMELL_SOURCE.with_suffix(".uexp").is_file() and MAPPINGS.is_file(),
        "local extracted MagMell quest package and CV2.usmap are not available",
    )
    def test_mag_mell_hot_spring_enables_repeat_gains_for_self_and_changed_assignments(self) -> None:
        slot = next(item for item in self.catalogs.world_loot if item.destination_id == "hot_spring_magmel")
        source_hashes = {
            suffix: MAGMELL_SOURCE.with_suffix(suffix).read_bytes()
            for suffix in (".uasset", ".uexp")
        }
        material_rewards = (
            RewardPayload("/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpWeaponA_01", 1, True),
            RewardPayload("/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpWeaponA_02", 1, True),
        )
        for label, rewards in (("self", slot.rewards), ("changed", material_rewards)):
            with self.subTest(assignment=label), tempfile.TemporaryDirectory(prefix="cv2-mag-mell-repeat-gain-") as temporary:
                candidate = WorldLootRewardCandidate(f"fixture:magmell-{label}", rewards)
                plan = WorldLootRandomizationPlan((WorldLootAssignment(slot, None, candidate),), (), (), ())
                root = Path(temporary).resolve()
                result, = apply_world_loot_direct_grants(
                    plan,
                    game_root=root / "game",
                    work_root=root,
                    output_content_root=root / "staging" / "Content",
                    mappings_path=MAPPINGS,
                    game_version="local-research-fixture",
                    data_revision="current",
                    extractor=_PackageMapExtractor({slot.package_path: MAGMELL_SOURCE}),
                    serializer=RawArticleGrantSerializer(),
                    quest_serializer=BloodCodeSerializer(),
                )

                self.assertTrue(result.changed)
                self.assertTrue(result.reopened_and_validated)
                self.assertEqual(result.assigned_article_path, rewards[0].article_path)
                self.assertEqual({path.suffix for path in result.output_files}, {".uasset", ".uexp"})
                self.assertTrue(all(path.is_file() for path in result.output_files))

        self.assertEqual(
            {suffix: MAGMELL_SOURCE.with_suffix(suffix).read_bytes() for suffix in source_hashes},
            source_hashes,
        )

    # Check paired hot springs can receive
    def test_paired_hot_springs_can_receive_singleton_weapons_from_world_loot(self) -> None:
        springs = tuple(
            item for item in self.catalogs.world_loot
            if item.destination_id in {"hot_spring_city", "hot_spring_city02"}
        )
        singletons = [
            item for item in self.catalogs.world_loot
            if item.mirror_of_package_path is None
            and len(item.rewards) == 1
            and not item.protected
            and not item.protection_reasons
            and item.destination_id is None
            and item.destination_kind == "ARTICLE_BULK"
            and item.actor_category in {"GROUND_PICKUP", "TREASURE_BOX"}
        ]
        weapon = next(item for item in singletons if "/Weapon/" in item.rewards[0].article_path)
        other_singletons = [item for item in singletons if item != weapon][:3]
        self.assertEqual(len(springs), 2)
        self.assertEqual(len(other_singletons), 3)

        plan = build_world_loot_randomization_plan(
            (*springs, weapon, *other_singletons), "hot-spring-can-receive-weapon",
        )
        assignments = [
            item for item in plan.assignments
            if item.destination.destination_id in {"hot_spring_city", "hot_spring_city02"}
        ]
        self.assertEqual(len(assignments), 2)
        self.assertTrue(all(len(item.assigned_rewards) == 2 for item in assignments))
        self.assertTrue(any(
            "/Weapon/" in reward.article_path
            for assignment in assignments for reward in assignment.assigned_rewards
        ))
        self.assertTrue(all(
            len(assignment.source_candidate.source_locations) == 2
            and all(len(source.rewards) == 1 for source in assignment.source_candidate.source_locations)
            for assignment in assignments
            if assignment.source_candidate is not None
        ))

    # Check idris conceit mag Mell quest
    def test_idris_conceit_magMell_quest_is_a_movable_world_loot_destination(self) -> None:
        slot = next(
            item for item in self.catalogs.world_loot
            if item.destination_id == "bequeathed_lou_axe_magmell_quest"
        )
        self.assertEqual(slot.package_path, "/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10365")
        self.assertEqual((slot.export_index, slot.export_name), (28, "QSTask_GainItem_1"))
        self.assertEqual(slot.rewards[0].article_path, "/Game/Blueprint/Article/Gadget/StandAlone/DA_LouAxe")
        self.assertEqual(slot.quest_article_layout.expected_articles_count, 1)
        self.assertEqual((slot.quest_article_layout.count, slot.quest_article_layout.notification), (1, True))
        self.assertTrue(slot.quest_article_layout.require_strict_export_snapshots)

        plan = build_world_loot_randomization_plan(
            self.catalogs.world_loot,
            "idri-open-world-test-1",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        assignments = [
            assignment for assignment in plan.assignments
            if assignment.destination.destination_id == slot.destination_id
        ]
        self.assertEqual(len(assignments), 1)
        self.assertNotEqual(assignments[0].assigned_rewards[0].article_path, slot.rewards[0].article_path)
        self.assertEqual(
            assignments[0].assigned_rewards,
            next(
                item for item in build_world_loot_randomization_plan(
                    self.catalogs.world_loot,
                    "idri-open-world-test-1",
                    unsafe_packages=self.catalogs.unsafe_packages,
                ).assignments
                if item.destination.destination_id == slot.destination_id
            ).assigned_rewards,
        )

    # Check five harmony moon slots have
    def test_five_harmony_moon_slots_have_exact_two_article_metadata_and_generic_pool_assignments(self) -> None:
        slots = {
            item.destination_id: item for item in self.catalogs.world_loot
            if item.destination_id in HARMONY_IDS
        }
        self.assertEqual(set(slots), set(HARMONY_IDS))
        for display_name in ("Holly", "Josee", "Lyle", "Noah", "Zenon"):
            destination_id = f"harmony_moon_{display_name.casefold()}_reward"
            if display_name == "Noah":
                destination_id = "harmony_moon_noah_reward"
            slot = slots[destination_id]
            self.assertEqual(slot.destination_kind, "QUEST_ARTICLE_GRANT")
            self.assertEqual(slot.actor_category, "DIRECT_ARTICLE_GRANT")
            self.assertEqual(slot.export_index, 39)
            self.assertEqual(slot.export_name, "QSTask_GainItem_0")
            self.assertEqual(slot.quest_article_layout.reward_index, 1)
            self.assertEqual(slot.quest_article_layout.expected_articles_count, 2)
            self.assertEqual(slot.quest_article_layout.preserved_export_index, 40)
            self.assertEqual(slot.quest_article_layout.preserved_export_name, "QSTask_GainMultiplayReward_0")
            self.assertEqual(slot.quest_article_layout.count, 1)
            self.assertIs(slot.quest_article_layout.notification, True)
            self.assertEqual(slot.rewards[0].article_path.rsplit("/", 1)[-1], f"DA_Booster_HarmonyMoon_{display_name}_1")
            self.assertEqual(slot.quest_article_layout.expected_package_name_number, 2)
            self.assertEqual(slot.quest_article_layout.expected_asset_name_number, 2)

        plan = build_world_loot_randomization_plan(
            classified_world_fixture(self.catalogs.world_loot),
            "harmony-moon-integrity-0",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        assignments = [item for item in plan.assignments if item.destination.destination_id in HARMONY_IDS]
        self.assertEqual({item.destination.destination_id for item in assignments}, set(HARMONY_IDS))
        self.assertTrue(all(len(item.assigned_rewards) == 1 for item in assignments))
        self.assertTrue(all(item.assigned_rewards[0].article_path != item.destination.rewards[0].article_path for item in assignments))
        self.assertTrue(any("/Booster/" not in item.assigned_rewards[0].article_path for item in assignments))

    # Check iris quest metadata canonical boosters
    def test_iris_quest_metadata_and_canonical_boosters_have_three_logical_identities(self) -> None:
        iris_ids = {IRIS_SAMPLE_ID, *IRIS_PAIR_IDS}
        slots = {item.destination_id: item for item in self.catalogs.world_loot if item.destination_id in iris_ids}
        self.assertEqual(set(slots), iris_ids)
        sample = slots[IRIS_SAMPLE_ID]
        self.assertEqual((sample.export_index, sample.export_name), (16, "QSTask_GainItem_1"))
        self.assertEqual(sample.quest_article_layout.expected_articles_count, 1)
        self.assertEqual(sample.rewards[0].article_path, IRIS_BOOSTER_PATHS[0])
        self.assertTrue(sample.quest_article_layout.require_strict_export_snapshots)

        reward_1 = slots[IRIS_PAIR_IDS[0]]
        reward_2 = slots[IRIS_PAIR_IDS[1]]
        self.assertEqual((reward_1.export_index, reward_1.export_name), (39, "QSTask_GainItem_2"))
        self.assertEqual((reward_2.export_index, reward_2.export_name), (40, "QSTask_GainItem_3"))
        self.assertEqual(reward_1.quest_article_layout.mirrored_exports[0].export_index, 40)
        self.assertEqual(reward_2.quest_article_layout.mirrored_exports[0].export_index, 39)
        self.assertEqual((reward_1.quest_article_layout.expected_articles_count, reward_2.quest_article_layout.expected_articles_count), (2, 2))
        self.assertEqual((reward_1.rewards[0].article_path, reward_2.rewards[0].article_path), IRIS_BOOSTER_PATHS[1:])

        reward_catalog = CanonicalArticleRewardCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        self.assertEqual(len(reward_catalog.items), 3)
        self.assertEqual({item.asset_path for item in reward_catalog.items}, set(IRIS_BOOSTER_PATHS))
        self.assertEqual({item.asset_path: item.physical_source_occurrences for item in reward_catalog.items}, {
            IRIS_BOOSTER_PATHS[0]: 1,
            IRIS_BOOSTER_PATHS[1]: 2,
            IRIS_BOOSTER_PATHS[2]: 2,
        })
        self.assertEqual(len({item.asset_path.casefold() for item in reward_catalog.items}), 3)

        plan = build_world_loot_randomization_plan(
            classified_world_fixture(self.catalogs.world_loot),
            "iris-logical-slot-count-seed",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        iris_assignments = [item for item in plan.assignments if item.destination.destination_id in iris_ids]
        self.assertEqual(len(iris_assignments), 3)
        self.assertEqual({item.destination.destination_id for item in iris_assignments}, iris_ids)
        self.assertEqual(len([item for item in iris_assignments if item.destination.destination_id in IRIS_PAIR_IDS]), 2)

    # Check unowned iris catalog boosters do
    def test_unowned_iris_catalog_boosters_do_not_create_world_loot_occurrences(self) -> None:
        reward_catalog = CanonicalArticleRewardCatalogStore(ROOT / "data" / "reward_pools" / "current").load("current")
        candidates = canonical_article_world_candidates(reward_catalog)
        ordinary_locations = tuple(
            WorldLootLocation(
                package_path=f"/Game/Map/MainWorld/MainWorld/_Generated_/IrisCandidateTest_{index}",
                source_file=f"IrisCandidateTest_{index}.umap",
                export_index=1,
                export_name=f"BP_TestPickup_{index}",
                actor_class="BP_PickupItem_C",
                actor_category="TREASURE_BOX" if index == 0 else "GROUND_PICKUP",
                save_id=f"iris-candidate-{index}",
                protected=False,
                protection_reasons=(),
                rewards=(RewardPayload(f"/Game/Blueprint/Article/Item/Support/DA_Ordinary_{index}", 1, True),),
            )
            for index in range(4)
        )
        plan = build_world_loot_randomization_plan(
            ordinary_locations, "iris-unowned-catalog-seed", catalog_candidates=candidates,
        )
        chest = next(item for item in plan.assignments if item.destination.actor_category == "TREASURE_BOX")
        self.assertEqual(plan.booster_candidate_count, 0)
        self.assertEqual(plan.booster_injected_count, 0)
        self.assertEqual(plan.candidate_stats("booster").unowned_candidates_skipped, 3)
        self.assertNotIn(chest.assigned_rewards[0].article_path, IRIS_BOOSTER_PATHS)
        self.assertEqual(
            Counter(reward.article_path for item in plan.assignments for reward in item.assigned_rewards),
            Counter(location.rewards[0].article_path for location in ordinary_locations),
        )

    # Check all five harmony moon packages
    @unittest.skipUnless(all(path.is_file() and path.with_suffix(".uexp").is_file() for path in HARMONY_SOURCES.values()) and MAPPINGS.is_file(),
                         "local extracted Harmony Moon quest packages and CV2.usmap are not available")
    def test_all_five_harmony_moon_packages_round_trip_through_the_world_loot_serializer(self) -> None:
        slots = {item.destination_id: item for item in self.catalogs.world_loot if item.destination_id in HARMONY_IDS}
        plan = build_world_loot_randomization_plan(
            classified_world_fixture(self.catalogs.world_loot),
            "harmony-moon-integrity-0",
            unsafe_packages=self.catalogs.unsafe_packages,
        )
        harmony_plan = WorldLootRandomizationPlan(
            tuple(item for item in plan.assignments if item.destination.destination_id in HARMONY_IDS), (), (), (),
        )
        self.assertEqual(len(harmony_plan.assignments), 5)
        source_hashes = {
            path: path.read_bytes()
            for source in HARMONY_SOURCES.values()
            for path in (source, source.with_suffix(".uexp"))
        }
        with tempfile.TemporaryDirectory(prefix="cv2-harmony-moon-world-loot-") as temporary:
            root = Path(temporary).resolve()
            output_root = root / "staging" / "Content"
            results = apply_world_loot_direct_grants(
                harmony_plan,
                game_root=root / "game",
                work_root=root,
                output_content_root=output_root,
                mappings_path=MAPPINGS,
                game_version="local-research-fixture",
                data_revision="current",
                extractor=_PackageMapExtractor({slots[key].package_path: value for key, value in HARMONY_SOURCES.items()}),
                serializer=RawArticleGrantSerializer(),
                quest_serializer=BloodCodeSerializer(),
            )
            self.assertEqual({result.destination_id for result in results}, set(HARMONY_IDS))
            assignments = {item.destination.destination_id: item for item in harmony_plan.assignments}
            for result in results:
                with self.subTest(destination=result.destination_id):
                    self.assertTrue(result.reopened_and_validated)
                    self.assertTrue(result.changed)
                    self.assertEqual({path.suffix for path in result.output_files}, {".uasset", ".uexp"})
                    self.assertTrue(all(path.is_file() for path in result.output_files))
                    slot = slots[result.destination_id]
                    staged_header = next(path for path in result.output_files if path.suffix == ".uasset")
                    snapshot = next(
                        item for item in BloodCodeSerializer().inspect_world_loot_quest_packages(
                            work_root=root,
                            mappings_path=MAPPINGS,
                            package_files={slot.package_path: staged_header},
                        )
                        if (item.export_index, item.bulk_index, item.reward_index) == (39, 0, 1)
                    )
                    self.assertEqual(snapshot.reward_package, assignments[result.destination_id].assigned_rewards[0].article_path)
                    self.assertEqual(snapshot.reward_asset, snapshot.reward_package.rsplit("/", 1)[-1])
                    self.assertEqual((snapshot.package_name_number, snapshot.asset_name_number), (0, 0))
                    self.assertEqual((snapshot.count, snapshot.notification, snapshot.articles_count), (1, True, 2))
        self.assertEqual({path: path.read_bytes() for path in source_hashes}, source_hashes)

    # Check josee accept canonical bow warp
    @unittest.skipUnless(HARMONY_SOURCES["harmony_moon_josee_reward"].is_file() and HARMONY_SOURCES["harmony_moon_josee_reward"].with_suffix(".uexp").is_file() and MAPPINGS.is_file(),
                         "local extracted Harmony Moon Josee package and CV2.usmap are not available")
    def test_josee_accepts_canonical_bow_warp_one_and_noise_reduce_walk_paths(self) -> None:
        slot = next(item for item in self.catalogs.world_loot if item.destination_id == "harmony_moon_josee_reward")
        source = HARMONY_SOURCES["harmony_moon_josee_reward"]
        for assigned_path in JOSEE_TEST_ARTICLES:
            with self.subTest(assigned_path=assigned_path), tempfile.TemporaryDirectory(prefix="cv2-harmony-josee-article-") as temporary:
                root = Path(temporary).resolve()
                reward = RewardPayload(assigned_path, 1, True)
                candidate = WorldLootRewardCandidate("fixture:" + assigned_path, (reward,))
                plan = WorldLootRandomizationPlan((WorldLootAssignment(slot, None, candidate),), (), (), ())
                results = apply_world_loot_direct_grants(
                    plan,
                    game_root=root / "game",
                    work_root=root,
                    output_content_root=root / "staging" / "Content",
                    mappings_path=MAPPINGS,
                    game_version="local-research-fixture",
                    data_revision="current",
                    extractor=_PackageMapExtractor({slot.package_path: source}),
                    serializer=RawArticleGrantSerializer(),
                    quest_serializer=BloodCodeSerializer(),
                )
                self.assertEqual(len(results), 1)
                self.assertTrue(results[0].reopened_and_validated)
                self.assertEqual(results[0].assigned_article_path, assigned_path)

    # Check iris c00010 sample round trip
    @unittest.skipUnless(IRIS_SOURCES[IRIS_SAMPLE_ID].is_file() and IRIS_SOURCES[IRIS_SAMPLE_ID].with_suffix(".uexp").is_file() and MAPPINGS.is_file(),
                         "local extracted Iris c00010 package and CV2.usmap are not available")
    def test_iris_c00010_sample_round_trip_changes_only_the_confirmed_article_slot(self) -> None:
        slot = next(item for item in self.catalogs.world_loot if item.destination_id == IRIS_SAMPLE_ID)
        source = IRIS_SOURCES[IRIS_SAMPLE_ID]
        hashes = {suffix: source.with_suffix(suffix).read_bytes() for suffix in (".uasset", ".uexp")}
        assigned_path = "/Game/Blueprint/Article/Gadget/StandAlone/DA_Bow"
        candidate = WorldLootRewardCandidate("fixture:iris-sample-bow", (RewardPayload(assigned_path, 1, True),))
        plan = WorldLootRandomizationPlan((WorldLootAssignment(slot, None, candidate),), (), (), ())
        with tempfile.TemporaryDirectory(prefix="cv2-iris-c00010-article-") as temporary:
            root = Path(temporary).resolve()
            results = apply_world_loot_direct_grants(
                plan,
                game_root=root / "game",
                work_root=root,
                output_content_root=root / "staging" / "Content",
                mappings_path=MAPPINGS,
                game_version="local-research-fixture",
                data_revision="current",
                extractor=_PackageMapExtractor({slot.package_path: source}),
                serializer=RawArticleGrantSerializer(),
                quest_serializer=BloodCodeSerializer(),
            )
            self.assertEqual(len(results), 1)
            result = results[0]
            self.assertTrue(result.changed)
            self.assertTrue(result.reopened_and_validated)
            self.assertEqual(result.assigned_article_path, assigned_path)
            self.assertEqual({path.suffix for path in result.output_files}, {".uasset", ".uexp"})
            header = next(path for path in result.output_files if path.suffix == ".uasset")
            snapshot, = tuple(
                item for item in BloodCodeSerializer().inspect_world_loot_quest_packages(
                    work_root=root, mappings_path=MAPPINGS, package_files={slot.package_path: header},
                )
                if (item.export_index, item.bulk_index, item.reward_index) == (16, 0, 0)
            )
            self.assertEqual(snapshot.export_name, "QSTask_GainItem_1")
            self.assertEqual(snapshot.reward_package, assigned_path)
            self.assertEqual(snapshot.reward_asset, "DA_Bow")
            self.assertEqual((snapshot.package_name_value, snapshot.asset_name_value), (assigned_path, "DA_Bow"))
            self.assertEqual((snapshot.package_name_number, snapshot.asset_name_number), (0, 0))
            self.assertEqual((snapshot.count, snapshot.notification, snapshot.articles_count), (1, True, 1))
        self.assertEqual({suffix: source.with_suffix(suffix).read_bytes() for suffix in hashes}, hashes)

    # Check iris c00070 writes two logical
    @unittest.skipUnless(IRIS_SOURCES[IRIS_PAIR_IDS[0]].is_file() and IRIS_SOURCES[IRIS_PAIR_IDS[0]].with_suffix(".uexp").is_file() and MAPPINGS.is_file(),
                         "local extracted Iris c00070 package and CV2.usmap are not available")
    def test_iris_c00070_writes_two_logical_rewards_to_both_mirrored_exports(self) -> None:
        slots = {item.destination_id: item for item in self.catalogs.world_loot if item.destination_id in IRIS_PAIR_IDS}
        package_path = slots[IRIS_PAIR_IDS[0]].package_path
        source = IRIS_SOURCES[IRIS_PAIR_IDS[0]]
        hashes = {suffix: source.with_suffix(suffix).read_bytes() for suffix in (".uasset", ".uexp")}
        logical_assignments = tuple(
            WorldLootAssignment(
                slots[destination_id], None,
                WorldLootRewardCandidate(f"fixture:{destination_id}", (RewardPayload(assigned_path, 1, True),)),
            )
            for destination_id, assigned_path in zip(IRIS_PAIR_IDS, IRIS_PAIR_ARTICLES, strict=True)
        )
        plan = WorldLootRandomizationPlan(logical_assignments, (), (), ())
        with tempfile.TemporaryDirectory(prefix="cv2-iris-c00070-article-") as temporary:
            root = Path(temporary).resolve()
            results = apply_world_loot_direct_grants(
                plan,
                game_root=root / "game",
                work_root=root,
                output_content_root=root / "staging" / "Content",
                mappings_path=MAPPINGS,
                game_version="local-research-fixture",
                data_revision="current",
                extractor=_PackageMapExtractor({package_path: source}),
                serializer=RawArticleGrantSerializer(),
                quest_serializer=BloodCodeSerializer(),
            )
            self.assertEqual(len(results), 2)
            self.assertEqual({result.destination_id for result in results}, set(IRIS_PAIR_IDS))
            self.assertTrue(all(result.changed and result.reopened_and_validated for result in results))
            self.assertEqual({path.suffix for path in results[0].output_files}, {".uasset", ".uexp"})
            self.assertEqual(set(results[0].output_files), set(results[1].output_files))
            header = next(path for path in results[0].output_files if path.suffix == ".uasset")
            snapshots = BloodCodeSerializer().inspect_world_loot_quest_packages(
                work_root=root, mappings_path=MAPPINGS, package_files={package_path: header},
            )
            expected_by_index = dict(enumerate(IRIS_PAIR_ARTICLES))
            for export_index, export_name in ((39, "QSTask_GainItem_2"), (40, "QSTask_GainItem_3")):
                for reward_index, expected_path in expected_by_index.items():
                    matches = tuple(
                        item for item in snapshots
                        if (item.export_index, item.bulk_index, item.reward_index) == (export_index, 0, reward_index)
                    )
                    self.assertEqual(len(matches), 1)
                    snapshot = matches[0]
                    self.assertEqual(snapshot.export_name, export_name)
                    self.assertEqual(snapshot.reward_package, expected_path)
                    self.assertEqual(snapshot.reward_asset, expected_path.rsplit("/", 1)[-1])
                    self.assertEqual((snapshot.package_name_value, snapshot.asset_name_value), (expected_path, expected_path.rsplit("/", 1)[-1]))
                    self.assertEqual((snapshot.package_name_number, snapshot.asset_name_number), (0, 0))
                    self.assertEqual((snapshot.count, snapshot.notification, snapshot.articles_count), (1, True, 2))
        self.assertEqual({suffix: source.with_suffix(suffix).read_bytes() for suffix in hashes}, hashes)

    # Check real gainitem round trip writes
    @unittest.skipUnless(SOURCE.is_file() and SOURCE.with_suffix(".uexp").is_file() and MAPPINGS.is_file(),
                         "local extracted DLC quest package and CV2.usmap are not available")
    def test_real_gainitem_round_trip_writes_literal_canonical_fnames_for_generic_articles(self) -> None:
        extractor = _Extractor()
        raw_serializer = RawArticleGrantSerializer()
        quest_serializer = BloodCodeSerializer()
        source_hashes = {
            suffix: (SOURCE.with_suffix(suffix).read_bytes())
            for suffix in (".uasset", ".uexp")
        }
        for assigned_path in ASSIGNED_ARTICLES:
            with self.subTest(assigned_path=assigned_path), tempfile.TemporaryDirectory(prefix="cv2-world-quest-article-") as temporary:
                root = Path(temporary).resolve()
                output_root = root / "staging" / "Content"
                if assigned_path == ASSIGNED_ARTICLES[0]:
                    existing = output_root / "Quest" / "DLC" / "QST_MainEpisode_DLC_e90360.uasset"
                    existing.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(SOURCE, existing)
                    shutil.copyfile(SOURCE.with_suffix(".uexp"), existing.with_suffix(".uexp"))
                reward = RewardPayload(assigned_path, 1, True)
                candidate = WorldLootRewardCandidate("fixture:" + assigned_path, (reward,))
                plan = WorldLootRandomizationPlan(
                    (WorldLootAssignment(self.slot, None, candidate),), (), (), (),
                )
                results = apply_world_loot_direct_grants(
                    plan,
                    game_root=root / "game",
                    work_root=root,
                    output_content_root=output_root,
                    mappings_path=MAPPINGS,
                    game_version="local-research-fixture",
                    data_revision="current",
                    extractor=extractor,
                    serializer=raw_serializer,
                    quest_serializer=quest_serializer,
                )
                self.assertEqual(len(results), 1)
                result = results[0]
                self.assertTrue(result.reopened_and_validated)
                self.assertEqual(result.assigned_article_path, assigned_path)
                self.assertEqual({path.suffix for path in result.output_files}, {".uasset", ".uexp"})
                self.assertTrue(all(path.is_file() for path in result.output_files))
        self.assertEqual({suffix: SOURCE.with_suffix(suffix).read_bytes() for suffix in source_hashes}, source_hashes)


if __name__ == "__main__":
    unittest.main()
