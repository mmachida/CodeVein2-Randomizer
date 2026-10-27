from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cv2randomizer.game.game_data_cache import (
    _read_prepared_cached_asset,
    _valid_world_loot_binary_descriptor,
    _validate_prepared_world_asset,
)
from cv2randomizer.game.world_loot_dependencies import WorldLootDependencyResolver


class PreparedWorldLootCacheTests(unittest.TestCase):
    # Check prepared package is stored manifest
    def test_prepared_package_is_stored_with_manifest_and_rejected_when_corrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_root = root / "work"
            input_root = work_root / "source" / "Content" / "Map" / "MainWorld"
            output_root = work_root / ".cv2-prepared-world-loot" / "Map" / "MainWorld"
            input_root.mkdir(parents=True)
            output_root.mkdir(parents=True)
            input_header = input_root / "Cell.umap"
            input_uexp = input_root / "Cell.uexp"
            input_header.write_bytes(b"vanilla header")
            input_uexp.write_bytes(b"vanilla exports")
            output_header = output_root / "Cell.umap"
            output_uexp = output_root / "Cell.uexp"
            output_header.write_bytes(b"prepared header")
            output_uexp.write_bytes(b"vanilla exports")
            package_path = "/Game/Map/MainWorld/Cell"
            request = {"packagePath": package_path, "inputFile": str(input_header)}

            prepared = WorldLootDependencyResolver._store_prepared_world_package(
                work_root,
                root / "cache" / "prepared-assets",
                request,
                {"outputFiles": [str(output_header), str(output_uexp)]},
            )
            rows = {Path(row["name"]).suffix: row for row in prepared["files"]}
            descriptor = {
                "version": 3,
                "package_path": package_path,
                "header_sha256": rows[".umap"]["sha256"],
                "uexp_sha256": rows[".uexp"]["sha256"],
                "header_size": rows[".umap"]["size"],
                "uexp_size": rows[".uexp"]["size"],
                "package_file_hashes": {
                    ".umap": rows[".umap"]["sha256"],
                    ".uexp": rows[".uexp"]["sha256"],
                },
                "name_map_indices": {},
                "name_map_count_before": 0,
                "name_map_count_after": 0,
                "names_added_count": 0,
                "fields": [],
                "prepared_asset": prepared,
            }

            self.assertTrue(_valid_world_loot_binary_descriptor(package_path.casefold(), descriptor))
            self.assertTrue(_validate_prepared_world_asset(root / "cache", descriptor, verify_hash=True))
            cached = _read_prepared_cached_asset(
                root / "cache", package_path, Path("Map/MainWorld/Cell"), verify_hash=True,
            )
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual({path.suffix for path in cached.files}, {".umap", ".uexp"})

            cached.primary_file.write_bytes(b"tampered header")
            self.assertFalse(_validate_prepared_world_asset(root / "cache", descriptor, verify_hash=True))
            self.assertFalse(_valid_world_loot_binary_descriptor(package_path.casefold(), {
                **descriptor,
                "prepared_asset": {**prepared, "relative_path": "../escape"},
            }))

    # Retry transient cache publication locks
    def test_prepared_package_retries_a_transient_rename_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_root = root / "work"
            input_root = work_root / "source" / "Content" / "Map"
            output_root = work_root / ".cv2-prepared-world-loot" / "Map"
            input_root.mkdir(parents=True)
            output_root.mkdir(parents=True)
            input_header = input_root / "Cell.umap"
            input_uexp = input_root / "Cell.uexp"
            input_header.write_bytes(b"vanilla header")
            input_uexp.write_bytes(b"vanilla exports")
            output_header = output_root / "Cell.umap"
            output_uexp = output_root / "Cell.uexp"
            output_header.write_bytes(b"prepared header")
            output_uexp.write_bytes(b"vanilla exports")

            original_replace = Path.replace
            replace_calls = 0

            def replace_once(source: Path, target: Path) -> Path:
                nonlocal replace_calls
                if source.name.endswith(".tmp") and replace_calls == 0:
                    replace_calls += 1
                    raise PermissionError(5, "Access is denied", str(source))
                replace_calls += 1
                return original_replace(source, target)

            with (
                patch("cv2randomizer.game.world_loot_dependencies.Path.replace", new=replace_once),
                patch("cv2randomizer.game.world_loot_dependencies.time.sleep"),
            ):
                prepared = WorldLootDependencyResolver._store_prepared_world_package(
                    work_root,
                    root / "cache" / "prepared-assets",
                    {"packagePath": "/Game/Map/Cell", "inputFile": str(input_header)},
                    {"outputFiles": [str(output_header), str(output_uexp)]},
                )

            self.assertEqual(replace_calls, 2)
            self.assertEqual(prepared["relative_path"], "Map/Cell")
            self.assertTrue((root / "cache" / "prepared-assets" / "Map" / "Cell" / ".cv2randomizer-prepared-asset.json").is_file())


if __name__ == "__main__":
    unittest.main()
