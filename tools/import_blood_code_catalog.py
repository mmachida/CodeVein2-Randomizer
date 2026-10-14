"""Create the version-bound runtime Blood Code grant catalog from a UAssetAPI scan."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any


# This import is the quest GainItem scan only. Scenario and BuddySettings
# locations are merged by their dedicated, version-bound DEV importers.
EXPECTED_COUNTS = {"BASE": 14, "AFTER_ENDING": 13, "DLC": 8}
SLOT_ID_RE = re.compile(r"[A-Za-z0-9_./:=+-]+\Z")


# Calculate the SHA-256 hash
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Classify the reward source
def classify(package: str) -> str:
    folded = package.casefold()
    if "/quest/dlc/" in folded:
        return "DLC"
    if "/quest/epilogue_afterending/drain_afterending/" in folded:
        return "AFTER_ENDING"
    if "/quest/" in folded:
        return "BASE"
    raise ValueError(f"Blood Code grant is outside the classified Quest scopes: {package}")


# Build catalog
def build_catalog(scan: dict[str, Any], content_root: Path, revision: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if scan.get("failureCount") != 0:
        raise ValueError(f"UAssetAPI scan must be complete; found {scan.get('failureCount')} failures.")
    grants = scan.get("grants")
    if not isinstance(grants, list):
        raise ValueError("Scan report has no grant list.")
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, int]] = set()
    source_hashes: dict[str, dict[str, str]] = {}
    for hit in grants:
        package = hit.get("package")
        export_index = hit.get("exportIndex")
        export_name = hit.get("exportName")
        bulk_index = hit.get("bulkIndex")
        reward_index = hit.get("rewardIndex")
        reward_package = hit.get("rewardPackage")
        reward_asset = hit.get("rewardAsset")
        count = hit.get("count")
        notification = hit.get("notification")
        if not isinstance(package, str) or not package.startswith("/Game/Quest/"):
            raise ValueError(f"Invalid Quest package path: {package!r}")
        if any(type(value) is not int or value < 0 for value in (export_index, bulk_index, reward_index)):
            raise ValueError(f"Invalid export/article identity in {hit!r}")
        if export_index == 0 or not isinstance(export_name, str) or "gainitem" not in export_name.casefold():
            raise ValueError(f"Invalid GainItem export identity in {hit!r}")
        if not isinstance(reward_package, str) or not reward_package.startswith("/Game/Blueprint/Article/BloodCode/"):
            raise ValueError(f"Invalid Blood Code reward package in {hit!r}")
        if not isinstance(reward_asset, str) or reward_asset != reward_package.rsplit("/", 1)[-1]:
            raise ValueError(f"Blood Code reward asset name does not match its package: {hit!r}")
        if type(count) is not int or count <= 0 or type(notification) is not bool:
            raise ValueError(f"Invalid direct reward Count/bNotification in {hit!r}")
        slot_key = (package.casefold(), export_index, bulk_index, reward_index)
        if slot_key in seen:
            raise ValueError(f"Duplicate grant slot: {slot_key}")
        seen.add(slot_key)
        relative = PurePosixPath(*package.split("/")[2:])
        source_header = content_root.joinpath(*relative.parts).with_suffix(".uasset")
        source_sidecar = source_header.with_suffix(".uexp")
        if not source_header.is_file() or not source_sidecar.is_file():
            raise ValueError(f"Grant source package is missing its .uasset/.uexp pair: {package}")
        source_hashes[package] = {
            ".uasset": sha256(source_header),
            ".uexp": sha256(source_sidecar),
        }
        slot_id = f"{package}::export={export_index}::bulk={bulk_index}::article={reward_index}"
        if not SLOT_ID_RE.fullmatch(slot_id):
            raise ValueError(f"Generated slot id has unexpected characters: {slot_id}")
        records.append({
            "slot_id": slot_id,
            "content_class": classify(package),
            "virtual_package": package,
            "export_index": export_index,
            "export_name": export_name,
            "bulk_index": bulk_index,
            "reward_index": reward_index,
            "original_blood_code_package": reward_package,
            "original_blood_code_asset": reward_asset,
            "count": count,
            "b_notification": notification,
        })
    records.sort(key=lambda row: (
        ("BASE", "AFTER_ENDING", "DLC").index(row["content_class"]),
        row["virtual_package"].casefold(),
        row["export_index"],
        row["bulk_index"],
        row["reward_index"],
    ))
    counts = Counter(row["content_class"] for row in records)
    if len(records) != sum(EXPECTED_COUNTS.values()) or dict(counts) != EXPECTED_COUNTS:
        raise ValueError(f"Blood Code grant shape differs from research: expected {EXPECTED_COUNTS}, got {dict(counts)}.")
    if scan.get("grantCount") != len(records):
        raise ValueError("UAssetAPI grant count does not match the serialized grant list.")
    catalog = {
        "schema": "cv2-blood-code-grants-v1",
        "catalog_revision": revision,
        "slots": records,
    }
    catalog_bytes = (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest = {
        "schema": "cv2-blood-code-catalog-v1",
        "catalog_revision": revision,
        "files": {
            "blood_code_grants.json": {
                "count": len(records),
                "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            },
        },
        "content_class_counts": dict(sorted(counts.items())),
        "source_scan": {
            "quest_assets": scan.get("questAssetCount"),
            "parsed_gainitem_exports": scan.get("parsedGainItemCount"),
            "grant_count": scan.get("grantCount"),
            "failures": scan.get("failureCount"),
        },
        "source_package_sha256": source_hashes,
    }
    return catalog, manifest


# Start the application
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan-json", type=Path, required=True)
    parser.add_argument("--content-root", type=Path, required=True, help="Extracted CodeVein2/Content root used for the read-only scan")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--catalog-revision", default="current")
    args = parser.parse_args()
    scan = json.loads(args.scan_json.read_text(encoding="utf-8-sig"))
    catalog, manifest = build_catalog(scan, args.content_root.resolve(strict=True), args.catalog_revision)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "blood_code_grants.json").write_bytes(
        (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(catalog['slots'])} version-bound Blood Code grants to {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
