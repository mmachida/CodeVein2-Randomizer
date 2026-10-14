from __future__ import annotations

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.fname_identity import FNameIdentity, SoftObjectPathIdentity
from cv2randomizer.game.world_loot_binary_patch import (
    binary_name_map_fallback_reason,
    preflight_binary_package,
    write_binary_package,
)
from cv2randomizer.game.world_loot_serialization import ArticleReward, WorldLootTuplePatch


class WorldLootBinaryPatchTests(unittest.TestCase):
    # Read fixture
    def _fixture(self, root: Path):
        cache = root / "cache" / "Map" / "Generated"
        cache.mkdir(parents=True)
        header = cache / "Map001.umap"
        sidecar = cache / "Map001.uexp"
        ubulk = cache / "Map001.ubulk"
        header.write_bytes(b"synthetic immutable umap header")
        original_path = struct.pack("<iiii", 2, 0, 3, 0)
        source_payload = b"prefix!" + original_path + b"tail data unchanged"
        sidecar.write_bytes(source_payload)
        ubulk.write_bytes(b"immutable bulk data")
        original_identity = SoftObjectPathIdentity.from_literal_path("/Game/Item/DA_A")
        assigned_identity = SoftObjectPathIdentity(
            FNameIdentity("/Game/Item/DA_B", 3), FNameIdentity("DA_B", 3),
        )
        patch = WorldLootTuplePatch(
            export_index=5,
            reward_index=0,
            export_name="BP_Pickup_C_UAID_test",
            expected_original=ArticleReward("/Game/Item/DA_A", 1, True, original_identity),
            assigned=ArticleReward("/Game/Item/DA_B_2", 1, True, assigned_identity),
        )
        descriptor = {
            "version": 3,
            "package_path": "/Game/Map/Generated/Map001",
            "header_sha256": hashlib.sha256(header.read_bytes()).hexdigest(),
            "uexp_sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            "header_size": header.stat().st_size,
            "uexp_size": sidecar.stat().st_size,
            "package_file_hashes": {
                ".umap": hashlib.sha256(header.read_bytes()).hexdigest(),
                ".uexp": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                ".ubulk": hashlib.sha256(ubulk.read_bytes()).hexdigest(),
            },
            "name_map_indices": {
                "/Game/Item/DA_A": 2,
                "DA_A": 3,
                "/Game/Item/DA_B": 7,
                "DA_B": 9,
            },
            "name_map_count_before": 10,
            "name_map_count_after": 12,
            "names_added_count": 2,
            "fields": [{
                "export_index": 5,
                "reward_index": 0,
                "export_name": "BP_Pickup_C_UAID_test",
                "offset": 7,
                "expected_bytes_hex": original_path.hex(),
                "expected_identity": {
                    "package_name": {"value": "/Game/Item/DA_A", "number": 0},
                    "asset_name": {"value": "DA_A", "number": 0},
                },
                "available": True,
                "unavailable_reason": None,
            }],
        }
        asset = ExtractedAsset("/Game/Map/Generated/Map001", (header, sidecar, ubulk))
        return asset, descriptor, patch, source_payload

    # Check preflight write preserve every byte
    def test_preflight_and_write_preserve_every_byte_outside_the_fname_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "work").mkdir()
            asset, descriptor, patch, before = self._fixture(root)
            plan, reason = preflight_binary_package(
                asset.virtual_path, asset, descriptor, (patch,),
            )
            self.assertIsNone(reason)
            self.assertIsNotNone(plan)
            assert plan is not None
            output_root = root / "work" / "output" / "Content"
            result = write_binary_package(
                plan, work_root=root / "work", output_content_root=output_root,
            )
            output_header = output_root / "Map" / "Generated" / "Map001.umap"
            output_sidecar = output_header.with_suffix(".uexp")
            after = output_sidecar.read_bytes()
            expected = struct.pack("<iiii", 7, 3, 9, 3)
            self.assertEqual(len(before), len(after))
            self.assertEqual(after[:7], before[:7])
            self.assertEqual(after[7:23], expected)
            self.assertEqual(after[23:], before[23:])
            self.assertEqual(output_header.read_bytes(), asset.primary_file.read_bytes())
            self.assertEqual(output_header.with_suffix(".ubulk").read_bytes(), b"immutable bulk data")
            self.assertEqual(result.output_path, output_header)
            self.assertEqual(result.patches[0].assigned, patch.assigned)

    # Check missing assigned name require structural
    def test_missing_assigned_name_requires_structural_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset, descriptor, patch, _before = self._fixture(Path(temporary))
            del descriptor["name_map_indices"]["DA_B"]
            reason = binary_name_map_fallback_reason(asset.virtual_path, descriptor, (patch,))
            self.assertEqual(reason, "assigned ArticleDataPath requires a NameMap entry")
            plan, reason = preflight_binary_package(asset.virtual_path, asset, descriptor, (patch,))
            self.assertIsNone(plan)
            self.assertIn("NameMap", reason or "")

    # Check binary preflight does not rehash
    def test_binary_preflight_does_not_rehash_ready_cache_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset, descriptor, patch, _before = self._fixture(Path(temporary))
            # READY cache members are validated when Extract commits the cache.
            # Generate still checks sizes and exact patch bytes but avoids
            # rereading every immutable member to recompute its SHA-256.
            asset.primary_file.write_bytes(b"x" * asset.primary_file.stat().st_size)
            plan, reason = preflight_binary_package(asset.virtual_path, asset, descriptor, (patch,))
            self.assertIsNotNone(plan)
            self.assertIsNone(reason)

    # Check unexpected vanilla bytes require structural
    def test_unexpected_vanilla_bytes_require_structural_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset, descriptor, patch, _before = self._fixture(Path(temporary))
            asset.primary_file.with_suffix(".uexp").write_bytes(b"unexpected" + b"x" * 40)
            descriptor["uexp_size"] = asset.primary_file.with_suffix(".uexp").stat().st_size
            descriptor["uexp_sha256"] = hashlib.sha256(asset.primary_file.with_suffix(".uexp").read_bytes()).hexdigest()
            descriptor["package_file_hashes"][".uexp"] = descriptor["uexp_sha256"]
            plan, reason = preflight_binary_package(asset.virtual_path, asset, descriptor, (patch,))
            self.assertIsNone(plan)
            self.assertIn("expected vanilla bytes", reason or "")

    # Check overlapping ranges require structural fallback
    def test_overlapping_ranges_require_structural_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            asset, descriptor, patch, _before = self._fixture(Path(temporary))
            second = dict(descriptor["fields"][0])
            second["export_index"] = 6
            second["export_name"] = "Other_C"
            descriptor["fields"].append(second)
            other = WorldLootTuplePatch(
                6, 0, "Other_C", patch.expected_original, patch.assigned,
            )
            plan, reason = preflight_binary_package(asset.virtual_path, asset, descriptor, (patch, other))
            self.assertIsNone(plan)
            self.assertIn("overlap", reason or "")


if __name__ == "__main__":
    unittest.main()
