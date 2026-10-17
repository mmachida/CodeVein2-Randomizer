from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.tutorial_disable_generation import (
    BASE_TUTORIAL_FLAG_TABLE,
    TUTORIAL_FLAG_TABLES,
    TutorialDisableResult,
    TutorialDisableSerializer,
    TutorialTablePatch,
    _validate_table_patches,
    apply_tutorial_disable,
    inventory_for_tutorial_flags,
)
from cv2randomizer.generation.pipeline import (
    _changed_package_registry,
    _expected_output_files,
    _known_source_packages,
)


class TutorialDisableGenerationTests(unittest.TestCase):
    # Check table catalog order base table
    def test_table_catalog_order_and_base_table_requirement_are_stable(self) -> None:
        self.assertEqual(len(TUTORIAL_FLAG_TABLES), 3)
        self.assertEqual(TUTORIAL_FLAG_TABLES[0], BASE_TUTORIAL_FLAG_TABLE)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source.uasset"
            source.write_bytes(b"source")
            with self.assertRaisesRegex(RuntimeError, "include the base table"):
                _validate_table_patches(root, (TutorialTablePatch(TUTORIAL_FLAG_TABLES[1], source, root / "out.uasset"),))

    # Check use cached inventory every tutorial
    def test_uses_cached_inventory_when_every_tutorial_table_is_prepared(self) -> None:
        inventory = PackageInventory.create(TUTORIAL_FLAG_TABLES)
        extractor = Mock()
        result = inventory_for_tutorial_flags(
            game_root=Path("fixture-game"),
            cached_inventory=inventory,
            installed_extractor=extractor,
        )
        self.assertIs(result, inventory)
        extractor.inspect_package_inventory.assert_not_called()

    # Check extract source registry include all
    def test_extract_source_registry_includes_all_optional_tutorial_tables(self) -> None:
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
            include_tutorial_flag_tables=True,
        )

        self.assertEqual(set(registry["tutorial_flags"]), set(TUTORIAL_FLAG_TABLES))

    # Check use prepared inventory without scanning
    def test_uses_prepared_inventory_without_scanning_for_uninstalled_dlc_tables(self) -> None:
        cached = PackageInventory.create((BASE_TUTORIAL_FLAG_TABLE,))
        extractor = Mock()
        result = inventory_for_tutorial_flags(
            game_root=Path("fixture-game"),
            cached_inventory=cached,
            installed_extractor=extractor,
        )
        self.assertIs(result, cached)
        extractor.inspect_package_inventory.assert_not_called()

    # Check applies every installed tutorial table
    def test_applies_to_every_installed_tutorial_table_and_stages_outputs(self) -> None:
        class CacheExtractor:
            # Initialize this object
            def __init__(self, root: Path) -> None:
                self.root = root
                self.requested: tuple[str, ...] = ()

            # Extract requested game assets
            def extract_assets(self, _game_root: Path, packages, **_kwargs):
                self.requested = tuple(packages)
                assets = []
                for package in self.requested:
                    header = self.root / (package.rsplit("/", 1)[-1] + ".uasset")
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes(b"cached header")
                    sidecar = header.with_suffix(".uexp")
                    sidecar.write_bytes(b"cached data")
                    assets.append(ExtractedAsset(package, (header, sidecar)))
                return tuple(assets)

        class Serializer:
            # Initialize this object
            def __init__(self) -> None:
                self.tables: tuple[TutorialTablePatch, ...] = ()

            # Patch the prepared asset package
            def patch(self, *, tables, **_kwargs):
                self.tables = tables
                files = []
                for table in tables:
                    table.output_file.parent.mkdir(parents=True, exist_ok=True)
                    table.output_file.write_bytes(b"patched header")
                    sidecar = table.output_file.with_suffix(".uexp")
                    sidecar.write_bytes(b"patched data")
                    files.extend((table.output_file, sidecar))
                return TutorialDisableResult(tuple(files), tuple(table.virtual_package for table in tables), 100, True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = root / "generation"
            generation.mkdir()
            extractor = CacheExtractor(root / "prepared-cache" / "Content")
            serializer = Serializer()
            result = apply_tutorial_disable(
                game_root=root / "game",
                work_root=generation,
                output_content_root=generation / "staging" / "Content",
                mappings_path=root / "CV2.usmap",
                game_version="fixture-build",
                data_revision="fixture-revision",
                extractor=extractor,
                package_inventory=PackageInventory.create(TUTORIAL_FLAG_TABLES),
                serializer=serializer,
                table_packages=TUTORIAL_FLAG_TABLES,
            )

        self.assertEqual(extractor.requested, TUTORIAL_FLAG_TABLES)
        self.assertEqual(tuple(table.virtual_package for table in serializer.tables), TUTORIAL_FLAG_TABLES)
        self.assertEqual(result.patched_tables, TUTORIAL_FLAG_TABLES)
        self.assertEqual(result.patched_flag_count, 100)
        self.assertEqual(len(result.output_files), 6)

    # Check partial tutorial outputs are rolled back
    def test_partial_tutorial_outputs_are_removed_when_layout_validation_fails(self) -> None:
        class Extractor:
            # Extract requested game assets
            def extract_assets(self, _game_root, packages, **_kwargs):
                assets = []
                for package in packages:
                    source = root / "cache" / f"{package.rsplit('/', 1)[-1]}.uasset"
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_bytes(b"source")
                    sidecar = source.with_suffix(".uexp")
                    sidecar.write_bytes(b"source data")
                    assets.append(ExtractedAsset(package, (source, sidecar)))
                return tuple(assets)

        class FailingSerializer:
            # Partially patch and reject layout
            def patch(self, *, tables, **_kwargs):
                for table in tables:
                    table.output_file.parent.mkdir(parents=True, exist_ok=True)
                    table.output_file.write_bytes(b"partial header")
                    table.output_file.with_suffix(".uexp").write_bytes(b"partial data")
                raise RuntimeError("InvalidDataException: tutorial table layout changed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = root / "generation"
            generation.mkdir()
            output_root = generation / "staging" / "Content"
            with self.assertRaisesRegex(RuntimeError, "InvalidDataException"):
                apply_tutorial_disable(
                    game_root=root / "game",
                    work_root=generation,
                    output_content_root=output_root,
                    mappings_path=root / "CV2.usmap",
                    game_version="fixture",
                    data_revision="revision",
                    extractor=Extractor(),
                    package_inventory=PackageInventory.create((BASE_TUTORIAL_FLAG_TABLE,)),
                    serializer=FailingSerializer(),
                    table_packages=(BASE_TUTORIAL_FLAG_TABLE,),
                )
            self.assertEqual(tuple(output_root.rglob("*.uasset")), ())
            self.assertEqual(tuple(output_root.rglob("*.uexp")), ())

    # Check serializer validate helper outputs cleans
    def test_serializer_validates_helper_outputs_and_cleans_request_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            helper = root / "helper.exe"
            helper.touch()
            mappings = root / "CV2.usmap"
            mappings.touch()
            source = root / "source" / "DT_StoryFlagTutorialDefine.uasset"
            source.parent.mkdir()
            source.write_bytes(b"source")
            output = root / "staging" / "DT_StoryFlagTutorialDefine.uasset"
            table = TutorialTablePatch(BASE_TUTORIAL_FLAG_TABLE, source, output)

            # Run the requested operation
            def runner(command, **_kwargs):
                manifest = Path(command[-1])
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                self.assertEqual(payload["tables"][0]["virtualPackage"], BASE_TUTORIAL_FLAG_TABLE)
                output.parent.mkdir(parents=True)
                output.write_bytes(b"patched")
                sidecar = output.with_suffix(".uexp")
                sidecar.write_bytes(b"patched data")
                return subprocess.CompletedProcess(command, 0, json.dumps({
                    "patchedTables": [BASE_TUTORIAL_FLAG_TABLE],
                    "patchedFlagCount": 1,
                    "outputFiles": [str(output), str(sidecar)],
                    "reopenedAndValidated": True,
                }), "")

            serializer = TutorialDisableSerializer(helper_project=helper, runner=runner)
            result = serializer.patch(work_root=root, mappings_path=mappings, tables=(table,))
            self.assertTrue(result.reopened)
            self.assertEqual(result.patched_flag_count, 1)
            self.assertFalse(tuple(root.glob(".cv2-disable-tutorials-*.json")))

    # Check tutorial outputs join validated miscellaneous
    def test_tutorial_outputs_join_the_validated_miscellaneous_package_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            header = root / "Content" / "Blueprint" / "GameSystem" / "StoryFlag" / "DT_StoryFlagTutorialDefine.uasset"
            header.parent.mkdir(parents=True)
            header.write_bytes(b"header")
            sidecar = header.with_suffix(".uexp")
            sidecar.write_bytes(b"sidecar")
            result = TutorialDisableResult(
                (header, sidecar), (BASE_TUTORIAL_FLAG_TABLE,), 115, True,
            )
            expected = _expected_output_files(None, None, None, tutorial_disable=result)
            registry = _changed_package_registry(
                root / "Content",
                world=None,
                shops=None,
                materials=None,
                enemy_drops=None,
                blood_codes=None,
                boosters=None,
                defensive_gadgets=None,
                bequeathed_formae=None,
                direct_grants=(),
                extractor=None,
                initial_inventory=None,
                tutorial_disable=result,
            )
            registered = {
                (root / "Content" / path).resolve()
                for package in registry
                for path in package["files"]
            }
            self.assertEqual(registered, {path.resolve() for path in expected})
            self.assertEqual(registry[0]["sources"], ["miscellaneous"])


if __name__ == "__main__":
    unittest.main()
