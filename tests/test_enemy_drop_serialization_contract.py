from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cv2randomizer.game.enemy_drop_generation import (
    ENEMY_DROP_TABLES,
    EnemyDropGenerationError,
    EnemyDropSerializer,
)


class _HelperRunner:
    # Initialize this object
    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.documents: list[dict[str, Any]] = []

    # Call the wrapped operation
    def __call__(self, command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        manifest = Path(command[-1])
        document = json.loads(manifest.read_text(encoding="utf-8"))
        self.documents.append(document)
        if self.returncode:
            return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)
        if command[-5] == "inspect-enemy-drops":
            output = [
                {"table": item["table"], "inputFile": item["inputFile"], "rowCount": 0, "slotCount": 0, "rows": []}
                for item in document["tables"]
            ]
        else:
            output = []
        return subprocess.CompletedProcess(command, 0, json.dumps(output) + "\n", "")


class EnemyDropSerializationContractTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "generation"
        self.root.mkdir()
        self.mappings = Path(self.temporary.name) / "CV2.usmap"
        self.mappings.write_bytes(b"fixture mapping")
        self.helper = Path(self.temporary.name) / "helper.csproj"
        self.helper.write_text("fixture project", encoding="utf-8")
        self.packages: dict[str, Path] = {}
        for table, virtual in ENEMY_DROP_TABLES:
            header = self.root / Path(*virtual.split("/")[2:]).with_suffix(".uasset")
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_bytes(b"fixture uasset")
            header.with_suffix(".uexp").write_bytes(b"fixture exports")
            self.packages[table] = header

    # Read serializer
    def _serializer(self, runner: _HelperRunner) -> EnemyDropSerializer:
        return EnemyDropSerializer(helper_project=self.helper, runner=runner)

    # Check inspection accept every available optional
    def test_inspection_accepts_every_available_optional_table_subset(self) -> None:
        for names in (
            ("BASE",),
            ("BASE", "AFTER_ENDING"),
            ("BASE", "DLC"),
            ("BASE", "AFTER_ENDING", "DLC"),
        ):
            with self.subTest(names=names):
                runner = _HelperRunner()
                snapshots = self._serializer(runner).inspect_tables(
                    work_root=self.root,
                    mappings_path=self.mappings,
                    table_files={name: self.packages[name] for name in names},
                )
                self.assertEqual(tuple(snapshot.table for snapshot in snapshots), names)

    # Check inspection rejects noncanonical subsets
    def test_inspection_rejects_noncanonical_tables_before_helper(self) -> None:
        for names in (("AFTER_ENDING", "BASE"), ("BASE", "DLC", "AFTER_ENDING")):
            with self.subTest(names=names):
                runner = _HelperRunner()
                with self.assertRaises(EnemyDropGenerationError):
                    self._serializer(runner).inspect_tables(
                        work_root=self.root,
                        mappings_path=self.mappings,
                        table_files={name: self.packages[name] for name in names},
                    )
                self.assertEqual(runner.documents, [])

    # Check optional destination table can read
    def test_optional_destination_table_can_read_from_base_and_dlc_source_pool(self) -> None:
        runner = _HelperRunner()
        destination = self.root / "staging" / "DT_DropItemData_DLC.uasset"
        assignments = ({
            "destinationRowId": "Enemy_DLC",
            "destinationSlotIndex": 0,
            "sourceTable": "BASE",
            "sourceRowId": "Enemy_Base",
            "sourceSlotIndex": 1,
            "expectedDestinationFingerprint": hashlib.sha256(b"destination").hexdigest(),
            "expectedSourceFingerprint": hashlib.sha256(b"source").hexdigest(),
        },)
        self._serializer(runner).patch_tables(
            work_root=self.root,
            mappings_path=self.mappings,
            patch_tables={"DLC": (self.packages["DLC"], destination, assignments)},
            source_tables={"BASE": self.packages["BASE"], "DLC": self.packages["DLC"]},
        )
        self.assertEqual(runner.documents[0]["tables"][0]["table"], "DLC")
        self.assertEqual([item["table"] for item in runner.documents[0]["sourceTables"]], ["BASE", "DLC"])

    # Check patch request failure writes path
    def test_patch_request_failure_writes_path_sanitized_repro_json(self) -> None:
        runner = _HelperRunner(returncode=1, stderr="NullReferenceException in fixture")
        destination = self.root / "staging" / "DT_DropItemData.uasset"
        fingerprint = hashlib.sha256(b"reward").hexdigest()
        assignments = ({
            "destinationRowId": "Enemy_Base",
            "destinationSlotIndex": 0,
            "sourceTable": "BASE",
            "sourceRowId": "Enemy_Base",
            "sourceSlotIndex": 1,
            "expectedDestinationFingerprint": fingerprint,
            "expectedSourceFingerprint": fingerprint,
        },)
        with self.assertRaisesRegex(EnemyDropGenerationError, "NullReferenceException"):
            self._serializer(runner).patch_tables(
                work_root=self.root,
                mappings_path=self.mappings,
                patch_tables={"BASE": (self.packages["BASE"], destination, assignments)},
                source_tables={"BASE": self.packages["BASE"]},
            )
        reports = tuple((self.root / "logs").glob("enemy-drop-failure-*.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(report["request"]["tables"][0]["inputFile"], "Blueprint/Article/DropItem/DT_DropItemData.uasset")
        self.assertNotIn(str(self.root), reports[0].read_text(encoding="utf-8"))

    # Check inspection reject missing UEXP sidecar
    def test_inspection_rejects_missing_uexp_sidecar(self) -> None:
        self.packages["BASE"].with_suffix(".uexp").unlink()
        runner = _HelperRunner()
        with self.assertRaisesRegex(EnemyDropGenerationError, "required .uexp sidecar"):
            self._serializer(runner).inspect_tables(
                work_root=self.root,
                mappings_path=self.mappings,
                table_files={"BASE": self.packages["BASE"]},
            )
        self.assertEqual(runner.documents, [])


if __name__ == "__main__":
    unittest.main()
