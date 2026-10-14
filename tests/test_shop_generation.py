from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cv2randomizer.game.shop_generation import ShopGenerationError, prepare_shop_tables
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
                with self.assertRaisesRegex(ShopGenerationError, "omitted required"):
                    prepare_shop_tables(
                        catalogs,
                        game_root=root,
                        work_root=root / "work",
                        mappings_path=mapping,
                        game_version="test",
                        data_revision="test",
                        extractor=extractor,  # type: ignore[arg-type]
                    )
            return extractor.package_paths, extractor.optional_paths

    # Check catalogued additional tables are requested
    def test_catalogued_additional_tables_are_requested_as_optional_sources(self) -> None:
        base_paths, base_optional = self._requested_paths(("NORMAL", "LUXURY", "TRADE"))
        self.assertEqual(
            {path.rsplit("/", 1)[-1] for path in base_paths},
            {SHOP_TABLE_NAMES[family] for family in ("NORMAL", "LUXURY", "TRADE")},
        )
        self.assertEqual(base_optional, ())

        dlc_paths, dlc_optional = self._requested_paths(
            ("NORMAL", "LUXURY", "TRADE", "DLC", "TRADE_DLC")
        )
        self.assertEqual(
            {path.rsplit("/", 1)[-1] for path in dlc_paths},
            set(SHOP_TABLE_NAMES.values()),
        )
        self.assertEqual(
            {path.rsplit("/", 1)[-1] for path in dlc_optional},
            {SHOP_TABLE_NAMES["DLC"], SHOP_TABLE_NAMES["TRADE_DLC"]},
        )

if __name__ == "__main__":
    unittest.main()
