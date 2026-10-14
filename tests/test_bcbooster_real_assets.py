from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.blood_code_catalog import BloodCodeCatalogStore
from cv2randomizer.game.blood_code_generation import BloodCodeSerializer, apply_blood_code_plan, prepare_blood_code_plan
from cv2randomizer.game.booster_catalog import BOOSTER_TABLE_PACKAGE, BoosterCatalogStore
from cv2randomizer.game.booster_generation import BoosterSerializer, apply_booster_plan, prepare_booster_plan
from cv2randomizer.packaging.retoc import RetocPackager


BLOOD_CONTENT = os.environ.get("CV2_BLOOD_CODE_SOURCE_CONTENT")
BOOSTER_CONTENT = os.environ.get("CV2_BOOSTER_SOURCE_CONTENT")
MAPPINGS_FILE = os.environ.get("CV2_USMAP_PATH")


class _ExtractedAssetSource:
    # Initialize this object
    def __init__(self, blood_root: Path, booster_root: Path) -> None:
        self.blood_root = blood_root
        self.booster_root = booster_root

    # Extract requested game assets
    def extract_assets(self, _game_root, package_paths, **_kwargs):
        assets = []
        for virtual in package_paths:
            content_root = self.booster_root if virtual == BOOSTER_TABLE_PACKAGE else self.blood_root
            relative = Path(*virtual.split("/")[2:])
            header = content_root / relative.with_suffix(".uasset")
            files = tuple(path for path in (header, header.with_suffix(".uexp"), header.with_suffix(".ubulk"), header.with_suffix(".uptnl")) if path.is_file())
            if not files:
                raise FileNotFoundError(header)
            assets.append(ExtractedAsset(virtual, files))
        return tuple(assets)


@unittest.skipUnless(
    BLOOD_CONTENT and BOOSTER_CONTENT and MAPPINGS_FILE,
    "set CV2_BLOOD_CODE_SOURCE_CONTENT, CV2_BOOSTER_SOURCE_CONTENT, and CV2_USMAP_PATH for real-asset round-trip and Retoc validation",
)
class BloodCodeBoosterRealAssetIntegrationTests(unittest.TestCase):
    # Check extract plan patch reopen package
    def test_extract_plan_patch_reopen_and_package(self) -> None:
        blood_content = Path(BLOOD_CONTENT).expanduser().resolve(strict=True)
        booster_content = Path(BOOSTER_CONTENT).expanduser().resolve(strict=True)
        mappings = Path(MAPPINGS_FILE).expanduser().resolve(strict=True)
        source_headers = [
            blood_content / Path(*package.split("/")[2:]).with_suffix(".uasset")
            for package in BloodCodeCatalogStore().load("current").source_packages
        ]
        source_headers.append(booster_content / Path(*BOOSTER_TABLE_PACKAGE.split("/")[2:]).with_suffix(".uasset"))
        source_files = {
            path.with_suffix(suffix)
            for header in source_headers
            for suffix in (".uasset", ".uexp", ".ubulk", ".uptnl")
            if (path := header.with_suffix(suffix)).is_file()
        }
        original_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
        with tempfile.TemporaryDirectory(prefix="cv2-bc-booster-integration-") as temporary:
            root = Path(temporary).resolve()
            extractor = _ExtractedAssetSource(blood_content, booster_content)
            blood_catalog = BloodCodeCatalogStore().load("current")
            booster_catalog = BoosterCatalogStore().load("current")
            blood_preparation = prepare_blood_code_plan(
                blood_catalog, "real-bc-booster-integration", game_root=root / "unused-game", work_root=root,
                mappings_path=mappings, game_version="test-build", data_revision="current", extractor=extractor,
                serializer=BloodCodeSerializer(), mix_postgame_dlc_with_base=False,
            )
            booster_preparation = prepare_booster_plan(
                booster_catalog, "real-bc-booster-integration", game_root=root / "unused-game", work_root=root,
                mappings_path=mappings, game_version="test-build", data_revision="current", extractor=extractor,
                serializer=BoosterSerializer(), mix_postgame_dlc_with_base=False,
            )
            self.assertEqual(len(blood_preparation.plan.assignments), len(blood_catalog.slots))
            self.assertEqual(len(booster_preparation.plan.assignments), 102)

            swap_output = root / "manual-swap" / "Content" / "Cinematics" / "CinematicScene" / "Scenario" / "SmallScenario" / "Blueprint" / "Tool" / "DT_GainBoosterSmallScenario.uasset"
            swap_output.parent.mkdir(parents=True, exist_ok=True)
            by_row = {slot.row_name: slot for slot in booster_catalog.slots}
            row_a, row_b = by_row["Booster_MND_1"], by_row["Booster_WIL_1"]
            swap_result = BoosterSerializer().patch_table(
                work_root=root,
                mappings_path=mappings,
                input_file=booster_preparation.source_file,
                output_file=swap_output,
                assignments=(
                    _swap_assignment(row_a, row_b),
                    _swap_assignment(row_b, row_a),
                ),
            )
            swapped = {row.row_name: row for row in swap_result["parsedRows"]}
            self.assertEqual(
                swapped["Booster_MND_1"].evaluation_formula,
                "BloodCodeMastery.Lou >= Rank01_Master && Booster_WIL_1 == false",
            )
            self.assertEqual(swapped["Booster_MND_1"].booster_package, row_b.booster_article_path)
            self.assertEqual(swapped["Booster_MND_1"].blood_code_package, row_a.blood_code_article_path)
            self.assertEqual((swapped["Booster_MND_1"].count, swapped["Booster_MND_1"].notification), (row_a.count, row_a.b_notification))
            self.assertEqual(
                swapped["Booster_WIL_1"].evaluation_formula,
                "BloodCodeMastery.Lavinia >= Rank01_Master && Booster_MND_1 == false",
            )
            self.assertEqual(swapped["Booster_WIL_1"].booster_package, row_a.booster_article_path)
            self.assertEqual(swapped["Booster_WIL_1"].blood_code_package, row_b.blood_code_article_path)
            unchanged_names = set(swapped) - {"Booster_MND_1", "Booster_WIL_1"}
            original_rows = {row.row_name: row for row in booster_preparation.original_rows}
            self.assertTrue(all(swapped[name] == original_rows[name] for name in unchanged_names))

            output_content = root / "staging" / "Content"
            blood_result = apply_blood_code_plan(
                blood_preparation, work_root=root, output_content_root=output_content,
                mappings_path=mappings, serializer=BloodCodeSerializer(),
            )
            booster_result = apply_booster_plan(
                booster_preparation, work_root=root, output_content_root=output_content,
                mappings_path=mappings, serializer=BoosterSerializer(),
            )
            self.assertTrue(blood_result.reopened)
            self.assertTrue(booster_result.reopened)
            self.assertTrue(blood_result.output_files)
            self.assertEqual(len(booster_result.output_files), 2)
            package = RetocPackager().package(
                output_content, work_root=root, output_directory=root / "packaged", mod_name="CV2BloodCodesBoostersFixture",
            )
            self.assertEqual({path.suffix for path in package.files}, {".pak", ".utoc", ".ucas"})
            self.assertTrue(any("/Quest/" in path for path in package.package_paths))
            self.assertIn(
                "../../../CodeVein2/Content/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/DT_GainBoosterSmallScenario.uasset",
                package.package_paths,
            )

        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}, original_hashes)


# Read swap assignment
def _swap_assignment(destination, source) -> dict[str, object]:
    return {
        "rowIndex": destination.row_index,
        "rowName": destination.row_name,
        "expectedFormula": destination.evaluation_formula_original,
        "masteryOwner": destination.mastery_owner,
        "masteryRank": destination.mastery_rank,
        "expectedBloodCodePackage": destination.blood_code_article_path,
        "expectedBloodCodeAsset": destination.blood_code_article_asset,
        "expectedStoryFlag": destination.booster_story_flag,
        "expectedPackage": destination.booster_article_path,
        "expectedAsset": destination.booster_article_asset,
        "expectedCount": destination.count,
        "expectedNotification": destination.b_notification,
        "assignedStoryFlag": source.booster_story_flag,
        "assignedPackage": source.booster_article_path,
        "assignedAsset": source.booster_article_asset,
    }


if __name__ == "__main__":
    unittest.main()
