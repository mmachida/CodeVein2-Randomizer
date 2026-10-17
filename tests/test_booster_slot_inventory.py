from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from cv2randomizer.game.booster_slot_unlock_generation import (
    BOOSTER_SLOT_FLAGS_PACKAGE,
    BOOSTER_SLOT_FLAG_ROWS,
    SECOND_WEAPON_SLOT_FLAG_ROW,
    SUPPORTED_UNLOCK_FLAG_ROWS,
    BoosterSlotUnlockResult,
    _validate_target_flags,
    apply_booster_slot_unlocks,
    inventory_for_booster_slot_flags,
)
from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.content_availability import PackageInventory


class BoosterSlotInventoryTests(unittest.TestCase):
    # Check supported slot flags have stable
    def test_supported_slot_flags_have_stable_order_and_reject_unknown_names(self) -> None:
        self.assertEqual(SUPPORTED_UNLOCK_FLAG_ROWS, (*BOOSTER_SLOT_FLAG_ROWS, SECOND_WEAPON_SLOT_FLAG_ROW))
        self.assertEqual(
            _validate_target_flags((SECOND_WEAPON_SLOT_FLAG_ROW, BOOSTER_SLOT_FLAG_ROWS[0])),
            (BOOSTER_SLOT_FLAG_ROWS[0], SECOND_WEAPON_SLOT_FLAG_ROW),
        )
        with self.assertRaisesRegex(RuntimeError, "Unsupported equipment-slot StoryFlag"):
            _validate_target_flags(("UnlockEquipSlot_Unknown",))

    # Check second weapon flag use prepared
    def test_second_weapon_flag_uses_the_prepared_cache_and_only_requests_its_row(self) -> None:
        class CacheExtractor:
            # Initialize this object
            def __init__(self, content: Path) -> None:
                self.content = content
                self.requested: tuple[str, ...] = ()

            # Extract requested game assets
            def extract_assets(self, _game_root: Path, packages, **_kwargs):
                self.requested = tuple(packages)
                header = self.content / "DT_StoryFlagDefine_GameSystem.uasset"
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_bytes(b"cached header")
                sidecar = header.with_suffix(".uexp")
                sidecar.write_bytes(b"cached data")
                return (ExtractedAsset(BOOSTER_SLOT_FLAGS_PACKAGE, (header, sidecar)),)

            # Reuse extracted data from cache
            def with_cache(self, *_args, **_kwargs):
                raise AssertionError("Generate must keep using the persistent content-addressed cache")

        class Serializer:
            # Initialize this object
            def __init__(self) -> None:
                self.target_flags: tuple[str, ...] = ()

            # Patch the prepared asset package
            def patch(self, *, work_root: Path, output_file: Path, target_flags, **_kwargs):
                self.target_flags = target_flags
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_bytes(b"patched header")
                sidecar = output_file.with_suffix(".uexp")
                sidecar.write_bytes(b"patched data")
                return BoosterSlotUnlockResult((output_file, sidecar), target_flags, True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "generation").mkdir()
            extractor = CacheExtractor(root / "ready-cache" / "Content")
            serializer = Serializer()
            result = apply_booster_slot_unlocks(
                game_root=root / "game",
                work_root=root / "generation",
                output_content_root=root / "generation" / "staging" / "Content",
                mappings_path=root / "CV2.usmap",
                game_version="fixture-build",
                data_revision="fixture-revision",
                extractor=extractor,
                package_inventory=PackageInventory.create((BOOSTER_SLOT_FLAGS_PACKAGE,)),
                serializer=serializer,
                unlock_all_booster_slots=False,
                unlock_second_weapon=True,
            )

        self.assertEqual(extractor.requested, (BOOSTER_SLOT_FLAGS_PACKAGE,))
        self.assertEqual(serializer.target_flags, (SECOND_WEAPON_SLOT_FLAG_ROW,))
        self.assertEqual(result.patched_flags, (SECOND_WEAPON_SLOT_FLAG_ROW,))

    # Check partial table output is rolled back
    def test_partial_table_output_is_removed_when_layout_validation_fails(self) -> None:
        class Extractor:
            # Extract requested game assets
            def extract_assets(self, _game_root, packages, **_kwargs):
                source = root / "cache" / "DT_StoryFlagDefine_GameSystem.uasset"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"source")
                sidecar = source.with_suffix(".uexp")
                sidecar.write_bytes(b"source data")
                return (ExtractedAsset(packages[0], (source, sidecar)),)

        class FailingSerializer:
            # Patch and then reject incompatible data
            def patch(self, *, output_file: Path, **_kwargs):
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_bytes(b"partial header")
                output_file.with_suffix(".uexp").write_bytes(b"partial data")
                raise RuntimeError("InvalidDataException: StoryFlag layout changed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = root / "generation"
            generation.mkdir()
            output_root = generation / "staging" / "Content"
            output = output_root / "Blueprint" / "GameSystem" / "StoryFlag" / "StoryFlag" / "Define" / "DT_StoryFlagDefine_GameSystem.uasset"
            with self.assertRaisesRegex(RuntimeError, "InvalidDataException"):
                apply_booster_slot_unlocks(
                    game_root=root / "game",
                    work_root=generation,
                    output_content_root=output_root,
                    mappings_path=root / "CV2.usmap",
                    game_version="fixture",
                    data_revision="revision",
                    extractor=Extractor(),
                    package_inventory=PackageInventory.create((BOOSTER_SLOT_FLAGS_PACKAGE,)),
                    serializer=FailingSerializer(),
                    unlock_all_booster_slots=False,
                    unlock_second_weapon=True,
                )
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".uexp").exists())

    # Check use installed inventory optional table
    def test_uses_installed_inventory_when_optional_table_is_not_in_asset_cache(self) -> None:
        cached_inventory = PackageInventory.create(("/Game/Fixture/CachedAsset",))
        installed_inventory = PackageInventory.create(
            ("/Game/Fixture/CachedAsset", BOOSTER_SLOT_FLAGS_PACKAGE)
        )
        extractor = Mock()
        extractor.inspect_package_inventory.return_value = installed_inventory

        with tempfile.TemporaryDirectory() as temporary:
            result = inventory_for_booster_slot_flags(
                game_root=Path(temporary),
                cached_inventory=cached_inventory,
                installed_extractor=extractor,
            )

        self.assertIs(result, installed_inventory)
        extractor.inspect_package_inventory.assert_called_once_with(Path(temporary))

    # Check reuses cached inventory it contain
    def test_reuses_cached_inventory_when_it_contains_the_flag_table(self) -> None:
        cached_inventory = PackageInventory.create((BOOSTER_SLOT_FLAGS_PACKAGE,))
        extractor = Mock()

        result = inventory_for_booster_slot_flags(
            game_root=Path("fixture-game"),
            cached_inventory=cached_inventory,
            installed_extractor=extractor,
        )

        self.assertIs(result, cached_inventory)
        extractor.inspect_package_inventory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
