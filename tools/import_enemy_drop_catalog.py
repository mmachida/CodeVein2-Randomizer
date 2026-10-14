"""Normalize the confirmed local Enemy Drop row classification for runtime use."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT / "workspace" / "research_assets" / "enemy_drops" / "drop_row_usage_summary.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "enemy_drops" / "enemy_drops_pool_v1.csv"
DEFAULT_MANIFEST = PROJECT_ROOT / "data" / "enemy_drops" / "manifest.json"
TABLE_ORDER = ("BASE", "AFTER_ENDING", "DLC")
CLASSIFICATIONS = ("NORMAL_ONLY", "SHARED_BOSS_NORMAL", "UNRESOLVED")
EXPECTED_CLASS_COUNTS = {
    "NORMAL_ONLY": 189,
    "SHARED_BOSS_NORMAL": 12,
    "UNRESOLVED": 32,
}
EXPECTED_TABLE_SHAPES = {
    "BASE": (154, 877, 120, 711),
    "AFTER_ENDING": (3, 27, 3, 27),
    "DLC": (76, 594, 66, 510),
}


class ImportError(ValueError):
    """Raised when research classifications do not match the confirmed V1 catalog."""


# Count stable JSON bytes
def stable_json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


# Normalize rows
def normalize_rows(source: Path) -> list[dict[str, str]]:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"source_table", "row_name", "drop_slot_count", "usage_class"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ImportError("Research summary is missing source_table, row_name, drop_slot_count, or usage_class.")
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for source_row in reader:
            table = source_row["source_table"].strip().upper()
            row_id = source_row["row_name"].strip()
            classification = source_row["usage_class"].strip().upper()
            try:
                slot_count = int(source_row["drop_slot_count"])
            except (TypeError, ValueError) as error:
                raise ImportError(f"Invalid slot count for {table}:{row_id}.") from error
            key = (table, row_id.casefold())
            if table not in TABLE_ORDER or not row_id or key in seen:
                raise ImportError(f"Invalid or duplicate classified row: {table}:{row_id}.")
            if classification not in CLASSIFICATIONS or slot_count <= 0:
                raise ImportError(f"Invalid classification or slot count for {table}:{row_id}.")
            seen.add(key)
            rows.append({
                "table": table,
                "row_id": row_id,
                "classification": classification,
                "expected_slots": str(slot_count),
            })
    rows.sort(key=lambda row: (TABLE_ORDER.index(row["table"]), row["row_id"].casefold(), row["row_id"]))
    validate_rows(rows)
    return rows


# Validate rows
def validate_rows(rows: list[dict[str, str]]) -> None:
    if len(rows) != 233:
        raise ImportError(f"Expected 233 classified rows, got {len(rows)}.")
    counts = Counter(row["classification"] for row in rows)
    if counts != Counter(EXPECTED_CLASS_COUNTS):
        raise ImportError(f"Classification counts do not match the confirmed catalog: {dict(counts)}.")
    for table in TABLE_ORDER:
        table_rows = [row for row in rows if row["table"] == table]
        normal_rows = [row for row in table_rows if row["classification"] == "NORMAL_ONLY"]
        try:
            total_slots = sum(int(row["expected_slots"]) for row in table_rows)
            eligible_slots = sum(int(row["expected_slots"]) for row in normal_rows)
        except ValueError as error:
            raise ImportError(f"Non-integer expected_slots value in {table}.") from error
        expected = EXPECTED_TABLE_SHAPES[table]
        observed = (len(table_rows), total_slots, len(normal_rows), eligible_slots)
        if observed != expected:
            raise ImportError(f"{table} shape mismatch: expected {expected}, got {observed}.")


# Write catalog
def write_catalog(source: Path, output: Path, manifest_path: Path, revision: str) -> dict[str, Any]:
    rows = normalize_rows(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    if output.exists():
        with output.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), [])
        if "classification" not in header:
            preserved = output.with_name("enemy_drops_reward_slots_research.csv")
            if preserved.exists() and hashlib.sha256(preserved.read_bytes()).digest() != hashlib.sha256(output.read_bytes()).digest():
                raise ImportError(f"Refusing to overwrite the existing research snapshot at {preserved}.")
            if not preserved.exists():
                shutil.copy2(output, preserved)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=output.parent,
                                     prefix=".enemy-drops-", suffix=".tmp", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        writer = csv.DictWriter(temporary, fieldnames=("table", "row_id", "classification", "expected_slots"), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        os.replace(temporary_path, output)
    finally:
        temporary_path.unlink(missing_ok=True)

    payload = output.read_bytes()
    manifest = {
        "schema": "cv2-enemy-drop-catalog-v1",
        "catalog_revision": revision,
        "files": {
            output.name: {
                "count": len(rows),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        },
        "source_sha256": {
            source.name: hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "classifications": dict(sorted(Counter(row["classification"] for row in rows).items())),
        "table_shapes": {
            table: {
                "rows": EXPECTED_TABLE_SHAPES[table][0],
                "slots": EXPECTED_TABLE_SHAPES[table][1],
                "eligible_rows": EXPECTED_TABLE_SHAPES[table][2],
                "eligible_slots": EXPECTED_TABLE_SHAPES[table][3],
            }
            for table in TABLE_ORDER
        },
    }
    manifest_path.write_bytes(stable_json_bytes(manifest))
    return manifest


# Start the application
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--catalog-revision", default="current")
    args = parser.parse_args()
    try:
        manifest = write_catalog(args.source, args.output, args.manifest, args.catalog_revision)
    except (ImportError, OSError) as error:
        parser.error(str(error))
    print(f"Wrote {manifest['files'][args.output.name]['count']} classified Enemy Drop rows for revision {args.catalog_revision}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
