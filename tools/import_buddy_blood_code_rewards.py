"""Merge structurally verified BuddySettings Blood Code unlocks into the catalog.

This is a DEV-time import. Generate consumes the checked-in, fingerprinted
catalog and never scans BuddySettings assets at runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


FIXED_INITIAL = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01"


# Calculate the SHA-256 hash
def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# List buddy slot
def _buddy_slot(
    buddy: dict[str, Any],
    imported: dict[str, Any],
    reward: str,
    *,
    linked_proficiency_references: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    package = buddy["package"]
    export_index = buddy["exportIndex"]
    export_name = buddy["exportName"]
    asset = reward.rsplit("/", 1)[-1]
    npc = export_name.removeprefix("DA_BuddySettings_")
    display_name = {
        "DA_BloodCode_Noah_01": "Noah_01 quest reward",
        "DA_BloodCode_GuardianHeart": "Guardian's Heart quest reward",
    }.get(asset, f"Companion unlock - {npc} tier 1")
    return {
        "slot_id": f"buddy:{package}::export={export_index}::BloodCode",
        "location_kind": "buddy_settings_reference",
        "content_class": "DLC" if "/DLC/" in reward else "BASE",
        "virtual_package": package,
        "export_index": export_index,
        "export_name": export_name,
        "bulk_index": None,
        "reward_index": None,
        "original_blood_code_package": reward,
        "original_blood_code_asset": asset,
        "count": None,
        "b_notification": None,
        "display_name": display_name,
        "reference_property": "BloodCode",
        "reference_index": 0,
        "expected_package_name_value": reward,
        "expected_package_name_number": 0,
        "expected_asset_name_value": imported["objectName"],
        "expected_asset_name_number": imported["objectNameNumber"],
        "object_reference": {
            "virtual_package": package,
            "export_index": export_index,
            "export_name": export_name,
            "reference_property": "BloodCode",
            "property_schema_index": 6,
            "expected_import_package": reward,
            "expected_import_asset": imported["objectName"],
            "expected_package_name_number": 0,
            "expected_asset_name_number": imported["objectNameNumber"],
            "expected_import_class_name": imported["className"],
            "expected_import_class_package": imported["classPackage"],
        },
        "linked_proficiency_references": linked_proficiency_references or [],
    }


# Fingerprint buddy source
def _fingerprint_buddy_source(
    package: str,
    *,
    content_root: Path,
    source_hashes: dict[str, Any],
) -> None:
    source_header = content_root.joinpath(*package.split("/")[2:]).with_suffix(".uasset")
    source_sidecar = source_header.with_suffix(".uexp")
    if not source_header.is_file() or not source_sidecar.is_file():
        raise ValueError(f"BuddySettings source package is missing a required sidecar: {package}")
    source_hashes[package] = {".uasset": sha256(source_header), ".uexp": sha256(source_sidecar)}


# Read complete linked proficiency references
def _complete_linked_proficiency_references(slot: dict[str, Any]) -> None:
    references = slot.get("linked_proficiency_references", [])
    if not references:
        return
    required_fields = (
        "reference_property", "reference_index", "expected_array_count",
        "expected_package_name_value", "expected_package_name_number",
        "expected_asset_name_value", "expected_asset_name_number",
    )
    template = next((item for item in references if all(item.get(key) is not None for key in required_fields)), None)
    if template is None:
        raise ValueError(f"Linked proficiency references have no complete source target: {slot.get('slot_id')}")
    for reference in references:
        for key in required_fields:
            reference.setdefault(key, template[key])


# Resolve article path
def _article_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.startswith("/Game/Blueprint/Article/BloodCode/"):
        raise ValueError(f"Invalid BuddySettings Blood Code package: {raw!r}")
    if raw.rsplit("/", 1)[-1] == "Debug" or "/Debug/" in raw:
        raise ValueError(f"Debug assets are not BuddySettings unlock candidates: {raw}")
    return raw


# Import buddy rewards
def import_buddy_rewards(
    *,
    catalog_path: Path,
    manifest_path: Path,
    inspection_path: Path,
    installed_inventory_path: Path,
    content_root: Path,
) -> int:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    inspections = json.loads(inspection_path.read_text(encoding="utf-8"))
    inventory = json.loads(installed_inventory_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("slots"), list):
        raise ValueError("Blood Code catalog has no slots list.")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("source_package_sha256"), dict):
        raise ValueError("Blood Code manifest has no source fingerprints.")
    if not isinstance(inventory, list) or not inventory or any(not isinstance(item, str) for item in inventory):
        raise ValueError("Installed Blood Code Article inventory is invalid.")

    slots = catalog["slots"]
    existing_reward_packages = {slot["original_blood_code_package"].casefold() for slot in slots}
    installed = {
        _article_path(item)
        for item in inventory
        if item.rsplit("/", 1)[-1].startswith("DA_BloodCode_") and "/Debug/" not in item
    }
    installed_by_fold = {path.casefold(): path for path in installed}
    available_candidates = {
        installed_by_fold[folded]
        for folded in installed_by_fold.keys() - existing_reward_packages - {FIXED_INITIAL.casefold()}
    }
    by_reward: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for buddy in inspections:
        if not isinstance(buddy, dict) or buddy.get("className") != "BuddySettings":
            raise ValueError("BuddySettings inspection contains an invalid record.")
        package = buddy.get("package")
        export_index = buddy.get("exportIndex")
        export_name = buddy.get("exportName")
        dependent = buddy.get("dependentExports")
        imports = buddy.get("bloodCodeImports")
        if (
            not isinstance(package, str) or not package.startswith("/Game/Blueprint/Characters/NPC/Buddy/")
            or type(export_index) is not int or export_index < 1
            or export_name != package.rsplit("/", 1)[-1]
            or not isinstance(dependent, list)
            or not any(
                item.get("exportIndex") == export_index
                and item.get("exportName") == export_name
                and item.get("dependencies")
                for item in dependent if isinstance(item, dict)
            )
            or not isinstance(imports, list) or len(imports) != 1
        ):
            raise ValueError(f"BuddySettings unlock identity is incomplete: {package!r}")
        imported = imports[0]
        reward = _article_path(imported.get("package"))
        if (
            imported.get("objectName") != reward.rsplit("/", 1)[-1]
            or imported.get("objectNameNumber") != 0
            or imported.get("className") != "BloodCodeArticleData"
            or imported.get("classPackage") != "/Script/Extensions"
            or imported.get("outerIndex", 0) == 0
        ):
            raise ValueError(f"BuddySettings has an unexpected BloodCodeArticleData import: {package}")
        by_reward.setdefault(reward, []).append((buddy, imported))

    ambiguous = {reward: len(matches) for reward, matches in by_reward.items() if len(matches) != 1}
    content_root = content_root.resolve(strict=True)
    source_hashes = manifest["source_package_sha256"]
    added = 0
    converted = 0

    # A quest proficiency list can mention an Article without owning the player-facing
    # unlock. Prefer the unique BuddySettings.BloodCode owner and keep the mirrored
    # proficiency entries as linked metadata so the randomized code still gains the
    # intended proficiency. This prevents a reference-only slot from masking the
    # actual grant owner during catalog discovery.
    for index, old_slot in enumerate(tuple(slots)):
        if old_slot.get("location_kind") != "proficiency_reference":
            continue
        reward = old_slot.get("original_blood_code_package")
        matches = by_reward.get(reward, [])
        if len(matches) != 1:
            continue
        buddy, imported = matches[0]
        if any(
            item.get("location_kind") == "buddy_settings_reference"
            and item.get("virtual_package", "").casefold() == buddy["package"].casefold()
            for item in slots
        ):
            raise ValueError(f"Proficiency placeholder and BuddySettings owner both catalog the same reward: {reward}")
        proficiency_template = {
            "reference_property": old_slot["reference_property"],
            "reference_index": old_slot["reference_index"],
            "expected_array_count": old_slot["expected_array_count"],
            "expected_package_name_value": old_slot["expected_package_name_value"],
            "expected_package_name_number": old_slot["expected_package_name_number"],
            "expected_asset_name_value": old_slot["expected_asset_name_value"],
            "expected_asset_name_number": old_slot["expected_asset_name_number"],
        }
        linked = [{**proficiency_template,
            "virtual_package": old_slot["virtual_package"],
            "export_index": old_slot["export_index"],
            "export_name": old_slot["export_name"],
        }]
        linked.extend({**proficiency_template, **target} for target in old_slot.get("mirrored_references", []))
        slots[index] = _buddy_slot(
            buddy, imported, reward,
            linked_proficiency_references=sorted(
                linked, key=lambda item: item["virtual_package"].casefold(),
            ),
        )
        _fingerprint_buddy_source(buddy["package"], content_root=content_root, source_hashes=source_hashes)
        converted += 1

    for slot in slots:
        if slot.get("location_kind") == "buddy_settings_reference":
            _complete_linked_proficiency_references(slot)

    missing = sorted(available_candidates - set(by_reward))
    if missing or ambiguous:
        raise ValueError(f"Installed Blood Codes lack one unique BuddySettings owner (missing={missing}, ambiguous={ambiguous}).")

    for reward in sorted(available_candidates, key=str.casefold):
        buddy, imported = by_reward[reward][0]
        slot = _buddy_slot(buddy, imported, reward)
        slots.append(slot)
        _fingerprint_buddy_source(buddy["package"], content_root=content_root, source_hashes=source_hashes)
        added += 1

    represented = {slot["original_blood_code_package"].casefold() for slot in slots}
    unresolved = sorted(
        (
            path for path in installed
            if path.casefold() not in represented
            and path.casefold() != FIXED_INITIAL.casefold()
            and "/debug/" not in path.casefold()
        ),
        key=str.casefold,
    )
    if unresolved:
        raise ValueError(f"Installed obtainable Blood Code inventory has unrepresented articles: {unresolved}")

    slots.sort(key=lambda row: (
        ("BASE", "AFTER_ENDING", "DLC").index(row["content_class"]),
        row["virtual_package"].casefold(), row["export_index"],
        row.get("bulk_index", -1) if row.get("bulk_index") is not None else -1,
        row.get("reward_index", row.get("reference_index", -1)),
    ))
    payload = (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    catalog_path.write_bytes(payload)
    manifest["files"][catalog_path.name] = {"count": len(slots), "sha256": hashlib.sha256(payload).hexdigest()}
    manifest["content_class_counts"] = dict(Counter(row["content_class"] for row in slots))
    manifest["source_scan"]["buddy_settings_exports_inspected"] = len(inspections)
    manifest["source_scan"]["buddy_settings_unlock_locations_added"] = added + converted
    manifest["source_scan"]["proficiency_only_locations_converted_to_buddy_owners"] = converted
    manifest["source_scan"]["installed_blood_code_articles"] = sum(
        item.rsplit("/", 1)[-1].startswith("DA_BloodCode_") for item in inventory
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added + converted


# Start the application
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--content-root", type=Path, required=True, help="Extracted game Content root used for source fingerprints")
    parser.add_argument("--inventory-json", type=Path, required=True, help="Complete installed Blood Code package inventory")
    parser.add_argument("--data-dir", type=Path, default=Path("data/blood_codes/current"))
    args = parser.parse_args()
    added = import_buddy_rewards(
        catalog_path=args.data_dir / "blood_code_grants.json",
        manifest_path=args.data_dir / "manifest.json",
        inspection_path=args.research_root / "buddy-settings-imports.json",
        installed_inventory_path=args.inventory_json,
        content_root=args.content_root,
    )
    print(f"Imported {added} BuddySettings Blood Code unlock locations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
