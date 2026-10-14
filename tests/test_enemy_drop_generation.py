from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.core.workspace import WorkspaceManager
from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.enemy_drop_catalog import EnemyDropCatalogStore
from cv2randomizer.game.enemy_drop_generation import (
    ENEMY_DROP_TABLES,
    apply_enemy_drop_plan,
    prepare_enemy_drop_plan,
)
from cv2randomizer.randomizer.enemy_drops import (
    EnemyDropRowSnapshot,
    EnemyDropSlotSnapshot,
    EnemyDropTableSnapshot,
)
from cv2randomizer.game.versioning import VersionDetection
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.generation.pipeline import GenerationEngine, GenerationRequest
from cv2randomizer.packaging.retoc import PackageResult


ROOT = Path(__file__).resolve().parents[1]
CORE_PACKAGE = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"


# Calculate the source fingerprint
def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _FakeExtractor:
    # Initialize this object
    def __init__(self, root: Path) -> None:
        self.root = root
        self.requested = ()

    # Inspect package inventory
    def inspect_package_inventory(self, _game_root):
        return PackageInventory.create((CORE_PACKAGE, *(package for _table, package in ENEMY_DROP_TABLES[:2])))

    # Extract requested game assets
    def extract_assets(self, game_root, virtual_paths, **_kwargs):
        self.requested = tuple(virtual_paths)
        results = []
        for virtual in self.requested:
            relative = Path(*virtual.split("/")[2:])
            package = self.root / relative.parent
            package.mkdir(parents=True, exist_ok=True)
            header = package / f"{relative.name}.uasset"
            sidecar = package / f"{relative.name}.uexp"
            header.write_bytes(b"source header")
            sidecar.write_bytes(b"source exports")
            results.append(ExtractedAsset(virtual, (header, sidecar)))
        return tuple(results)


class _FakeSerializer:
    # Initialize this object
    def __init__(self, catalog, *, uniform_bundles: bool = False) -> None:
        self.catalog = catalog
        self.uniform_bundles = uniform_bundles
        self.snapshots = self._make_snapshots()
        self.patch_requests = None

    # Make snapshots
    def _make_snapshots(self):
        by_table = {table: [] for table, _ in ENEMY_DROP_TABLES}
        for row in self.catalog.rows:
            slots = tuple(
                EnemyDropSlotSnapshot(
                    row.table,
                    row.row_id,
                    index,
                    _fingerprint(f"weight:{row.table}:{row.row_id}:{index}"),
                    _fingerprint(f"other:{row.table}:{row.row_id}:{index}"),
                    _fingerprint("same-bundle" if self.uniform_bundles else f"bundle:{row.table}:{row.row_id}:{index}"),
                )
                for index in range(row.expected_slots)
            )
            by_table[row.table].append(EnemyDropRowSnapshot(row.table, row.row_id, slots))
        return tuple(EnemyDropTableSnapshot(table, tuple(by_table[table])) for table, _ in ENEMY_DROP_TABLES)

    # Inspect tables
    def inspect_tables(self, *, table_files, **_kwargs):
        requested = set(table_files)
        return tuple(snapshot for snapshot in self.snapshots if snapshot.table in requested)

    # Patch tables
    def patch_tables(self, *, patch_tables, **_kwargs):
        self.patch_requests = patch_tables
        results = []
        source_slots = {
            (table.table, row.row_id.casefold(), slot.slot_index): slot
            for table in self.snapshots
            for row in table.rows
            for slot in row.slots
        }
        for table, (source_path, output_path, assignments) in patch_tables.items():
            original = next(snapshot for snapshot in self.snapshots if snapshot.table == table)
            assigned_fingerprints = {
                (item["destinationRowId"].casefold(), item["destinationSlotIndex"]): source_slots[
                    (item["sourceTable"], item["sourceRowId"].casefold(), item["sourceSlotIndex"])
                ].bundle_fingerprint
                for item in assignments
            }
            rows_json = []
            for row in original.rows:
                slots_json = []
                for slot in row.slots:
                    slots_json.append({
                        "slotIndex": slot.slot_index,
                        "weightFingerprint": slot.weight_fingerprint,
                        "nonRewardFingerprint": slot.non_reward_fingerprint,
                        "bundleFingerprint": assigned_fingerprints.get(
                            (row.row_id.casefold(), slot.slot_index), slot.bundle_fingerprint
                        ),
                    })
                rows_json.append({"rowId": row.row_id, "slotCount": len(slots_json), "slots": slots_json})
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"patched header")
            sidecar = output_path.with_suffix(".uexp")
            sidecar.write_bytes(b"patched exports")
            results.append({
                "table": table,
                "inputFile": str(source_path),
                "outputFile": str(output_path),
                "rowCount": len(rows_json),
                "slotCount": sum(len(row["slots"]) for row in rows_json),
                "rows": rows_json,
                "outputFiles": [str(output_path), str(sidecar)],
            })
        return results


class _FakeVersionDetector:
    # Detect the installed game version
    def detect(self, game_root: Path) -> VersionDetection:
        return VersionDetection(
            True, "fixture-build", "current", game_root / "CodeVein2.exe", "fixture", (CORE_PACKAGE,),
        )


class _FakePackager:
    # Resolve the package path
    def package(self, content_root: Path, *, work_root: Path, output_directory: Path,
                input_files: tuple[Path, ...] | None = None, game_root: Path | None = None) -> PackageResult:
        output_directory.mkdir(parents=True, exist_ok=True)
        files = tuple(output_directory / f"CV2Randomizer_P{suffix}" for suffix in (".pak", ".ucas", ".utoc"))
        for path in files:
            path.write_bytes(b"verified test container")
        package_paths = tuple(
            "../../../CodeVein2/Content/" + path.relative_to(content_root).as_posix()
            for path in sorted(content_root.rglob("*.uasset"))
        )
        return PackageResult("CV2Randomizer_P", files, package_paths)


class EnemyDropGenerationTests(unittest.TestCase):
    # Check missing optional table keep base
    def test_missing_optional_table_keeps_base_and_after_ending_drop_tables(self) -> None:
        catalog = EnemyDropCatalogStore(ROOT / "data" / "enemy_drops").load("current")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_root = root / "generation"
            work_root.mkdir()
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            extractor = _FakeExtractor(root / "cache")
            serializer = _FakeSerializer(catalog)
            preparation = prepare_enemy_drop_plan(
                catalog,
                "enemy-drop-no-dlc",
                game_root=root / "game",
                work_root=work_root,
                mappings_path=mappings,
                game_version="fixture-build",
                data_revision="current",
                extractor=extractor,
                serializer=serializer,
                active_tables=("BASE", "AFTER_ENDING"),
            )
            self.assertEqual(extractor.requested, tuple(path for _table, path in ENEMY_DROP_TABLES[:2]))
            self.assertEqual(len(preparation.source_tables), 2)
            self.assertEqual({table.table for table in preparation.original_snapshots}, {"BASE", "AFTER_ENDING"})

    # Check extracts plans stages changed validated
    def test_extracts_plans_and_stages_only_changed_validated_tables(self) -> None:
        catalog = EnemyDropCatalogStore(ROOT / "data" / "enemy_drops").load("current")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_root = root / "generation"
            work_root.mkdir()
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            extractor = _FakeExtractor(root / "cache")
            serializer = _FakeSerializer(catalog)
            preparation = prepare_enemy_drop_plan(
                catalog,
                "enemy-drop-generation-test",
                game_root=root / "game",
                work_root=work_root,
                mappings_path=mappings,
                game_version="fixture-build",
                data_revision="current",
                extractor=extractor,
                serializer=serializer,
            )
            self.assertEqual(extractor.requested, tuple(path for _table, path in ENEMY_DROP_TABLES))
            self.assertEqual(preparation.plan.eligible_rows, 201)
            self.assertEqual(preparation.plan.eligible_slots, 1311)
            self.assertTrue(preparation.plan.reward_multiset_preserved)

            result = apply_enemy_drop_plan(
                preparation,
                work_root=work_root,
                output_content_root=work_root / "staging" / "Content",
                mappings_path=mappings,
                serializer=serializer,
            )
            self.assertTrue(result.reopened)
            self.assertEqual(set(result.patched_tables), set(serializer.patch_requests))
            self.assertTrue(result.output_files)
            self.assertEqual(
                {path.suffix for path in result.output_files},
                {".uasset", ".uexp"},
            )
            self.assertEqual(
                {path.name for path in result.output_files if path.suffix == ".uasset"},
                {f"{Path(virtual).name}.uasset" for _table, virtual in ENEMY_DROP_TABLES if _table in result.patched_tables},
            )

    # Check no changed bundles produces no
    def test_no_changed_bundles_produces_no_patch_outputs(self) -> None:
        catalog = EnemyDropCatalogStore(ROOT / "data" / "enemy_drops").load("current")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_root = root / "generation"
            work_root.mkdir()
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            extractor = _FakeExtractor(root / "cache")
            serializer = _FakeSerializer(catalog, uniform_bundles=True)
            preparation = prepare_enemy_drop_plan(
                catalog,
                "same-bundle-test",
                game_root=root / "game",
                work_root=work_root,
                mappings_path=mappings,
                game_version="fixture-build",
                data_revision="current",
                extractor=extractor,
                serializer=serializer,
            )
            result = apply_enemy_drop_plan(
                preparation,
                work_root=work_root,
                output_content_root=work_root / "staging" / "Content",
                mappings_path=mappings,
                serializer=serializer,
            )
            self.assertEqual(result.output_files, ())
            self.assertEqual(serializer.patch_requests, None)

    # Check enemy drop option runs through
    def test_enemy_drop_option_runs_through_manifest_and_packaging(self) -> None:
        catalog = EnemyDropCatalogStore(ROOT / "data" / "enemy_drops").load("current")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            serializer = _FakeSerializer(catalog)
            engine = GenerationEngine(
                version_detector=_FakeVersionDetector(),
                catalog_store=CatalogStore(ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "workspaces"),
                extractor=_FakeExtractor(root / "cache"),
                enemy_drop_serializer=serializer,
                packager=_FakePackager(),
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_materials=False,
                randomize_shops=False,
                randomize_enemy_drops=True,
                install_after_generation=False,
            )
            result = engine.generate(GenerationRequest(root / "game", mappings, "enemy-pipeline", options))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["enemy_drops"]["eligible_rows"], 133)
            self.assertEqual(manifest["counts"]["enemy_drops"]["eligible_slots"], 789)
            self.assertEqual(manifest["counts"]["enemy_drops"]["active_tables"], ["BASE", "AFTER_ENDING"])
            self.assertNotIn("DLC", manifest["counts"]["enemy_drops"]["patched_tables"])
            self.assertTrue(manifest["counts"]["enemy_drops"]["reopened"])
            self.assertEqual(len(result.output_files), 4)
            self.assertEqual(len(result.package_result.files), 3)
            self.assertIsNone(result.installation_result)


if __name__ == "__main__":
    unittest.main()
