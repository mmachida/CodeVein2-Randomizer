from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cv2randomizer.game.asset_extraction import MaterializedAsset
from cv2randomizer.game.shop_generation import prepare_shop_tables
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES


class RecordingExtractor:
    # Initialize this object
    def __init__(self) -> None:
        self.package_paths: tuple[str, ...] = ()
        self.optional_paths: tuple[str, ...] = ()

    # Extract requested game assets
    def extract_assets(self, _game_root, package_paths, **kwargs):
        self.package_paths = tuple(package_paths)
        self.optional_paths = tuple(kwargs["optional_virtual_paths"])
        return ()


class ShopGenerationSelectionTests(unittest.TestCase):
    # Resolve requested paths
    def _requested_paths(
        self,
        families: tuple[str, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with tempfile.TemporaryDirectory(prefix="cv2-shop-selection-") as temporary:
            root = Path(temporary)
            mapping = root / "CV2.usmap"
            mapping.write_bytes(b"test mapping")
            extractor = RecordingExtractor()
            catalogs = SimpleNamespace(
                shops=tuple(SimpleNamespace(family=family) for family in families)
            )
            with (
                patch("cv2randomizer.game.shop_generation.materialize_extracted_assets", return_value=[]),
                patch("cv2randomizer.game.shop_generation.read_usmap", return_value=object()),
            ):
                stage = prepare_shop_tables(
                    catalogs,
                    game_root=root,
                    work_root=root / "work",
                    mappings_path=mapping,
                    game_version="test",
                    data_revision="test",
                    extractor=extractor,  # type: ignore[arg-type]
                )
            self.assertEqual(set(stage.unavailable_families), set(families))
            return extractor.package_paths, extractor.optional_paths

    # Check requested shop tables are optional
    def test_catalogued_tables_are_requested_as_optional_sources(self) -> None:
        base_paths, base_optional = self._requested_paths(("NORMAL", "LUXURY", "TRADE"))
        self.assertEqual(
            {path.rsplit("/", 1)[-1] for path in base_paths},
            {SHOP_TABLE_NAMES[family] for family in ("NORMAL", "LUXURY", "TRADE")},
        )
        self.assertEqual(set(base_optional), set(base_paths))

        dlc_paths, dlc_optional = self._requested_paths(
            ("NORMAL", "LUXURY", "TRADE", "DLC", "TRADE_DLC")
        )
        self.assertEqual(
            {path.rsplit("/", 1)[-1] for path in dlc_paths},
            set(SHOP_TABLE_NAMES.values()),
        )
        self.assertEqual(set(dlc_optional), set(dlc_paths))

    # Skip offers absent from this version
    def test_filters_catalog_offers_missing_from_game_name_map(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cv2-shop-version-") as temporary:
            root = Path(temporary)
            mapping = root / "CV2.usmap"
            mapping.write_bytes(b"test mapping")
            header = root / "DT_ShopLineup.uasset"
            export = root / "DT_ShopLineup.uexp"
            header.write_bytes(b"header")
            export.write_bytes(b"export")
            family_path = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"
            row = "Shop_Test"
            present = SimpleNamespace(
                family="NORMAL", shop_row=row, product_index=0,
                original_product_path="/Game/Blueprint/Article/Item/DA_Present",
                item_object="DA_Present",
            )
            absent = SimpleNamespace(
                family="NORMAL", shop_row=row, product_index=1,
                original_product_path="/Game/Blueprint/Article/Item/DA_DlcOnly",
                item_object="DA_DlcOnly",
            )
            infinite = SimpleNamespace(
                family="NORMAL", shop_row=row, product_index=2,
                original_product_path="/Game/Blueprint/Article/Item/DA_Infinite",
                item_object="DA_Infinite", uses_stock=False, stock=0,
            )
            protected = SimpleNamespace(
                family="NORMAL", shop_row=row, product_index=3,
                original_product_path="/Game/Blueprint/Article/Item/DA_Key",
                item_object="DA_Key", protected=True, uses_stock=True, stock=1,
            )
            name_map = SimpleNamespace(names=(row, present.original_product_path, present.item_object))
            extractor = RecordingExtractor()
            extractor.extract_assets = lambda *_args, **_kwargs: (SimpleNamespace(
                virtual_path=family_path, files=(header, export),
            ),)  # type: ignore[method-assign]

            with (
                patch("cv2randomizer.game.shop_generation.materialize_extracted_assets", return_value=(
                    MaterializedAsset(family_path, (header, export)),
                )),
                patch("cv2randomizer.game.shop_generation.read_usmap", return_value=object()),
                patch("cv2randomizer.game.shop_generation.read_cooked_name_map", return_value=name_map),
                patch("cv2randomizer.game.shop_generation.parse_shop_table", return_value=SimpleNamespace(products=())) as parse,
            ):
                stage = prepare_shop_tables(
                    SimpleNamespace(shops=(present, absent, infinite, protected)),
                    game_root=root,
                    work_root=root / "work",
                    mappings_path=mapping,
                    game_version="1.0.3",
                    data_revision="test",
                    enabled_families=("NORMAL",),
                    extractor=extractor,  # type: ignore[arg-type]
                )

            self.assertEqual(stage.tables[0].expected_products, (present,))
            self.assertEqual(stage.skipped_catalog_product_count, 1)
            self.assertEqual(stage.incompatible_families, ())
            self.assertEqual(parse.call_args.kwargs["expected_products"], (present,))

    # Leave layouts with catalog gaps untouched
    def test_skips_family_when_available_catalog_slots_have_a_gap(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cv2-shop-layout-") as temporary:
            root = Path(temporary)
            mapping = root / "CV2.usmap"
            mapping.write_bytes(b"test mapping")
            header = root / "DT_ShopLineup.uasset"
            export = root / "DT_ShopLineup.uexp"
            header.write_bytes(b"header")
            export.write_bytes(b"export")
            family_path = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"
            row = "Shop_Test"
            missing_slot = SimpleNamespace(
                family="NORMAL", shop_row=row, product_index=0,
                original_product_path="/Game/Blueprint/Article/Item/DA_Missing",
                item_object="DA_Missing",
            )
            later_slot = SimpleNamespace(
                family="NORMAL", shop_row=row, product_index=1,
                original_product_path="/Game/Blueprint/Article/Item/DA_Later",
                item_object="DA_Later",
            )
            name_map = SimpleNamespace(names=(row, later_slot.original_product_path, later_slot.item_object))
            extractor = RecordingExtractor()
            extractor.extract_assets = lambda *_args, **_kwargs: (SimpleNamespace(
                virtual_path=family_path, files=(header, export),
            ),)  # type: ignore[method-assign]

            with (
                patch("cv2randomizer.game.shop_generation.materialize_extracted_assets", return_value=(
                    MaterializedAsset(family_path, (header, export)),
                )),
                patch("cv2randomizer.game.shop_generation.read_usmap", return_value=object()),
                patch("cv2randomizer.game.shop_generation.read_cooked_name_map", return_value=name_map),
                patch("cv2randomizer.game.shop_generation.parse_shop_table") as parse,
            ):
                stage = prepare_shop_tables(
                    SimpleNamespace(shops=(missing_slot, later_slot)),
                    game_root=root,
                    work_root=root / "work",
                    mappings_path=mapping,
                    game_version="1.0.3",
                    data_revision="test",
                    enabled_families=("NORMAL",),
                    extractor=extractor,  # type: ignore[arg-type]
                )

            self.assertEqual(stage.tables, ())
            self.assertEqual(stage.incompatible_families, ("NORMAL",))
            self.assertEqual(stage.skipped_catalog_product_count, 0)
            parse.assert_not_called()

if __name__ == "__main__":
    unittest.main()
