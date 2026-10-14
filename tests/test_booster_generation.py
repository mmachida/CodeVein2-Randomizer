from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.booster_catalog import (
    BOOSTER_ROW_STRUCT_PACKAGE,
    BOOSTER_TABLE_PACKAGE,
    BoosterCatalogStore,
)
from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.booster_generation import (
    BoosterRowSnapshot,
    prepare_booster_plan,
)
from cv2randomizer.game.booster_slot_unlock_generation import BOOSTER_SLOT_FLAGS_PACKAGE
from cv2randomizer.game.motorcycle_unlock_generation import MOTORCYCLE_CACHE_PACKAGES
from cv2randomizer.generation.pipeline import _known_source_packages


ROOT = Path(__file__).resolve().parents[1]


class _Extractor:
    # Initialize this object
    def __init__(self, cache_content: Path) -> None:
        self.cache_content = cache_content
        self.requested: tuple[str, ...] = ()

    # Extract requested game assets
    def extract_assets(self, _game_root: Path, packages: tuple[str, ...], **_kwargs: Any):
        self.requested = tuple(packages)
        results = []
        for package in packages:
            header = self.cache_content / Path(*package.split("/")[2:]).with_suffix(".uasset")
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_bytes(("header:" + package).encode("utf-8"))
            sidecar = header.with_suffix(".uexp")
            sidecar.write_bytes(("sidecar:" + package).encode("utf-8"))
            results.append(ExtractedAsset(package, (header, sidecar)))
        return tuple(results)


class _Serializer:
    # Initialize this object
    def __init__(self, catalog) -> None:
        self.catalog = catalog
        self.inspected_file: Path | None = None

    # Inspect table
    def inspect_table(self, *, input_file: Path, **_kwargs: Any):
        self.inspected_file = input_file.resolve(strict=True)
        struct_file = self.inspected_file.parent / "STR_GainItemSmallScenario.uasset"
        if not struct_file.is_file() or not struct_file.with_suffix(".uexp").is_file():
            raise AssertionError("Booster DataTable inspection must have its UserDefinedStruct sidecars beside it.")
        return tuple(
            BoosterRowSnapshot(
                row_index=slot.row_index,
                row_name=slot.row_name,
                evaluation_formula=slot.evaluation_formula_original,
                mastery_owner=slot.mastery_owner,
                mastery_rank=slot.mastery_rank,
                blood_code_package=slot.blood_code_article_path,
                blood_code_asset=slot.blood_code_article_asset,
                booster_story_flag=slot.booster_story_flag,
                booster_package=slot.booster_article_path,
                booster_asset=slot.booster_article_asset,
                count=slot.count,
                notification=slot.b_notification,
            )
            for slot in self.catalog.slots
        )


class BoosterGenerationDependencyTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.catalog = BoosterCatalogStore(ROOT / "data" / "boosters" / "current").load("current")

    # Check prepare materializes inspects user defined
    def test_prepare_materializes_and_inspects_the_user_defined_row_struct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            extractor = _Extractor(root / "cache" / "Content")
            table_header = extractor.cache_content / Path(*BOOSTER_TABLE_PACKAGE.split("/")[2:]).with_suffix(".uasset")
            table_sidecar = table_header.with_suffix(".uexp")
            table_header.parent.mkdir(parents=True, exist_ok=True)
            table_header.write_bytes(("header:" + BOOSTER_TABLE_PACKAGE).encode("utf-8"))
            table_sidecar.write_bytes(("sidecar:" + BOOSTER_TABLE_PACKAGE).encode("utf-8"))
            catalog = replace(
                self.catalog,
                source_package_sha256={
                    ".uasset": hashlib.sha256(table_header.read_bytes()).hexdigest(),
                    ".uexp": hashlib.sha256(table_sidecar.read_bytes()).hexdigest(),
                },
            )
            serializer = _Serializer(catalog)

            preparation = prepare_booster_plan(
                catalog,
                "booster-struct-dependency",
                game_root=root / "game",
                work_root=root,
                mappings_path=mappings,
                game_version="fixture-build",
                data_revision="current",
                extractor=extractor,
                serializer=serializer,
                mix_postgame_dlc_with_base=False,
            )

            self.assertEqual(extractor.requested, (BOOSTER_TABLE_PACKAGE, BOOSTER_ROW_STRUCT_PACKAGE))
            self.assertIsNotNone(serializer.inspected_file)
            self.assertTrue(serializer.inspected_file.is_relative_to(root / "source" / "Content"))
            self.assertEqual(serializer.inspected_file, preparation.source_file)
            self.assertEqual(len(preparation.plan.assignments), 102)

    # Check booster extraction registry include row
    def test_booster_extraction_registry_includes_the_row_struct_dependency(self) -> None:
        catalogs = SimpleNamespace(world_loot=(), materials=(), shops=())
        registry = _known_source_packages(
            catalogs,
            enemy_drop_catalog=None,
            blood_code_catalog=None,
            booster_catalog=self.catalog,
            extractor_catalog=None,
            defensive_gadget_catalog=None,
            jail_catalog=None,
            field_enemy_catalog=None,
            canonical_article_catalog=None,
            bequeathed_formae_catalog=None,
        )

        self.assertIn(BOOSTER_TABLE_PACKAGE, registry["boosters"])
        self.assertIn(BOOSTER_ROW_STRUCT_PACKAGE, registry["boosters"])

    # Check extract registry include optional booster
    def test_extract_registry_includes_optional_booster_slot_flag_table(self) -> None:
        catalogs = SimpleNamespace(world_loot=(), materials=(), shops=())
        registry = _known_source_packages(
            catalogs,
            enemy_drop_catalog=None,
            blood_code_catalog=None,
            booster_catalog=None,
            extractor_catalog=None,
            defensive_gadget_catalog=None,
            jail_catalog=None,
            field_enemy_catalog=None,
            canonical_article_catalog=None,
            bequeathed_formae_catalog=None,
            include_booster_slot_flags=True,
        )

        self.assertEqual(registry["booster_slot_unlock"], (BOOSTER_SLOT_FLAGS_PACKAGE,))

    # Check extract registry include motorcycle unlock
    def test_extract_registry_includes_motorcycle_unlock_packages(self) -> None:
        catalogs = SimpleNamespace(world_loot=(), materials=(), shops=())
        registry = _known_source_packages(
            catalogs,
            enemy_drop_catalog=None,
            blood_code_catalog=None,
            booster_catalog=None,
            extractor_catalog=None,
            defensive_gadget_catalog=None,
            jail_catalog=None,
            field_enemy_catalog=None,
            canonical_article_catalog=None,
            bequeathed_formae_catalog=None,
            include_motorcycle_unlock_packages=True,
        )

        self.assertEqual(set(registry["motorcycle_unlock"]), set(MOTORCYCLE_CACHE_PACKAGES))

    # Check unsupported world CDO article references
    def test_unsupported_world_cdo_article_references_stay_in_extract_registry_only(self) -> None:
        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        self.assertEqual(len(catalogs.world_loot_unsupported_cdo_sources), 8)
        registry = _known_source_packages(
            catalogs,
            enemy_drop_catalog=None,
            blood_code_catalog=None,
            booster_catalog=None,
            extractor_catalog=None,
            defensive_gadget_catalog=None,
            jail_catalog=None,
            field_enemy_catalog=None,
            canonical_article_catalog=None,
            bequeathed_formae_catalog=None,
        )

        expected = {reference.reward.article_path for reference in catalogs.world_loot_unsupported_cdo_sources}
        self.assertTrue(expected <= set(registry["world_loot_articles"]))
        unsupported_packages = {reference.blueprint_package for reference in catalogs.world_loot_unsupported_cdo_sources}
        self.assertFalse(unsupported_packages & set(registry.get("world_loot", ())))


if __name__ == "__main__":
    unittest.main()
