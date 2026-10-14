from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.enemy_drop_catalog import (
    EnemyDropCatalogError,
    EnemyDropCatalogStore,
)


ROOT = Path(__file__).resolve().parents[1]


class EnemyDropCatalogTests(unittest.TestCase):
    # Check load confirmed revision exact v1
    def test_loads_confirmed_revision_and_exact_v1_counts(self) -> None:
        catalog = EnemyDropCatalogStore(ROOT / "data" / "enemy_drops").load("current")
        self.assertEqual(len(catalog.rows), 233)
        counts = {}
        for row in catalog.rows:
            counts[row.classification] = counts.get(row.classification, 0) + 1
        self.assertEqual(counts, {
            "NORMAL_ONLY": 189,
            "SHARED_BOSS_NORMAL": 12,
            "UNRESOLVED": 32,
        })

    # Check reject wrong revision modified csv
    def test_rejects_wrong_revision_and_modified_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "data" / "enemy_drops", root, dirs_exist_ok=True)
            store = EnemyDropCatalogStore(root)
            with self.assertRaisesRegex(EnemyDropCatalogError, "bound to revision"):
                store.load("stale-version")
            catalog_path = root / "enemy_drops_pool_v1.csv"
            catalog_path.write_bytes(catalog_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(EnemyDropCatalogError, "hash"):
                store.load("current")

    # Check reject duplicate rows even manifest
    def test_rejects_duplicate_rows_even_if_manifest_is_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "data" / "enemy_drops", root, dirs_exist_ok=True)
            catalog_path = root / "enemy_drops_pool_v1.csv"
            with catalog_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[-1] = dict(rows[0])
            with catalog_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            import hashlib

            manifest["files"][catalog_path.name]["sha256"] = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(EnemyDropCatalogError, "duplicate"):
                EnemyDropCatalogStore(root).load("current")


if __name__ == "__main__":
    unittest.main()
