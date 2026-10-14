"""Version-bound loader for the confirmed Enemy Drop row classifications."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import application_data_path

TABLE_ORDER = ("BASE", "AFTER_ENDING", "DLC")
CLASSIFICATIONS = frozenset({"NORMAL_ONLY", "SHARED_BOSS_NORMAL", "UNRESOLVED"})
RANDOMIZED_CLASSIFICATIONS = frozenset({"NORMAL_ONLY", "SHARED_BOSS_NORMAL"})
EXPECTED_CLASS_COUNTS = Counter({"NORMAL_ONLY": 189, "SHARED_BOSS_NORMAL": 12, "UNRESOLVED": 32})
EXPECTED_TABLE_SHAPES = {
    "BASE": (154, 877, 130, 762),
    "AFTER_ENDING": (3, 27, 3, 27),
    "DLC": (76, 594, 68, 522),
}


class EnemyDropCatalogError(ValueError):
    """Raised when the Enemy Drop classification catalog is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class EnemyDropRowClassification:
    table: str
    row_id: str
    classification: str
    expected_slots: int


@dataclass(frozen=True, slots=True)
class EnemyDropCatalog:
    revision: str
    rows: tuple[EnemyDropRowClassification, ...]

    # Index records by row
    def by_row(self) -> dict[tuple[str, str], EnemyDropRowClassification]:
        return {(row.table, row.row_id.casefold()): row for row in self.rows}


class EnemyDropCatalogStore:
    """Load and verify the confirmed, game-revision-bound row catalog."""

    # Initialize this object
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_path("enemy_drops")

    # Load and validate catalog data
    def load(self, revision: str) -> EnemyDropCatalog:
        if not isinstance(revision, str) or not revision.strip():
            raise EnemyDropCatalogError("Enemy Drop catalog revision must be a non-empty identifier.")
        manifest_path = self.data_dir / "manifest.json"
        catalog_path = self.data_dir / "enemy_drops_pool_v1.csv"
        try:
            manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = catalog_path.read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EnemyDropCatalogError(f"Could not read Enemy Drop catalog data: {error}") from error
        if not isinstance(manifest, dict) or manifest.get("schema") != "cv2-enemy-drop-catalog-v1":
            raise EnemyDropCatalogError("Enemy Drop catalog manifest has an unknown schema.")
        if manifest.get("catalog_revision") != revision:
            raise EnemyDropCatalogError(
                f"Enemy Drop catalog is bound to revision {manifest.get('catalog_revision')!r}, not {revision!r}."
            )
        files = manifest.get("files")
        metadata = files.get(catalog_path.name) if isinstance(files, dict) else None
        if not isinstance(metadata, dict):
            raise EnemyDropCatalogError("Enemy Drop manifest does not describe its classification CSV.")
        expected_hash = metadata.get("sha256")
        if not isinstance(expected_hash, str) or hashlib.sha256(payload).hexdigest() != expected_hash.casefold():
            raise EnemyDropCatalogError("Enemy Drop classification CSV hash does not match its manifest.")
        try:
            text = payload.decode("utf-8-sig")
            reader = csv.DictReader(text.splitlines())
            required = {"table", "row_id", "classification", "expected_slots"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise EnemyDropCatalogError("Enemy Drop CSV is missing required columns.")
            rows: list[EnemyDropRowClassification] = []
            seen: set[tuple[str, str]] = set()
            for raw in reader:
                table = (raw.get("table") or "").strip().upper()
                row_id = (raw.get("row_id") or "").strip()
                classification = (raw.get("classification") or "").strip().upper()
                slots_raw = (raw.get("expected_slots") or "").strip()
                try:
                    slots = int(slots_raw)
                except ValueError as error:
                    raise EnemyDropCatalogError(f"Invalid expected slot count for {table}:{row_id}.") from error
                key = (table, row_id.casefold())
                if table not in TABLE_ORDER or not row_id or key in seen:
                    raise EnemyDropCatalogError(f"Invalid or duplicate Enemy Drop row {table}:{row_id}.")
                if classification not in CLASSIFICATIONS or slots <= 0:
                    raise EnemyDropCatalogError(f"Invalid classification or slot count for {table}:{row_id}.")
                seen.add(key)
                rows.append(EnemyDropRowClassification(table, row_id, classification, slots))
        except (UnicodeError, csv.Error) as error:
            raise EnemyDropCatalogError(f"Could not parse Enemy Drop classification CSV: {error}") from error
        rows.sort(key=lambda row: (TABLE_ORDER.index(row.table), row.row_id.casefold(), row.row_id))
        self._validate(rows, metadata)
        return EnemyDropCatalog(revision, tuple(rows))

    # Validate catalog entries
    @staticmethod
    def _validate(rows: list[EnemyDropRowClassification], metadata: dict[str, Any]) -> None:
        if metadata.get("count") != 233 or len(rows) != 233:
            raise EnemyDropCatalogError(f"Expected 233 classified Enemy Drop rows, got {len(rows)}.")
        counts = Counter(row.classification for row in rows)
        if counts != EXPECTED_CLASS_COUNTS:
            raise EnemyDropCatalogError(f"Enemy Drop classification counts differ from confirmed V1: {dict(counts)}.")
        for table in TABLE_ORDER:
            table_rows = [row for row in rows if row.table == table]
            eligible = [row for row in table_rows if row.classification in RANDOMIZED_CLASSIFICATIONS]
            observed = (
                len(table_rows),
                sum(row.expected_slots for row in table_rows),
                len(eligible),
                sum(row.expected_slots for row in eligible),
            )
            if observed != EXPECTED_TABLE_SHAPES[table]:
                raise EnemyDropCatalogError(
                    f"{table} catalog shape differs from confirmed V1: expected {EXPECTED_TABLE_SHAPES[table]}, got {observed}."
                )
