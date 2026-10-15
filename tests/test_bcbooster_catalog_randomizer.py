from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from cv2randomizer.game.blood_code_catalog import (
    BloodCodeCatalogError,
    BloodCodeCatalogStore,
    catalogued_base_blood_code_package_paths,
)
from cv2randomizer.game.blood_code_audit import blood_code_coverage
from cv2randomizer.game.blood_code_generation import required_blood_code_package_paths
from cv2randomizer.game.booster_catalog import BoosterCatalogStore
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.randomizer.blood_codes import BloodCodePlanError, build_blood_code_plan
from cv2randomizer.randomizer.boosters import build_booster_plan


ROOT = Path(__file__).resolve().parents[1]


class BloodCodeAndBoosterCatalogTests(unittest.TestCase):
    # Check archive line endings validate
    def test_blood_code_store_accepts_lf_catalog_file(self) -> None:
        source_dir = ROOT / "data" / "blood_codes" / "current"
        with tempfile.TemporaryDirectory() as temporary:
            target_dir = Path(temporary)
            shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
            path = target_dir / "blood_code_grants.json"
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
            loaded = BloodCodeCatalogStore(target_dir).load("current")
            self.assertEqual(len(loaded.slots), 79)

    # Check initial inventory lou reference is
    def test_initial_inventory_lou_reference_is_not_a_blood_code_reward_slot(self) -> None:
        catalog = BloodCodeCatalogStore(ROOT / "data" / "blood_codes" / "current").load("current")
        self.assertFalse(any(slot.location_kind == "initial_inventory_reference" for slot in catalog.slots))
        self.assertFalse(any(slot.original_blood_code_asset == "DA_BloodCode_Lou_01" for slot in catalog.slots))
        for mix in (False, True):
            for seed in range(10):
                plan = build_blood_code_plan(catalog, seed, mix_postgame_dlc_with_base=mix)
                self.assertEqual(len(plan.assignments), 79)
                self.assertFalse(any(item.source.original_blood_code_asset == "DA_BloodCode_Lou_01" for item in plan.assignments))
        disabled = build_blood_code_plan(catalog, "blood-codes-disabled", enabled=False)
        self.assertEqual(disabled.assignments, ())

    # Check catalogs have confirmed shapes
    def test_catalogs_have_the_confirmed_shapes(self) -> None:
        blood_codes = BloodCodeCatalogStore(ROOT / "data" / "blood_codes" / "current").load("current")
        boosters = BoosterCatalogStore(ROOT / "data" / "boosters" / "current").load("current")
        self.assertEqual(Counter(slot.content_class for slot in blood_codes.slots), {"BASE": 56, "DLC": 10, "AFTER_ENDING": 13})
        self.assertEqual(len(blood_codes.slots), 79)
        buddy_slots = [slot for slot in blood_codes.slots if slot.location_kind == "buddy_settings_reference"]
        self.assertEqual(len(buddy_slots), 14)
        self.assertEqual({slot.original_blood_code_asset for slot in buddy_slots}, {
            "DA_BloodCode_Craig_01", "DA_BloodCode_Holly_01", "DA_BloodCode_Iris_01",
            "DA_BloodCode_Jadwiga_01", "DA_BloodCode_Josee_01", "DA_BloodCode_Lavinia_01",
            "DA_BloodCode_Lise_01", "DA_BloodCode_Liv_02", "DA_BloodCode_Lycoris_01",
            "DA_BloodCode_Lyle_01", "DA_BloodCode_Valentin_01", "DA_BloodCode_Zenon_01",
            "DA_BloodCode_Noah_01", "DA_BloodCode_GuardianHeart",
        })
        self.assertTrue(all(slot.object_reference.property_schema_index == 6 for slot in buddy_slots))
        self.assertTrue(all(slot.expected_asset_name_number == 0 for slot in buddy_slots))
        lavinia = next(slot for slot in buddy_slots if slot.original_blood_code_asset == "DA_BloodCode_Lavinia_01")
        self.assertEqual(lavinia.slot_id, "buddy:/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/Lavinia/DA_BuddySettings_Lavinia::export=1::BloodCode")
        self.assertEqual(len(lavinia.physical_reference_targets), 2)
        self.assertEqual(
            {target.virtual_package: (target.export_name, target.expected_super_class)
             for target in lavinia.physical_reference_targets},
            {
                "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/CharacterEpisode/CharacterEpisodeLavinia/Present_c08010/BP_SmallScenario_Lavinia_Present_c08010_B":
                    ("Default__BP_SmallScenario_Lavinia_Present_c08010_B_C", "BP_SmallScenarioGainBoosterBase_C"),
                "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/CharacterEpisode/CharacterEpisodeLavinia/Present_c08030/BP_SmallScenario_Lavinia_Present_c08030_B":
                    ("Default__BP_SmallScenario_Lavinia_Present_c08030_B_C", "BP_SmallScenarioBase_C"),
            },
        )
        self.assertEqual(len(lavinia.physical_object_reference_targets), 1)
        self.assertNotIn("/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01", {
            slot.original_blood_code_package for slot in blood_codes.slots
        })
        self.assertEqual(len({slot.original_blood_code_package for slot in blood_codes.slots}), 79)
        lou_disp = next(slot for slot in blood_codes.slots if slot.original_blood_code_asset == "DA_BloodCode_LouDisp_02")
        self.assertEqual(lou_disp.slot_id, "/Game/Quest/DLC/QST_MainEpisode_DLC_GainBC_LouDisp::export=15::bulk=0::article=0")
        self.assertEqual(
            {target.virtual_package for target in lou_disp.physical_reference_targets},
            {
                "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/BloodCode/Past_a20180/BP_SmallScenario_Lou_Disp_Past_a20180_01",
                "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/BloodCode/Past_a20180/BP_SmallScenario_Lou_Disp_Past_a20180_01_Yadorigi",
            },
        )
        self.assertEqual(sum(slot.original_blood_code_asset == "DA_BloodCode_LouDisp_02" for slot in blood_codes.slots), 1)
        catalogued_base = set(catalogued_base_blood_code_package_paths(blood_codes))
        self.assertTrue(all(
            package in catalogued_base
            for slot in blood_codes.slots if slot.content_class == "BASE"
            for package in (*slot.physical_source_packages, slot.original_blood_code_package)
        ))
        self.assertFalse(any(
            slot.content_class != "BASE" and slot.original_blood_code_package in catalogued_base
            for slot in blood_codes.slots
        ))
        self.assertEqual(
            [slot for slot in blood_codes.slots if slot.location_kind == "proficiency_reference"],
            [],
        )

        early_slots = {
            slot.original_blood_code_asset: slot
            for slot in buddy_slots
            if slot.original_blood_code_asset in {"DA_BloodCode_Noah_01", "DA_BloodCode_GuardianHeart"}
        }
        self.assertEqual(set(early_slots), {"DA_BloodCode_Noah_01", "DA_BloodCode_GuardianHeart"})
        self.assertEqual(early_slots["DA_BloodCode_Noah_01"].display_name, "Noah_01 quest reward")
        self.assertEqual(early_slots["DA_BloodCode_GuardianHeart"].display_name, "Guardian's Heart quest reward")
        self.assertEqual(
            {slot.virtual_package for slot in early_slots.values()},
            {
                "/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/noah/DA_BuddySettings_Noah",
                "/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/GuardianHert/DA_BuddySettings_GuardianHert",
            },
        )
        self.assertTrue(all(len(slot.physical_reference_targets) == 2 for slot in early_slots.values()))
        self.assertEqual(
            {target.virtual_package for slot in early_slots.values() for target in slot.physical_reference_targets},
            {
                "/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10210",
                "/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10250",
            },
        )
        self.assertEqual(
            {asset: {target.reference_index for target in slot.physical_reference_targets}
             for asset, slot in early_slots.items()},
            {"DA_BloodCode_Noah_01": {1}, "DA_BloodCode_GuardianHeart": {2}},
        )
        self.assertTrue(all(
            target.expected_array_count == 3
            for slot in early_slots.values() for target in slot.physical_reference_targets
        ))
        self.assertEqual(Counter(slot.content_class for slot in boosters.slots), {"BASE": 57, "DLC": 18, "AFTER_ENDING": 27})

    # Check initial lou article is protected
    def test_initial_lou_article_is_protected_until_true_new_game_source_is_proven(self) -> None:
        catalog = BloodCodeCatalogStore(ROOT / "data" / "blood_codes" / "current").load("current")
        inventory = PackageInventory.create(
            (
                *(package
                  for slot in catalog.slots
                  for package in (*slot.physical_source_packages, slot.original_blood_code_package)),
                "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01",
            )
        )

        coverage = blood_code_coverage(catalog, inventory)
        lou = next(row for row in coverage["articles"] if row["asset"] == "DA_BloodCode_Lou_01")

        self.assertEqual(lou["status"], "PROTECTED_STARTING_UNRESOLVED")
        self.assertNotIn("initial_inventory_reference", coverage["mechanism_counts"])
        self.assertEqual(len(coverage["locations"]), 79)

    # Check blood code plan is deterministic
    def test_blood_code_plan_is_deterministic_and_preserves_each_class_without_mix(self) -> None:
        catalog = BloodCodeCatalogStore(ROOT / "data" / "blood_codes" / "current").load("current")
        first = build_blood_code_plan(catalog, "bc-seed")
        second = build_blood_code_plan(catalog, "bc-seed")
        self.assertEqual(first, second)
        self.assertEqual(len(first.assignments), 79)
        early_assignments = {
            item.destination.original_blood_code_asset: item
            for item in first.assignments
            if item.destination.original_blood_code_asset in {"DA_BloodCode_Noah_01", "DA_BloodCode_GuardianHeart"}
        }
        self.assertEqual(set(early_assignments), {"DA_BloodCode_Noah_01", "DA_BloodCode_GuardianHeart"})
        for content_class in ("BASE", "DLC", "AFTER_ENDING"):
            assignments = [item for item in first.assignments if item.destination.content_class == content_class]
            self.assertTrue(all(item.source.content_class == content_class for item in assignments))
            self.assertEqual(
                Counter(item.destination.original_blood_code_package for item in assignments),
                Counter(item.source.original_blood_code_package for item in assignments),
            )

    # Check blood code mix use one
    def test_blood_code_mix_uses_one_multiset_preserving_pool(self) -> None:
        catalog = BloodCodeCatalogStore(ROOT / "data" / "blood_codes" / "current").load("current")
        plan = build_blood_code_plan(catalog, "bc-mix", mix_postgame_dlc_with_base=True)
        self.assertEqual(len(plan.assignments), 79)
        self.assertEqual({pool.content_class for pool in plan.pool_stats}, {"ALL"})
        self.assertEqual(
            Counter(item.destination.original_blood_code_package for item in plan.assignments),
            Counter(item.source.original_blood_code_package for item in plan.assignments),
        )

    # Check blood code plan reject duplicate
    def test_blood_code_plan_rejects_duplicate_source_identity(self) -> None:
        catalog = BloodCodeCatalogStore(ROOT / "data" / "blood_codes" / "current").load("current")
        first, second = catalog.slots[:2]
        duplicate = replace(
            second,
            original_blood_code_package=first.original_blood_code_package,
            original_blood_code_asset=first.original_blood_code_asset,
        )
        invalid = replace(catalog, slots=(first, duplicate, *catalog.slots[2:]))
        with self.assertRaisesRegex(BloodCodePlanError, "duplicate logical reward identities"):
            build_blood_code_plan(invalid, "duplicate-blood-code")

    # Check blood code catalog reject duplicate
    def test_blood_code_catalog_rejects_duplicate_article_identity(self) -> None:
        source_dir = ROOT / "data" / "blood_codes" / "current"
        with tempfile.TemporaryDirectory() as temporary:
            target_dir = Path(temporary)
            shutil.copy2(source_dir / "manifest.json", target_dir / "manifest.json")
            document = json.loads((source_dir / "blood_code_grants.json").read_text(encoding="utf-8"))
            document["slots"][1]["original_blood_code_package"] = document["slots"][0]["original_blood_code_package"]
            document["slots"][1]["original_blood_code_asset"] = document["slots"][0]["original_blood_code_asset"]
            payload = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            (target_dir / "blood_code_grants.json").write_bytes(payload)
            manifest_path = target_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["blood_code_grants.json"]["sha256"] = hashlib.sha256(payload).hexdigest()
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(BloodCodeCatalogError, "multiple logical reward slots"):
                BloodCodeCatalogStore(target_dir).load("current")

    # Check blood code plan accept available
    def test_blood_code_plan_accepts_only_available_grants(self) -> None:
        catalog = BloodCodeCatalogStore(ROOT / "data" / "blood_codes" / "current").load("current")
        available = tuple(slot for slot in catalog.slots if slot.content_class != "DLC")
        active_catalog = type(catalog)(catalog.revision, available, catalog.source_package_sha256)
        plan = build_blood_code_plan(active_catalog, "bc-optional-absent", mix_postgame_dlc_with_base=True)
        self.assertEqual(len(plan.assignments), len(available))
        self.assertTrue(all(item.destination.content_class != "DLC" for item in plan.assignments))
        self.assertEqual({item.destination.slot_id for item in plan.assignments}, {slot.slot_id for slot in available})

    # Check blood code required packages follow
    def test_blood_code_required_packages_follow_available_catalog(self) -> None:
        catalog = BloodCodeCatalogStore(ROOT / "data" / "blood_codes" / "current").load("current")
        all_packages = required_blood_code_package_paths(catalog)
        by_package = catalog.by_package()
        dlc_only = {
            package for package, slots in by_package.items()
            if all(slot.content_class == "DLC" for slot in slots)
        }
        available_catalog = type(catalog)(
            catalog.revision,
            tuple(slot for slot in catalog.slots if slot.virtual_package not in dlc_only),
            {package: hashes for package, hashes in catalog.source_package_sha256.items() if package not in dlc_only},
        )
        available_packages = required_blood_code_package_paths(available_catalog)
        self.assertEqual((len(all_packages), len(dlc_only), len(available_packages)), (108, 12, 96))
        self.assertFalse(dlc_only & set(available_packages))

    # Check booster plan keep reward bundle
    def test_booster_plan_keeps_reward_bundle_and_pool_boundaries(self) -> None:
        catalog = BoosterCatalogStore(ROOT / "data" / "boosters" / "current").load("current")
        first = build_booster_plan(catalog, "booster-seed")
        self.assertEqual(first, build_booster_plan(catalog, "booster-seed"))
        for item in first.assignments:
            self.assertEqual(item.destination.content_class, item.source.content_class)
            self.assertEqual(item.assigned_reward, item.source.original_reward)
        self.assertEqual(
            Counter(item.destination.original_reward.reward_id for item in first.assignments),
            Counter(item.assigned_reward.reward_id for item in first.assignments),
        )

    # Check booster mix use one 102
    def test_booster_mix_uses_one_102_row_pool(self) -> None:
        catalog = BoosterCatalogStore(ROOT / "data" / "boosters" / "current").load("current")
        plan = build_booster_plan(catalog, "booster-mix", mix_postgame_dlc_with_base=True)
        self.assertEqual(len(plan.assignments), 102)
        self.assertEqual({pool.content_class for pool in plan.pool_stats}, {"ALL"})
        self.assertEqual(
            Counter(item.destination.original_reward.reward_id for item in plan.assignments),
            Counter(item.assigned_reward.reward_id for item in plan.assignments),
        )

    # Check booster plan accept rows available
    def test_booster_plan_accepts_only_rows_with_available_rewards(self) -> None:
        catalog = BoosterCatalogStore(ROOT / "data" / "boosters" / "current").load("current")
        available = tuple(slot for slot in catalog.slots if slot.content_class != "DLC")
        active_catalog = type(catalog)(catalog.revision, available, catalog.source_package_sha256)
        plan = build_booster_plan(active_catalog, "booster-optional-absent", mix_postgame_dlc_with_base=True)
        self.assertEqual(len(plan.assignments), len(available))
        self.assertEqual({item.destination.row_name for item in plan.assignments}, {slot.row_name for slot in available})
        self.assertTrue(all(item.destination.content_class != "DLC" for item in plan.assignments))


if __name__ == "__main__":
    unittest.main()
