from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cv2randomizer.game.article_reward_catalog import CanonicalArticleRewardCatalogStore
from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.booster_catalog import BoosterCatalogStore
from cv2randomizer.game.booster_reward_pool import build_extractor_booster_pool
from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.extractor_catalog import ExtractorCatalogStore
from cv2randomizer.game.extractor_generation import (
    ExtractorSerializer,
    apply_extractor_plan,
    prepare_extractor_plan,
)
from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.core.workspace import WorkspaceManager
from cv2randomizer.game.versioning import VersionDetection
from cv2randomizer.generation.pipeline import GenerationEngine, GenerationRequest
from cv2randomizer.packaging.retoc import PackageResult
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "workspace" / "research_assets"
SOURCE_CONTENT = RESEARCH / "extractor_source" / "CodeVein2" / "Content"
MAPPINGS = RESEARCH / "CV2.usmap"
BASE_PACKAGE = "/Game/Blueprint/GameSystem/ItemSynthetic/Booster/DT_BoosterRecipe"
DLC_PACKAGE = "/Game/Blueprint/GameSystem/ItemSynthetic/Booster/DT_BoosterRecipe_DLC"
CORE_PACKAGE = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"
HAS_LOCAL_ASSETS = (
    MAPPINGS.is_file()
    and all((SOURCE_CONTENT.joinpath(*package.split("/")[2:]).with_suffix(ext)).is_file()
            for package in (BASE_PACKAGE, DLC_PACKAGE) for ext in (".uasset", ".uexp"))
)


class _CachedExtractor:
    # Initialize this object
    def __init__(self, package_inventory: tuple[str, ...] = ()) -> None:
        self.requested: tuple[str, ...] = ()
        self.package_inventory = PackageInventory.create(package_inventory)

    # Inspect package inventory
    def inspect_package_inventory(self, _game_root):
        return self.package_inventory

    # Extract requested game assets
    def extract_assets(self, game_root, virtual_paths, **_kwargs):
        self.requested = tuple(virtual_paths)
        return tuple(
            ExtractedAsset(
                package,
                tuple(SOURCE_CONTENT.joinpath(*package.split("/")[2:]).with_suffix(ext)
                      for ext in (".uasset", ".uexp")),
            )
            for package in self.requested
        )


class _LocalVersionDetector:
    # Detect the installed game version
    def detect(self, game_root: Path) -> VersionDetection:
        return VersionDetection(True, "local-unverified-extractor-research", "current", game_root / "CodeVein2.exe", "local test")


class _FakePackager:
    # Initialize this object
    def __init__(self) -> None:
        self.package_paths: tuple[str, ...] = ()

    # Resolve the package path
    def package(self, content_root: Path, *, work_root: Path, output_directory: Path,
                input_files: tuple[Path, ...] | None = None, game_root: Path | None = None) -> PackageResult:
        output_directory.mkdir(parents=True, exist_ok=True)
        files = tuple(output_directory / f"CV2Randomizer_Extractor{suffix}" for suffix in (".pak", ".utoc", ".ucas"))
        for path in files:
            path.write_bytes(b"fixture verified container")
        self.package_paths = tuple(
            "../../../CodeVein2/Content/" + path.relative_to(content_root).as_posix()
            for path in sorted(content_root.rglob("*.uasset"))
        )
        return PackageResult("CV2Randomizer_Extractor", files, self.package_paths)


@unittest.skipUnless(HAS_LOCAL_ASSETS, "local Extractor research assets and CV2.usmap are required for real DataTable round trips")
class ExtractorGenerationRealAssetTests(unittest.TestCase):
    # Read round trip
    def _round_trip(
        self, *, available_tables: tuple[str, ...], randomize: bool = True,
        one_material_each: bool = False,
    ) -> None:
        full_catalog = ExtractorCatalogStore().load("current")
        extractor_catalog = replace(
            full_catalog,
            tables=tuple(table for table in full_catalog.tables if table.virtual_package in available_tables),
        )
        empty_world_plan = build_world_loot_randomization_plan((), "extractor-round-trip")
        candidates = build_extractor_booster_pool(
            extractor=extractor_catalog,
            world_plan=empty_world_plan,
        ) if randomize else ()
        fake_extractor = _CachedExtractor()
        serializer = ExtractorSerializer()
        with tempfile.TemporaryDirectory(prefix="cv2-extractor-roundtrip-") as temporary:
            root = Path(temporary)
            preparation = prepare_extractor_plan(
                extractor_catalog,
                candidates,
                f"extractor-roundtrip-available-{len(available_tables)}-tables",
                game_root=root / "unused-game",
                work_root=root,
                mappings_path=MAPPINGS,
                game_version="local-unverified-extractor-research",
                data_revision="current",
                extractor=fake_extractor,
                serializer=serializer,
                mix_postgame_dlc_with_base=False,
                randomize_recipes=randomize,
            )
            output_root = root / "staging" / "Content"
            result = apply_extractor_plan(
                preparation,
                work_root=root,
                output_content_root=output_root,
                mappings_path=MAPPINGS,
                serializer=serializer,
                one_material_each=one_material_each,
            )
            assigned_packages = {
                item.destination.virtual_package
                for item in (preparation.plan.assignments if preparation.plan else ())
            }
            source_by_package = {table.virtual_package: table for table in preparation.source_tables}
            cost_test_packages = {
                package for package, table in source_by_package.items()
                if one_material_each and any(
                    quantity != 1 for row in table.rows for quantity in row.raw_material_quantities
                )
            }
            expected_packages = tuple(
                package for package in (BASE_PACKAGE, DLC_PACKAGE)
                if package in available_tables and (package in assigned_packages or package in cost_test_packages)
            )
            self.assertEqual(fake_extractor.requested, expected_packages)
            self.assertEqual(result.table_packages, expected_packages)
            self.assertTrue(result.reopened)
            self.assertEqual(result.one_material_each, one_material_each)
            if randomize:
                self.assertIsNotNone(preparation.plan)
            else:
                self.assertIsNone(preparation.plan)
            assignments = {
                (item.destination.virtual_package, item.destination.row_name): item.assigned_reward.article_path
                for item in (preparation.plan.assignments if preparation.plan else ())
            }
            reopened = serializer.inspect_tables(
                work_root=root, mappings_path=MAPPINGS,
                package_files={package: output_root.joinpath(*package.split('/')[2:]).with_suffix('.uasset')
                               for package in result.table_packages},
            )
            changed = 0
            reopened_by_package = {table.virtual_package: table for table in reopened}
            for package in result.table_packages:
                original = source_by_package[package]
                patched = reopened_by_package[package]
                for source_row, row in zip(original.rows, patched.rows, strict=True):
                    self.assertEqual(row.article_path, assignments.get(
                        (original.virtual_package, source_row.row_name), source_row.article_path,
                    ))
                    self.assertEqual(row.structural_fingerprint_sha256, source_row.structural_fingerprint_sha256)
                    self.assertEqual(row.haze_cost, source_row.haze_cost)
                    expected_quantities = (
                        (1,) * len(source_row.raw_material_quantities)
                        if one_material_each else source_row.raw_material_quantities
                    )
                    self.assertEqual(row.raw_material_quantities, expected_quantities)
                    changed += row.snapshot_sha256 != source_row.snapshot_sha256
            self.assertEqual(result.patched_rows, changed)
            for table in extractor_catalog.tables:
                for suffix, expected_hash in table.source_package_sha256.items():
                    source = SOURCE_CONTENT.joinpath(*table.virtual_package.split('/')[2:]).with_suffix(suffix)
                    self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), expected_hash)
            self.assertEqual(set(result.table_packages), set(expected_packages))
            output_paths = {path.relative_to(output_root).as_posix() for path in result.output_files}
            for package in result.table_packages:
                relative = Path(*package.split("/")[2:])
                self.assertIn(relative.with_suffix(".uasset").as_posix(), output_paths)
                self.assertIn(relative.with_suffix(".uexp").as_posix(), output_paths)
            if DLC_PACKAGE not in available_tables:
                self.assertFalse(any("DT_BoosterRecipe_DLC" in path for path in output_paths))

    # Check missing optional table is not
    def test_missing_optional_table_is_not_extracted_or_staged(self) -> None:
        self._round_trip(available_tables=(BASE_PACKAGE,))

    # Check both available tables are reopened
    def test_both_available_tables_are_reopened(self) -> None:
        self._round_trip(available_tables=(BASE_PACKAGE, DLC_PACKAGE))

    # Check cheaper extractor sets each ingredient
    def test_cheaper_extractor_sets_each_ingredient_to_one_and_keeps_haze(self) -> None:
        self._round_trip(
            available_tables=(BASE_PACKAGE, DLC_PACKAGE),
            randomize=False,
            one_material_each=True,
        )

    # Check generate use recipe tables present
    def test_generate_uses_only_recipe_tables_present_in_package_inventory(self) -> None:
        self._generate(randomize=True)

    # Generate randomized package output
    def _generate(self, *, randomize: bool) -> None:
        booster_catalog = BoosterCatalogStore().load("current")
        extractor_catalog = ExtractorCatalogStore().load("current")
        article_catalog = CanonicalArticleRewardCatalogStore().load("current")
        all_reward_packages = {
            slot.booster_article_path for slot in booster_catalog.slots
        } | {
            row.original_article_path for table in extractor_catalog.tables for row in table.rows
        } | {
            item.asset_path for item in article_catalog.items
        }
        extractor = _CachedExtractor((CORE_PACKAGE, BASE_PACKAGE, *sorted(all_reward_packages)))
        packager = _FakePackager()
        with tempfile.TemporaryDirectory(prefix="cv2-extractor-pipeline-") as temporary:
            root = Path(temporary)
            engine = GenerationEngine(
                version_detector=_LocalVersionDetector(),
                catalog_store=CatalogStore(),
                workspace_manager=WorkspaceManager(root / "workspaces"),
                extractor=extractor,
                extractor_serializer=ExtractorSerializer(),
                packager=packager,
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_materials=False,
                randomize_shops=False,
                randomize_extractor_recipes=randomize,
                install_after_generation=False,
                generate_spoiler_log=True,
            )
            result = engine.generate(GenerationRequest(root / "game", MAPPINGS, "extractor-full-pipeline", options))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(result.extractor_result.reopened)
            self.assertEqual(result.extractor_result.table_packages, (BASE_PACKAGE,))
            self.assertEqual(extractor.requested, (BASE_PACKAGE,))
            self.assertEqual(manifest["counts"]["extractor"]["assignments"], 36 if randomize else 0)
            self.assertEqual(manifest["counts"]["extractor"]["base_destinations"], 36 if randomize else 0)
            self.assertEqual(manifest["counts"]["extractor"]["dlc_destinations"], 0)
            if not randomize:
                self.assertIsNone(result.extractor_plan)
                self.assertEqual(manifest["counts"]["planning_priority"]["extractor_boosters_consumed"], 0)
            self.assertEqual(len(result.package_result.files), 3)
            self.assertEqual({path.suffix for path in result.package_result.files}, {".pak", ".utoc", ".ucas"})
            self.assertTrue(any("DT_BoosterRecipe.uasset" in item for item in packager.package_paths))
            self.assertFalse(any("DT_BoosterRecipe_DLC" in item for item in packager.package_paths))


if __name__ == "__main__":
    unittest.main()
