from __future__ import annotations

import subprocess
import hashlib
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from cv2randomizer.game import asset_extraction
from cv2randomizer.core.workspace import CacheManager
from cv2randomizer.game.asset_extraction import (
    AssetExtractionError,
    IoStoreAssetExtractor,
    RETOC_VERSION,
    _MAX_RETOC_FILTER_ARGUMENT_CHARACTERS,
    _RETOC_PRIMARY_EXTENSIONS,
    _asset_batches,
    _retoc_filter_value,
    archive_source_fingerprint,
    materialize_extracted_assets,
    ExtractedAsset,
)
from cv2randomizer.game.content_availability import PackageInventory


class FakeRetocRunner:
    # Initialize this object
    def __init__(
        self,
        *,
        version: str = RETOC_VERSION,
        produce_requested: bool = True,
        missing_filters: set[str] | None = None,
        package_listing: str | None = None,
    ) -> None:
        self.version = version
        self.produce_requested = produce_requested
        self.missing_filters = missing_filters or set()
        self.package_listing = package_listing or (
            "Package: /Game/Blueprint/Article/DropItem/DT_DropItemData_DLC.uasset\n"
        )
        self.commands: list[list[str]] = []

    # Call the wrapped operation
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, self.version + "\n", "")
        if len(command) > 1 and command[1] == "list":
            return subprocess.CompletedProcess(command, 0, self.package_listing, "")
        output = Path(command[-1])
        filter_values = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--filter"]
        if self.produce_requested:
            relative_by_name = {
                "DT_ShopLineup": "Blueprint/GameSystem/shop/DT_ShopLineup",
                "DT_ShopLineup_DLC": "Blueprint/GameSystem/shop/DT_ShopLineup_DLC",
                "Map001": "Map/MainWorld/MainWorld/_Generated_/Map001",
                "008YWFVVR3WYH2OZLKIYC10JN": "Map/MainWorld/MainWorld/_Generated_/008YWFVVR3WYH2OZLKIYC10JN",
                "DlcWorld": "Map/MainWorld/DlcWorld",
                "GeneratedCell": "Map/MainWorld/DlcWorld/_Generated_/GeneratedCell",
                "DA_SingleSword_IronPipe_1": "Blueprint/Article/Weapon/SingleSword/DA_SingleSword_IronPipe_1",
            }
            for filter_value in filter_values:
                filter_name = PurePosixPath(filter_value.replace("\\", "/")).name
                filter_stem = filter_name.rsplit(".", 1)[0]
                if filter_stem in self.missing_filters:
                    continue
                relative = next(
                    value for name, value in relative_by_name.items()
                    if name.casefold() == filter_stem.casefold()
                )
                target = output / "CodeVein2" / "Content" / Path(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                extension = ".umap" if relative.startswith("Map/") else ".uasset"
                target.with_suffix(extension).write_bytes(b"validated package header")
                target.with_suffix(".uexp").write_bytes(b"export data")
        return subprocess.CompletedProcess(command, 0, "Extracted 1 (0 failed).", "")


# Read installation
def _installation(root: Path) -> Path:
    game_root = root / "game"
    pak_dir = game_root / "CodeVein2" / "Content" / "Paks"
    pak_dir.mkdir(parents=True)
    (pak_dir / "pakchunk0-Windows.utoc").write_bytes(b"toc index")
    (pak_dir / "pakchunk0-Windows.ucas").write_bytes(b"chunk data")
    return game_root


class IoStoreAssetExtractorTests(unittest.TestCase):
    # Read extractor
    def _extractor(self, root: Path, runner: FakeRetocRunner) -> IoStoreAssetExtractor:
        executable = root / "retoc.exe"
        executable.write_bytes(b"test helper placeholder")
        return IoStoreAssetExtractor(
            executable=executable,
            cache=CacheManager(root / "cache"),
            runner=runner,
        )

    # Check prepared raw cache inventory can
    def test_prepared_raw_cache_inventory_can_be_seeded_without_retoc_or_hash_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entry = root / "READY"
            package_path = "/Game/Blueprint/Article/DA_Test"
            package = entry / "assets" / "Blueprint" / "Article" / "DA_Test"
            package.mkdir(parents=True)
            payload = b"source package payload"
            package_file = package / "DA_Test.uasset"
            package_file.write_bytes(payload)
            (package / ".cv2randomizer-asset.json").write_text(json.dumps({
                "virtual_path": package_path,
                "files": [{
                    "name": package_file.name,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }],
            }), encoding="utf-8")
            runner = FakeRetocRunner()
            extractor = self._extractor(root, runner)

            seeded = extractor.prime_cached_asset_inventory(entry, (package_path,))

            self.assertEqual(seeded, 1)
            assets = extractor.cached_asset_inventory(entry)
            self.assertEqual(tuple(asset.virtual_path for asset in assets), (package_path,))
            self.assertEqual(assets[0].file_hashes, (hashlib.sha256(payload).hexdigest(),))
            self.assertEqual(runner.commands, [])

    # Check isolated extractor reuses active run
    def test_isolated_extractor_reuses_the_active_run_package_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner()
            extractor = self._extractor(root, runner)
            package_path = "/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_IronPipe_1"
            inventory = PackageInventory.create((package_path,), ("pakchunk0-Windows.utoc",))

            isolated = extractor.with_cache(
                CacheManager(root / "isolated-cache"),
                package_inventory=inventory,
            )
            assets = isolated.extract_assets(
                game_root,
                (package_path,),
                game_version="fixture-build",
                data_revision="current",
            )

            self.assertEqual(tuple(asset.virtual_path for asset in assets), (package_path,))
            self.assertFalse(any("list" in command for command in runner.commands))

    # Check large deterministic retoc batches stay
    def test_large_deterministic_retoc_batches_stay_below_the_windows_command_limit(self) -> None:
        requests = [
            (f"/Game/Generated/{index:04d}", PurePosixPath(
                "Map", "MainWorld", f"Generated-{index:04d}-" + "x" * 125,
            ))
            for index in range(420)
        ]

        batches = _asset_batches(requests)

        flattened = tuple(request for batch in batches for request in batch)
        self.assertEqual(flattened, tuple(requests))
        self.assertGreater(len(batches), 1)
        for batch in batches:
            argument_characters = sum(
                len(_retoc_filter_value(relative, extension)) + len("--filter") + 8
                for _package, relative in batch
                for extension in _RETOC_PRIMARY_EXTENSIONS
            )
            self.assertLessEqual(argument_characters, _MAX_RETOC_FILTER_ARGUMENT_CHARACTERS)

    # Check extracts requested package reuses integrity
    def test_extracts_only_requested_package_and_reuses_integrity_checked_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner()
            extractor = self._extractor(root, runner)
            profile_events: list[tuple[str, dict[str, object]]] = []
            extractor.set_extraction_telemetry(
                None,
                None,
                lambda operation, **fields: profile_events.append((operation, fields)),
            )
            virtual_path = "/Game/Blueprint/GameSystem/shop/DT_ShopLineup_DLC"

            first = extractor.extract_assets(
                game_root,
                [virtual_path],
                game_version="test-build",
                data_revision="current",
            )[0]
            second = extractor.extract_assets(
                game_root,
                [virtual_path],
                game_version="test-build",
                data_revision="current",
            )[0]

            self.assertEqual(first, second)
            self.assertEqual(first.primary_file.suffix, ".uasset")
            self.assertEqual({path.suffix for path in first.files}, {".uasset", ".uexp"})
            self.assertEqual(len(first.file_hashes), 2)
            cache_entry = CacheManager(root / "cache").entry_path(
                game_version="test-build", source_hash=archive_source_fingerprint(
                    game_root / "CodeVein2" / "Content" / "Paks"
                ), data_revision="current", create=False,
            )
            self.assertEqual(extractor.cached_asset_inventory(cache_entry), (first,))
            commits = [fields for operation, fields in profile_events if operation == "retoc_output_commit"]
            self.assertEqual(len(commits), 1)
            self.assertEqual(commits[0]["bytes_copied"], 0)
            self.assertEqual(commits[0]["bytes_moved"], commits[0]["bytes_hashed"])
            self.assertEqual(len(runner.commands), 2)  # version check and one extraction
            command = runner.commands[1]
            filters = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--filter"]
            self.assertEqual(
                set(filters),
                {
                    "CodeVein2/Content/Blueprint/GameSystem/shop/DT_ShopLineup_DLC.uasset",
                    "CodeVein2/Content/Blueprint/GameSystem/shop/DT_ShopLineup_DLC.umap",
                },
            )
            self.assertIn("--no-shaders", command)
            self.assertIn("--no-script-objects", command)

    # Check extraction can share stronger prepared
    def test_extraction_can_share_the_stronger_prepared_cache_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            cache = CacheManager(root / "cache")
            extractor = self._extractor(root, FakeRetocRunner())
            extractor.set_cache_source_hash("validated-preparation-identity")

            asset = extractor.extract_assets(
                game_root,
                ["/Game/Blueprint/GameSystem/shop/DT_ShopLineup_DLC"],
                game_version="test-build",
                data_revision="current",
            )[0]

            expected_entry = cache.entry_path(
                game_version="test-build",
                source_hash="validated-preparation-identity",
                data_revision="current",
            )
            archive_entry = cache.entry_path(
                game_version="test-build",
                source_hash=archive_source_fingerprint(game_root / "CodeVein2" / "Content" / "Paks"),
                data_revision="current",
                create=False,
            )
            self.assertIn(expected_entry, asset.primary_file.parents)
            self.assertNotEqual(expected_entry, archive_entry)
            self.assertFalse(archive_entry.exists())

    # Check inspects exact package inventory without
    def test_inspects_exact_package_inventory_without_extracting_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner()
            extractor = self._extractor(root, runner)
            inventory = extractor.inspect_package_inventory(game_root)

            self.assertIn(
                "/game/blueprint/article/dropitem/dt_dropitemdata_dlc",
                inventory.package_paths,
            )
            self.assertEqual(inventory.container_names, frozenset({"pakchunk0-windows.utoc"}))
            self.assertEqual(
                inventory.containers_for("/Game/Blueprint/Article/DropItem/DT_DropItemData_DLC"),
                ("pakchunk0-Windows.utoc",),
            )
            self.assertEqual([command[1] for command in runner.commands[1:]], ["list"])
            self.assertFalse(any("to-legacy" in command for command in runner.commands))

    # Check package inventory index reuses matching
    def test_package_inventory_index_reuses_only_matching_archive_and_retoc_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            cache = CacheManager(root / "cache")
            first_runner = FakeRetocRunner()
            first = self._extractor(root, first_runner).inspect_package_inventory(game_root)

            second_runner = FakeRetocRunner()
            second = IoStoreAssetExtractor(
                executable=root / "retoc.exe",
                cache=cache,
                runner=second_runner,
            ).inspect_package_inventory(game_root)

            self.assertEqual(second.package_paths, first.package_paths)
            self.assertEqual(second.package_path_spellings, first.package_path_spellings)
            self.assertEqual(second.package_containers, first.package_containers)
            self.assertEqual([command[1] for command in first_runner.commands[1:]], ["list"])
            self.assertEqual([command[1] for command in second_runner.commands], ["--version"])

            toc = game_root / "CodeVein2" / "Content" / "Paks" / "pakchunk0-Windows.utoc"
            toc.write_bytes(b"updated toc index")
            third_runner = FakeRetocRunner()
            third = IoStoreAssetExtractor(
                executable=root / "retoc.exe",
                cache=cache,
                runner=third_runner,
            ).inspect_package_inventory(game_root)

            self.assertEqual(third.package_paths, first.package_paths)
            self.assertEqual([command[1] for command in third_runner.commands[1:]], ["list"])

            version_runner = FakeRetocRunner(version="retoc_cli 0.1.6")
            with patch.object(asset_extraction, "RETOC_VERSION", "retoc_cli 0.1.6"):
                fourth = IoStoreAssetExtractor(
                    executable=root / "retoc.exe",
                    cache=cache,
                    runner=version_runner,
                ).inspect_package_inventory(game_root)

            self.assertEqual(fourth.package_paths, first.package_paths)
            self.assertEqual([command[1] for command in version_runner.commands[1:]], ["list"])

    # Check extracts catalog base path unique
    def test_extracts_a_catalog_base_path_from_its_unique_numbered_fname_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            package_base = "/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_IronPipe"
            runner = FakeRetocRunner(
                package_listing=(
                    "Package: /Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_IronPipe_1.uasset\n"
                ),
            )
            extractor = self._extractor(root, runner)
            inventory = extractor.inspect_package_inventory(game_root)

            self.assertTrue(inventory.contains(package_base))
            asset = extractor.extract_assets(
                game_root,
                (package_base,),
                game_version="test-build",
                data_revision="current",
            )[0]

            self.assertEqual(
                asset.virtual_path,
                "/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_IronPipe_1",
            )
            self.assertEqual(asset.primary_file.name, "DA_SingleSword_IronPipe_1.uasset")
            filters = [
                runner.commands[-1][index + 1]
                for index, value in enumerate(runner.commands[-1][:-1])
                if value == "--filter"
            ]
            self.assertEqual(
                set(filters),
                {
                    "CodeVein2/Content/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_IronPipe_1.uasset",
                    "CodeVein2/Content/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_IronPipe_1.umap",
                },
            )

    # Check ambiguous numbered package variant fail
    def test_ambiguous_numbered_package_variant_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner(package_listing=(
                "Package: /Game/Blueprint/Article/Weapon/SingleSword/DA_Sword_1.uasset\n"
                "Package: /Game/Blueprint/Article/Weapon/SingleSword/DA_Sword_2.uasset\n"
            ))
            extractor = self._extractor(root, runner)
            extractor.inspect_package_inventory(game_root)

            with self.assertRaisesRegex(AssetExtractionError, "is ambiguous"):
                extractor.extract_assets(
                    game_root,
                    ("/Game/Blueprint/Article/Weapon/SingleSword/DA_Sword",),
                    game_version="test-build",
                    data_revision="current",
                )

    # Check extracts generated map package
    def test_extracts_a_generated_map_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner()
            extractor = self._extractor(root, runner)
            extracted = extractor.extract_assets(
                game_root,
                ["/Game/Map/MainWorld/MainWorld/_Generated_/008YWFVVR3WYH2OZLKIYC10JN"],
                game_version="test-build",
                data_revision="current",
            )[0]
            self.assertEqual(extracted.primary_file.suffix, ".umap")

    # Check extracts parent map after nested
    def test_extracts_parent_map_after_a_nested_generated_map_without_replacing_cache_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner()
            extractor = self._extractor(root, runner)
            parent_path = "/Game/Map/MainWorld/DlcWorld"
            child_path = "/Game/Map/MainWorld/DlcWorld/_Generated_/GeneratedCell"

            child = extractor.extract_assets(
                game_root,
                [child_path],
                game_version="test-build",
                data_revision="current",
            )[0]
            parent = extractor.extract_assets(
                game_root,
                [parent_path],
                game_version="test-build",
                data_revision="current",
            )[0]
            cached_both = extractor.extract_assets(
                game_root,
                [parent_path, child_path],
                game_version="test-build",
                data_revision="current",
            )

            self.assertEqual(parent.primary_file.name, "DlcWorld.umap")
            self.assertEqual(child.primary_file.name, "GeneratedCell.umap")
            self.assertEqual(len(cached_both), 2)
            self.assertTrue(parent.primary_file.is_file())
            self.assertTrue(child.primary_file.is_file())
            self.assertEqual(len(runner.commands), 3)  # version plus two separate extractions

    # Check extraction preserve case retoc package
    def test_extraction_preserves_case_from_retoc_package_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner(package_listing=(
                "Package: /Game/Map/MainWorld/MainWorld/_Generated_/008YWFVVR3WYH2OZLKIYC10JN.umap\n"
            ))
            extractor = self._extractor(root, runner)
            inventory = extractor.inspect_package_inventory(game_root)
            self.assertEqual(
                inventory.resolve_package_path_spelling(
                    "/Game/Map/MainWorld/MainWorld/_Generated_/008ywfvvr3wyh2ozlkiyc10jn"
                )[0],
                "/Game/Map/MainWorld/MainWorld/_Generated_/008YWFVVR3WYH2OZLKIYC10JN",
            )

            extractor.extract_assets(
                game_root,
                ("/Game/Map/MainWorld/MainWorld/_Generated_/008ywfvvr3wyh2ozlkiyc10jn",),
                game_version="test-build",
                data_revision="current",
            )

            filters = [
                runner.commands[-1][index + 1]
                for index, value in enumerate(runner.commands[-1][:-1])
                if value == "--filter"
            ]
            self.assertEqual(
                set(filters),
                {
                    "CodeVein2/Content/Map/MainWorld/MainWorld/_Generated_/008YWFVVR3WYH2OZLKIYC10JN.uasset",
                    "CodeVein2/Content/Map/MainWorld/MainWorld/_Generated_/008YWFVVR3WYH2OZLKIYC10JN.umap",
                },
            )

    # Check extracts multiple requested packages one
    def test_extracts_multiple_requested_packages_in_one_filtered_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner()
            extractor = self._extractor(root, runner)
            virtual_paths = (
                "/Game/Blueprint/GameSystem/shop/DT_ShopLineup_DLC",
                "/Game/Map/MainWorld/MainWorld/_Generated_/Map001",
            )
            extracted = extractor.extract_assets(
                game_root,
                virtual_paths,
                game_version="test-build",
                data_revision="current",
            )
            self.assertEqual(len(extracted), 2)
            self.assertEqual(len(runner.commands), 2)  # version check and one multi-filter extraction
            filters = [runner.commands[1][index + 1] for index, value in enumerate(runner.commands[1][:-1]) if value == "--filter"]
            self.assertEqual(
                set(filters),
                {
                    "CodeVein2/Content/Blueprint/GameSystem/shop/DT_ShopLineup_DLC.uasset",
                    "CodeVein2/Content/Blueprint/GameSystem/shop/DT_ShopLineup_DLC.umap",
                    "CodeVein2/Content/Map/MainWorld/MainWorld/_Generated_/Map001.uasset",
                    "CodeVein2/Content/Map/MainWorld/MainWorld/_Generated_/Map001.umap",
                },
            )
            again = extractor.extract_assets(game_root, virtual_paths, game_version="test-build", data_revision="current")
            self.assertEqual(extracted, again)
            self.assertEqual(len(runner.commands), 2)

    # Check invalid virtual paths are rejected
    def test_invalid_virtual_paths_are_rejected_before_running_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = FakeRetocRunner()
            extractor = self._extractor(root, runner)
            with self.assertRaises(AssetExtractionError):
                extractor.extract_assets(
                    root / "missing",
                    ["/Game/../../Windows/secret"],
                    game_version="test-build",
                    data_revision="current",
                )
            self.assertEqual(runner.commands, [])

    # Check wrong helper version fail closed
    def test_wrong_helper_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner(version="retoc_cli 9.9.9")
            extractor = self._extractor(root, runner)
            with self.assertRaisesRegex(AssetExtractionError, "Expected retoc_cli 0.1.5"):
                extractor.extract_assets(
                    game_root,
                    ["/Game/Blueprint/GameSystem/shop/DT_ShopLineup_DLC"],
                    game_version="test-build",
                    data_revision="current",
                )

    # Check missing exact requested package is
    def test_missing_exact_requested_package_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner(produce_requested=False)
            extractor = self._extractor(root, runner)
            with self.assertRaisesRegex(AssetExtractionError, "omitted requested packages from the filtered batch"):
                extractor.extract_assets(
                    game_root,
                    ["/Game/Blueprint/GameSystem/shop/DT_ShopLineup_DLC"],
                    game_version="test-build",
                    data_revision="current",
                )

    # Check missing catalogued optional package is
    def test_missing_catalogued_optional_package_is_omitted_without_failing_required_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = _installation(root)
            runner = FakeRetocRunner(missing_filters={"DT_ShopLineup_DLC"})
            extractor = self._extractor(root, runner)
            extracted = extractor.extract_assets(
                game_root,
                (
                    "/Game/Blueprint/GameSystem/shop/DT_ShopLineup",
                    "/Game/Blueprint/GameSystem/shop/DT_ShopLineup_DLC",
                ),
                optional_virtual_paths=("/Game/Blueprint/GameSystem/shop/DT_ShopLineup_DLC",),
                game_version="test-build",
                data_revision="current",
            )
            self.assertEqual([asset.virtual_path for asset in extracted], ["/Game/Blueprint/GameSystem/shop/DT_ShopLineup"])

    # Check optional paths must be requested
    def test_optional_paths_must_be_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = FakeRetocRunner()
            extractor = self._extractor(root, runner)
            with self.assertRaisesRegex(AssetExtractionError, "also appear"):
                extractor.extract_assets(
                    root / "missing",
                    ("/Game/Blueprint/GameSystem/shop/DT_ShopLineup",),
                    optional_virtual_paths=("/Game/Blueprint/GameSystem/shop/DT_ShopLineup_DLC",),
                    game_version="test-build",
                    data_revision="current",
                )
            self.assertEqual(runner.commands, [])

    # Check archive fingerprint changes container changes
    def test_archive_fingerprint_changes_when_a_container_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pak_dir = Path(temporary)
            toc = pak_dir / "pakchunk.utoc"
            toc.write_bytes(b"first toc")
            first = archive_source_fingerprint(pak_dir)
            toc.write_bytes(b"other toc")
            second = archive_source_fingerprint(pak_dir)
            self.assertNotEqual(first, second)

    # Check materializes packages content layout without
    def test_materializes_packages_in_content_layout_without_replacing_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache_dir = root / "cache" / "assets" / "Map" / "Generated" / "Sample"
            cache_dir.mkdir(parents=True)
            header = cache_dir / "Sample.umap"
            sidecar = cache_dir / "Sample.uexp"
            header.write_bytes(b"map header")
            sidecar.write_bytes(b"export bytes")
            asset = ExtractedAsset("/Game/Map/Generated/Sample", (header, sidecar))
            content = root / "staging" / "Content"

            result = materialize_extracted_assets((asset,), content)[0]
            self.assertEqual(result.primary_file, content / "Map" / "Generated" / "Sample.umap")
            self.assertEqual((content / "Map" / "Generated" / "Sample.uexp").read_bytes(), b"export bytes")
            self.assertEqual(header.read_bytes(), b"map header")
            self.assertEqual(materialize_extracted_assets((asset,), content), (result,))

            (content / "Map" / "Generated" / "Sample.uexp").write_bytes(b"conflict")
            with self.assertRaisesRegex(AssetExtractionError, "Refusing to overwrite conflicting"):
                materialize_extracted_assets((asset,), content)
            self.assertEqual((content / "Map" / "Generated" / "Sample.uexp").read_bytes(), b"conflict")


if __name__ == "__main__":
    unittest.main()
