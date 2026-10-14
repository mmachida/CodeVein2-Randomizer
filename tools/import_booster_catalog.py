"""Create the runtime Booster mastery catalog from the Pass 15 research CSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


TABLE_PACKAGE = "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/DT_GainBoosterSmallScenario"
EXPECTED_COUNTS = {"BASE": 57, "DLC": 18, "AFTER_ENDING": 27}
FORMULA_RE = re.compile(r"(?<![A-Za-z0-9_])(Booster_[A-Za-z0-9_]+)\s*==\s*false\Z")
MASTERY_RE = re.compile(r"\bBloodCodeMastery\.([A-Za-z0-9_]+)\s*>=\s*(Rank[0-9]+_Master)\b")


# Calculate the SHA-256 hash
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Parse soft path
def parse_soft_path(value: str, field: str) -> tuple[str, str]:
    parts = value.rsplit("::", 1)
    if len(parts) != 2 or not parts[0].startswith("/Game/"):
        raise ValueError(f"Invalid {field} soft path: {value!r}")
    package, asset = parts
    if package.rsplit("/", 1)[-1] != asset:
        raise ValueError(f"{field} asset name differs from its package: {value!r}")
    return package, asset


# Classify the reward source
def classify(reward_package: str) -> str:
    folded = reward_package.casefold()
    if "/aed_" in folded:
        return "AFTER_ENDING"
    if "/dlc_" in folded:
        return "DLC"
    return "BASE"


# Build catalog
def build_catalog(rows_csv: Path, source_header: Path, source_sidecar: Path, revision: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with rows_csv.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"source_package", "row_index", "row_name", "scalar_values", "booster_assets", "booster_flags", "mastery_tokens"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("Pass 15 row CSV is missing required columns.")
        rows = [row for row in reader if row.get("source_package") == TABLE_PACKAGE]
    if len(rows) != 102:
        raise ValueError(f"Expected 102 Booster mastery rows, found {len(rows)}.")

    records: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw in rows:
        row_index = int(raw["row_index"])
        row_name = raw["row_name"].strip()
        if row_index < 0 or row_name.casefold() in seen_names:
            raise ValueError(f"Invalid or duplicate Booster row {row_name!r}.")
        seen_names.add(row_name.casefold())
        scalar_values = raw["scalar_values"].split(" | ")
        if len(scalar_values) != 5:
            raise ValueError(f"Unexpected scalar field shape for {row_name}.")
        formula, blood_code_value, notification_value, _reward_value, count_value = scalar_values
        blood_code_package, blood_code_asset = parse_soft_path(blood_code_value, "Blood Code")
        formula_match = FORMULA_RE.search(formula)
        mastery_match = MASTERY_RE.search(formula)
        if formula_match is None or mastery_match is None:
            raise ValueError(f"Could not parse the Booster flag/mastery requirement for {row_name}.")
        flag = formula_match.group(1)
        raw_flags = [item.strip() for item in raw["booster_flags"].split(" | ") if item.strip()]
        if not raw_flags or raw_flags[0] != flag:
            raise ValueError(f"Booster formula flag differs from the Pass 15 row metadata for {row_name}.")
        reward_parts = [item.strip() for item in raw["booster_assets"].split(" | ")]
        if len(reward_parts) != 2:
            raise ValueError(f"Could not parse the Booster reward identity for {row_name}.")
        reward_package, reward_asset = reward_parts
        if reward_asset != reward_package.rsplit("/", 1)[-1]:
            raise ValueError(f"Booster asset name differs from its package for {row_name}.")
        count = int(count_value)
        if count <= 0 or notification_value not in {"True", "False"}:
            raise ValueError(f"Invalid Count/bNotification for {row_name}.")
        content_class = classify(reward_package)
        records.append({
            "row_name": row_name,
            "row_index": row_index,
            "content_class": content_class,
            "evaluation_formula_original": formula,
            "mastery_owner": mastery_match.group(1),
            "mastery_rank": mastery_match.group(2),
            "blood_code_article_path": blood_code_package,
            "blood_code_article_asset": blood_code_asset,
            "booster_story_flag": flag,
            "booster_article_path": reward_package,
            "booster_article_asset": reward_asset,
            "count": count,
            "b_notification": notification_value == "True",
            "reward_id": f"{flag}|{reward_package}",
        })
    records.sort(key=lambda row: (row["row_index"], row["row_name"].casefold()))
    if [row["row_index"] for row in records] != list(range(102)):
        raise ValueError("Booster source row indexes must contain every index 0..101 exactly once.")
    counts = Counter(row["content_class"] for row in records)
    if dict(counts) != EXPECTED_COUNTS:
        raise ValueError(f"Booster source content groups differ from Pass 15: expected {EXPECTED_COUNTS}, got {dict(counts)}.")
    if len({row["reward_id"].casefold() for row in records}) != 102:
        raise ValueError("Booster reward identities are not unique in the source table.")
    if not source_header.is_file() or not source_sidecar.is_file():
        raise ValueError("Original Booster DataTable .uasset and .uexp must both be supplied.")
    catalog = {"schema": "cv2-booster-mastery-v1", "catalog_revision": revision, "slots": records}
    catalog_bytes = (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest = {
        "schema": "cv2-booster-catalog-v1",
        "catalog_revision": revision,
        "source_package": TABLE_PACKAGE,
        "source_package_sha256": {
            ".uasset": sha256(source_header),
            ".uexp": sha256(source_sidecar),
        },
        "files": {
            "mastery_boosters.json": {
                "count": len(records),
                "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            },
        },
        "content_class_counts": dict(sorted(counts.items())),
    }
    return catalog, manifest


# Start the application
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows-csv", type=Path, required=True)
    parser.add_argument("--source-uasset", type=Path, required=True)
    parser.add_argument("--source-uexp", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--catalog-revision", default="current")
    args = parser.parse_args()
    catalog, manifest = build_catalog(
        args.rows_csv, args.source_uasset, args.source_uexp, args.catalog_revision
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "mastery_boosters.json").write_bytes(
        (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(catalog['slots'])} Booster mastery rows to {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
