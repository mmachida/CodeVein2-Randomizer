from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.motorcycle_unlock_generation import (
    MOTORCYCLE_CACHE_PACKAGES,
    MOTORCYCLE_PLAYER_PACKAGE,
    MOTORCYCLE_UNLOCK_FLAGS_PACKAGE,
    MOTORCYCLE_UNLOCK_FLAG_ROWS,
    MOTORCYCLE_UNLOCK_PACKAGES,
    MotorcycleUnlockResult,
    MotorcycleUnlockSerializer,
    apply_motorcycle_unlock,
    inventory_for_motorcycle_unlock_packages,
)
from cv2randomizer.generation.pipeline import _changed_package_registry, _expected_output_files


class MotorcycleUnlockGenerationTests(unittest.TestCase):
    # Check use installed inventory either optional
    def test_uses_installed_inventory_when_either_optional_package_is_missing(self) -> None:
        cached = PackageInventory.create((MOTORCYCLE_UNLOCK_FLAGS_PACKAGE,))
        installed = PackageInventory.create(MOTORCYCLE_UNLOCK_PACKAGES)
        extractor = Mock()
        extractor.inspect_package_inventory.return_value = installed

        result = inventory_for_motorcycle_unlock_packages(
            game_root=Path("fixture-game"),
            cached_inventory=cached,
            inventory_extractor=extractor,
        )

        self.assertIs(result, installed)
        extractor.inspect_package_inventory.assert_called_once_with(Path("fixture-game"))

    # Check reuses cached inventory it contain
    def test_reuses_cached_inventory_when_it_contains_both_packages(self) -> None:
        cached = PackageInventory.create(MOTORCYCLE_UNLOCK_PACKAGES)
        extractor = Mock()

        result = inventory_for_motorcycle_unlock_packages(
            game_root=Path("fixture-game"),
            cached_inventory=cached,
            inventory_extractor=extractor,
        )

        self.assertIs(result, cached)
        extractor.inspect_package_inventory.assert_not_called()

    # Check extracts stages story flag mount
    def test_extracts_and_stages_story_flag_and_mount_gate_overrides(self) -> None:
        class FakeExtractor:
            # Initialize this object
            def __init__(self, assets: tuple[ExtractedAsset, ...]) -> None:
                self.assets = assets
                self.requests: list[tuple[str, ...]] = []

            # Reuse extracted data from cache
            def with_cache(self, _cache, *, package_inventory=None):
                return self

            # Extract requested game assets
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                self.requests.append(tuple(virtual_paths))
                return self.assets

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets: list[ExtractedAsset] = []
            sources: dict[str, Path] = {}
            for package in MOTORCYCLE_UNLOCK_PACKAGES:
                name = package.rsplit("/", 1)[-1]
                source = root / "source-cache" / f"{name}.uasset"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"header")
                sidecar = source.with_suffix(".uexp")
                sidecar.write_bytes(b"exports")
                assets.append(ExtractedAsset(package, (source, sidecar)))
                sources[package] = source
            extractor = FakeExtractor(tuple(assets))
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"mapping")
            output_root = root / "staging" / "Content"
            output_root.mkdir(parents=True)
            patch_calls: list[str] = []

            # Patch the prepared asset package
            def patch(**kwargs):
                package = kwargs["virtual_package"]
                source = sources[package]
                output = kwargs["output_file"]
                self.assertEqual(kwargs["input_file"].name, source.name)
                self.assertEqual(output.name, source.name)
                patch_calls.append(package)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"patched-header")
                sidecar = output.with_suffix(".uexp")
                sidecar.write_bytes(b"patched-exports")
                is_flag_table = package == MOTORCYCLE_UNLOCK_FLAGS_PACKAGE
                return MotorcycleUnlockResult(
                    (output, sidecar),
                    MOTORCYCLE_UNLOCK_FLAG_ROWS if is_flag_table else (),
                    not is_flag_table,
                    True,
                )

            serializer = Mock()
            serializer.patch.side_effect = patch
            result = apply_motorcycle_unlock(
                game_root=root / "game",
                work_root=root,
                output_content_root=output_root,
                mappings_path=mappings,
                game_version="fixture",
                data_revision="revision",
                extractor=extractor,
                package_inventory=PackageInventory.create(MOTORCYCLE_UNLOCK_PACKAGES),
                serializer=serializer,
            )

            self.assertEqual(extractor.requests, [MOTORCYCLE_UNLOCK_PACKAGES])
            self.assertEqual(patch_calls, list(MOTORCYCLE_UNLOCK_PACKAGES))
            self.assertEqual(result.patched_flags, MOTORCYCLE_UNLOCK_FLAG_ROWS)
            self.assertTrue(result.patched_mount_gate)
            self.assertTrue(result.reopened)
            self.assertEqual(len(result.output_files), 4)

    # Check reuses persistent cache extractor without
    def test_reuses_persistent_cache_extractor_without_creating_a_generation_cache(self) -> None:
        class CacheOnlyExtractor:
            # Initialize this object
            def __init__(self, assets: tuple[ExtractedAsset, ...]) -> None:
                self.assets = assets
                self.requests: list[tuple[str, ...]] = []

            # Extract requested game assets
            def extract_assets(self, _game_root, virtual_paths, **_kwargs):
                self.requests.append(tuple(virtual_paths))
                return self.assets

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets: list[ExtractedAsset] = []
            for package in MOTORCYCLE_UNLOCK_PACKAGES:
                name = package.rsplit("/", 1)[-1]
                source = root / "persistent-cache" / f"{name}.uasset"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"cached header")
                sidecar = source.with_suffix(".uexp")
                sidecar.write_bytes(b"cached exports")
                assets.append(ExtractedAsset(package, (source, sidecar)))
            extractor = CacheOnlyExtractor(tuple(assets))
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"mapping")
            output_root = root / "staging" / "Content"
            output_root.mkdir(parents=True)

            # Patch the prepared asset package
            def patch(**kwargs):
                output = kwargs["output_file"]
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"patched header")
                sidecar = output.with_suffix(".uexp")
                sidecar.write_bytes(b"patched exports")
                is_flag_table = kwargs["virtual_package"] == MOTORCYCLE_UNLOCK_FLAGS_PACKAGE
                return MotorcycleUnlockResult(
                    (output, sidecar),
                    MOTORCYCLE_UNLOCK_FLAG_ROWS if is_flag_table else (),
                    not is_flag_table,
                    True,
                )

            serializer = Mock()
            serializer.patch.side_effect = patch
            result = apply_motorcycle_unlock(
                game_root=root / "game",
                work_root=root,
                output_content_root=output_root,
                mappings_path=mappings,
                game_version="fixture",
                data_revision="revision",
                extractor=extractor,
                package_inventory=PackageInventory.create(MOTORCYCLE_CACHE_PACKAGES),
                serializer=serializer,
            )

            self.assertEqual(extractor.requests, [MOTORCYCLE_UNLOCK_PACKAGES])
            self.assertFalse((root / "motorcycle-unlock-extract").exists())
            self.assertTrue(result.patched_mount_gate)

    # Check serializer validate story flag patch
    def test_serializer_validates_story_flag_patch_response(self) -> None:
        self._assert_serializer_response(
            virtual_package=MOTORCYCLE_UNLOCK_FLAGS_PACKAGE,
            patched_flags=list(MOTORCYCLE_UNLOCK_FLAG_ROWS),
            patched_mount_gate=False,
        )

    # Check serializer validate mount gate patch
    def test_serializer_validates_mount_gate_patch_response(self) -> None:
        self._assert_serializer_response(
            virtual_package=MOTORCYCLE_PLAYER_PACKAGE,
            patched_flags=[],
            patched_mount_gate=True,
        )

    # Assert serializer response
    def _assert_serializer_response(self, *, virtual_package: str, patched_flags: list[str], patched_mount_gate: bool) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "helper.exe"
            helper.write_bytes(b"helper")
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"mapping")
            work = root / "work"
            work.mkdir()
            source = work / "source.uasset"
            source.write_bytes(b"source")
            output = work / "Content" / "patched.uasset"
            output.parent.mkdir()

            # Run the requested operation
            def runner(command, **_kwargs):
                manifest = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
                self.assertEqual(manifest["virtualPackage"], virtual_package)
                target = Path(manifest["outputFile"])
                target.write_bytes(b"patched")
                response = {
                    "virtualPackage": virtual_package,
                    "outputFiles": [str(target)],
                    "patchedFlags": patched_flags,
                    "patchedMountGate": patched_mount_gate,
                    "reopenedAndValidated": True,
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

            serializer = MotorcycleUnlockSerializer(helper_project=helper, runner=runner)
            result = serializer.patch(
                work_root=work,
                mappings_path=mappings,
                input_file=source,
                output_file=output,
                virtual_package=virtual_package,
            )

        self.assertEqual(result.patched_flags, tuple(patched_flags))
        self.assertEqual(result.patched_mount_gate, patched_mount_gate)
        self.assertEqual(result.output_files, (output,))
        self.assertTrue(result.reopened)

    # Check motorcycle package files are registered
    def test_motorcycle_package_files_are_registered_for_container_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary) / "Content"
            headers: list[Path] = []
            files: list[Path] = []
            for package in MOTORCYCLE_UNLOCK_PACKAGES:
                header = content / Path(*package.removeprefix("/Game/").split("/")).with_suffix(".uasset")
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_bytes(b"header")
                export = header.with_suffix(".uexp")
                export.write_bytes(b"exports")
                headers.append(header)
                files.extend((header, export))
            result = MotorcycleUnlockResult(tuple(files), MOTORCYCLE_UNLOCK_FLAG_ROWS, True, True)

            expected = _expected_output_files(None, None, None, motorcycle_unlock=result)
            registry = _changed_package_registry(
                content,
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
                motorcycle_unlock=result,
            )

        self.assertEqual(set(expected), set(files))
        self.assertEqual({item["virtual_package"] for item in registry}, set(MOTORCYCLE_UNLOCK_PACKAGES))
        self.assertTrue(all(item["sources"] == ["miscellaneous"] for item in registry))


if __name__ == "__main__":
    unittest.main()
