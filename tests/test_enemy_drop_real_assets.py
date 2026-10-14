from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.asset_extraction import MaterializedAsset
from cv2randomizer.game.enemy_drop_catalog import EnemyDropCatalogStore
from cv2randomizer.game.enemy_drop_generation import (
    ENEMY_DROP_TABLES,
    EnemyDropPreparation,
    EnemyDropSerializer,
    apply_enemy_drop_plan,
)
from cv2randomizer.packaging.retoc import RetocPackager
from cv2randomizer.randomizer.enemy_drops import build_enemy_drop_plan


SOURCE_CONTENT = os.environ.get("CV2_ENEMY_DROP_SOURCE_CONTENT")
MAPPINGS_FILE = os.environ.get("CV2_USMAP_PATH")


@unittest.skipUnless(
    SOURCE_CONTENT and MAPPINGS_FILE,
    "set CV2_ENEMY_DROP_SOURCE_CONTENT and CV2_USMAP_PATH for the real-package integration test",
)
class EnemyDropRealAssetIntegrationTests(unittest.TestCase):
    # Check reads patches reopens packages all
    def test_reads_patches_reopens_and_packages_all_three_data_tables(self) -> None:
        source = Path(SOURCE_CONTENT).expanduser().resolve(strict=True)
        mappings = Path(MAPPINGS_FILE).expanduser().resolve(strict=True)
        catalog = EnemyDropCatalogStore().load("current")
        source_files = {
            source / f"{Path(virtual).name}{suffix}"
            for _table, virtual in ENEMY_DROP_TABLES
            for suffix in (".uasset", ".uexp")
        }
        missing = sorted(path.name for path in source_files if not path.is_file())
        if missing:
            self.fail(f"real Enemy Drop integration is missing extracted package files: {missing}")
        original_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}

        with tempfile.TemporaryDirectory(prefix="cv2-enemy-drop-integration-") as temporary:
            root = Path(temporary).resolve()
            source_content = root / "source" / "Content"
            assets: list[MaterializedAsset] = []
            table_files: dict[str, Path] = {}
            for table, virtual in ENEMY_DROP_TABLES:
                relative = Path(*virtual.split("/")[2:])
                package_dir = source_content / relative.parent
                package_dir.mkdir(parents=True, exist_ok=True)
                files = []
                for suffix in (".uasset", ".uexp"):
                    copied = package_dir / f"{relative.name}{suffix}"
                    shutil.copyfile(source / copied.name, copied)
                    files.append(copied)
                asset = MaterializedAsset(virtual, tuple(files))
                assets.append(asset)
                table_files[table] = asset.primary_file

            serializer = EnemyDropSerializer()
            snapshots = serializer.inspect_tables(
                work_root=root,
                mappings_path=mappings,
                table_files=table_files,
            )
            plan = build_enemy_drop_plan(snapshots, catalog, "real-enemy-drop-integration")
            self.assertEqual(plan.eligible_rows, 201)
            self.assertEqual(plan.eligible_slots, 1311)
            self.assertTrue(plan.reward_multiset_preserved)
            self.assertTrue(plan.excluded_rows_preserved)
            self.assertTrue(plan.weights_preserved)

            generated = apply_enemy_drop_plan(
                EnemyDropPreparation(plan, catalog, tuple(assets), snapshots),
                work_root=root,
                output_content_root=root / "staging" / "Content",
                mappings_path=mappings,
                serializer=serializer,
            )
            self.assertTrue(generated.reopened)
            self.assertEqual(set(generated.patched_tables), {table for table, _ in ENEMY_DROP_TABLES})
            self.assertEqual(len(generated.output_files), 6)
            package = RetocPackager().package(
                root / "staging" / "Content",
                work_root=root,
                output_directory=root / "packaged",
                mod_name="CV2EnemyDropsFixture",
            )
            self.assertEqual({path.suffix for path in package.files}, {".pak", ".utoc", ".ucas"})
            self.assertEqual(
                set(package.package_paths),
                {
                    "../../../CodeVein2/Content/Blueprint/Article/DropItem/DT_DropItemData.uasset",
                    "../../../CodeVein2/Content/Blueprint/Article/DropItem/DT_DropItemData_AfterEnding.uasset",
                    "../../../CodeVein2/Content/Blueprint/Article/DropItem/DT_DropItemData_DLC.uasset",
                },
            )

        self.assertEqual(
            {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files},
            original_hashes,
            "source extracted assets must remain unchanged",
        )


if __name__ == "__main__":
    unittest.main()
