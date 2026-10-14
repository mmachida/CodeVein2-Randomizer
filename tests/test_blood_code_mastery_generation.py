from __future__ import annotations

import unittest
from pathlib import Path

from cv2randomizer.game.blood_code_mastery_generation import (
    BloodCodeMasteryError,
    _validate_patch_response,
    blood_code_mastery_packages,
    inventory_for_blood_code_mastery,
)
from cv2randomizer.game.content_availability import PackageInventory


ROOT = "/Game/Blueprint/Article/BloodCode/"


class _InstalledExtractor:
    # Initialize this object
    def __init__(self, inventory: PackageInventory) -> None:
        self.inventory = inventory
        self.calls = 0

    # Inspect package inventory
    def inspect_package_inventory(self, _game_root: Path) -> PackageInventory:
        self.calls += 1
        return self.inventory


class BloodCodeMasteryInventoryTests(unittest.TestCase):
    # Check selects all installed articles skips
    def test_selects_all_installed_articles_and_skips_debug_assets(self) -> None:
        inventory = PackageInventory.create((
            ROOT + "Base/DA_BloodCode_Lou_01",
            ROOT + "DLC/DA_BloodCode_Iris_01",
            ROOT + "Debug/DA_BloodCode_Test_01",
            "/Game/Blueprint/Article/Booster/DA_Booster_Test",
        ))

        self.assertEqual(
            blood_code_mastery_packages(inventory),
            (ROOT + "Base/DA_BloodCode_Lou_01", ROOT + "DLC/DA_BloodCode_Iris_01"),
        )

    # Check reuses complete ready inventory without
    def test_reuses_complete_ready_inventory_without_scanning_game(self) -> None:
        articles = (ROOT + "Base/DA_BloodCode_Lou_01", ROOT + "DLC/DA_BloodCode_Iris_01")
        cached = PackageInventory.create(articles)
        installed = _InstalledExtractor(PackageInventory.create((*articles, ROOT + "DLC/DA_BloodCode_Holly_01")))

        result = inventory_for_blood_code_mastery(
            game_root=Path("game"),
            cached_inventory=cached,
            prepared_packages=articles,
            installed_extractor=installed,  # type: ignore[arg-type]
        )

        self.assertIs(result, cached)
        self.assertEqual(installed.calls, 0)

    # Check incomplete ready cache fall back
    def test_incomplete_ready_cache_falls_back_to_installed_inventory(self) -> None:
        articles = (ROOT + "Base/DA_BloodCode_Lou_01", ROOT + "DLC/DA_BloodCode_Iris_01")
        cached = PackageInventory.create(articles[:1])
        full = PackageInventory.create((*articles, ROOT + "DLC/DA_BloodCode_Holly_01"))
        installed = _InstalledExtractor(full)

        result = inventory_for_blood_code_mastery(
            game_root=Path("game"),
            cached_inventory=cached,
            prepared_packages=articles,
            installed_extractor=installed,  # type: ignore[arg-type]
        )

        self.assertIs(result, full)
        self.assertEqual(installed.calls, 1)


class BloodCodeMasteryResponseTests(unittest.TestCase):
    # Check accept complete inventory regardless helper
    def test_accepts_complete_inventory_regardless_of_helper_sort_order(self) -> None:
        expected = (ROOT + "Base/DA_BloodCode_Lou_01", ROOT + "DLC/DA_BloodCode_Iris_01")
        response = {
            "patchedPackages": list(reversed(expected)),
            "patchedCount": 2,
            "targetProficiency": 40.0,
            "lou01OriginalProficiency": 40.0,
            "targetAppropriateLevel": 1,
            "reopenedAndValidated": True,
        }

        proficiency, lou_baseline = _validate_patch_response(response, list(expected), None, 1)
        self.assertEqual(proficiency, 40.0)
        self.assertEqual(lou_baseline, 40.0)

    # Check reject missing duplicate package identities
    def test_rejects_missing_or_duplicate_package_identities(self) -> None:
        expected = (ROOT + "Base/DA_BloodCode_Lou_01", ROOT + "DLC/DA_BloodCode_Iris_01")
        response = {
            "patchedPackages": [expected[0], expected[0]],
            "patchedCount": 2,
            "targetProficiency": 40.0,
            "lou01OriginalProficiency": 40.0,
            "targetAppropriateLevel": 1,
            "reopenedAndValidated": True,
        }

        with self.assertRaisesRegex(BloodCodeMasteryError, "package inventory mismatch"):
            _validate_patch_response(response, list(expected), None, 1)

    # Check require dynamic target match lou
    def test_requires_dynamic_target_to_match_lou_one_baseline(self) -> None:
        expected = (ROOT + "Base/DA_BloodCode_Lou_01", ROOT + "DLC/DA_BloodCode_Iris_01")
        response = {
            "patchedPackages": list(expected),
            "patchedCount": 2,
            "targetProficiency": 50.0,
            "lou01OriginalProficiency": 40.0,
            "targetAppropriateLevel": 1,
            "reopenedAndValidated": True,
        }

        with self.assertRaisesRegex(BloodCodeMasteryError, "does not match Lou 1 source"):
            _validate_patch_response(response, list(expected), None, 1)


if __name__ == "__main__":
    unittest.main()
