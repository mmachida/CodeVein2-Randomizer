"""Build the DEV-only Blood Code Article/location audit requested by the plan."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from cv2randomizer.core.seed import normalize_seed
from cv2randomizer.game.blood_code_catalog import BloodCodeCatalogStore
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.randomizer.blood_codes import build_blood_code_plan


# Return content class
def _content_class(package: str) -> str:
    folded = package.casefold()
    if "/dlc/" in folded:
        return "DLC"
    if "/afterend" in folded:
        return "AFTER_ENDING"
    return "BASE"


# Find source group
def _source_group(slot: dict[str, Any]) -> tuple[str, str]:
    package = str(slot["virtual_package"])
    export = str(slot["export_name"])
    kind = slot.get("location_kind", "gain_item")
    asset = str(slot["original_blood_code_asset"])
    folded = package.casefold()
    if "harmonymoon" in folded:
        return "DLC Harmony", "dlc_harmony_mission"
    if "dullahan" in asset.casefold() or "franz" in folded:
        return "boss", "boss_reward_task"
    if kind == "buddy_settings_reference":
        return "companion/NPC", "buddy_settings_blood_code"
    if kind == "scenario_reward":
        return "cutscene/event", "companion_scenario_reward_event"
    if kind == "proficiency_reference":
        return "main quest", "beginning_proficiency_reference"
    if "/quest/dlc/" in folded:
        return "DLC Harmony", "dlc_quest_gain_item"
    if package.startswith("/Game/Quest/MainEpisode_Beginning/"):
        return "main quest", "quest_gain_item"
    return "other", f"{kind}:{export}"


# Resolve location record
def _location_record(
    slot: dict[str, Any],
    inventory: PackageInventory,
    cache_inventory: PackageInventory | None,
    assigned: dict[str, Any] | None,
    reopened: bool,
) -> dict[str, Any]:
    package = str(slot["original_blood_code_package"])
    asset = str(slot["original_blood_code_asset"])
    source_packages = slot.get("physical_source_packages") or [slot["virtual_package"]]
    if slot.get("location_kind") in {"scenario_reward", "proficiency_reference"}:
        source_packages = [slot["virtual_package"], *[
            row["virtual_package"] for row in slot.get("mirrored_references", [])
        ]]
        if slot.get("location_kind") == "scenario_reward" and slot.get("physical_object_reference"):
            source_packages.extend(slot["physical_object_reference"])
    installed = inventory.contains(package) and all(inventory.contains(item) for item in source_packages)
    cache_available = None if cache_inventory is None else (
        cache_inventory.contains(package) and all(cache_inventory.contains(item) for item in source_packages)
    )
    source_group, source_type = _source_group(slot)
    location = slot.get("display_name") or (
        f"{slot['virtual_package']}::{slot['export_name']}"
        + (f".ArticleBulkInfo[{slot['bulk_index']}].Articles[{slot['reward_index']}]"
           if slot.get("location_kind", "gain_item") == "gain_item" else f".{slot.get('reference_property', '')}")
    )
    expected_asset = slot.get("expected_asset_name_value") or asset
    expected_number = slot.get("expected_asset_name_number", 0)
    if slot.get("object_reference"):
        expected_asset = slot["object_reference"]["expected_import_asset"]
        expected_number = slot["object_reference"]["expected_asset_name_number"]
    assigned_package = assigned.get("assigned_package") if assigned else None
    assigned_asset = assigned.get("assigned_asset") if assigned else None
    changed = bool(assigned_asset and assigned_package and assigned_package.casefold() != package.casefold())
    return {
        "location_id": slot["slot_id"],
        "source_type": source_type,
        "source_group": source_group,
        "quest_package": slot["virtual_package"] if str(slot["virtual_package"]).startswith("/Game/Quest/") else "",
        "event_task": slot["export_name"],
        "npc_companion": slot.get("display_name", ""),
        "boss": "Dullahan/Franz" if source_group == "boss" else "",
        "dlc_mission": slot["virtual_package"] if source_group == "DLC Harmony" else "",
        "reward_owner": f"{slot['virtual_package']}::{slot['export_name']}",
        "export_object_path": f"{slot['virtual_package']}.{slot['export_name']}",
        "reward_index": slot.get("reward_index", slot.get("reference_index")),
        "vanilla_blood_code": asset,
        "literal_fname": expected_asset,
        "fname_number": expected_number,
        "resolved_article": f"{package}.{asset}",
        "content_class": slot["content_class"],
        "installed": installed,
        "cache_available": cache_available,
        "alias_used": False,
        "protected": False,
        "protection_reason": "",
        "eligible_source": installed and cache_available is not False,
        "eligible_destination": installed and cache_available is not False,
        "planner_present": assigned is not None,
        "assigned_blood_code": assigned_asset or "",
        "patched": changed,
        "self_map": bool(assigned_package and assigned_package.casefold() == package.casefold()),
        "reopened_valid": reopened if changed else True,
        "spoiler_entry": f"{location}: {asset} -> {assigned_asset}" if assigned_asset else "",
    }


# Build audit
def build_audit(
    *,
    catalog_dir: Path,
    inventory_json: Path,
    article_identities_json: Path,
    cache_inventory_json: Path | None,
    assignment_json: Path | None,
    output_json: Path,
    output_csv: Path,
) -> dict[str, Any]:
    catalog = BloodCodeCatalogStore(catalog_dir).load("current")
    inventory = PackageInventory.create(json.loads(inventory_json.read_text(encoding="utf-8-sig")))
    cache_inventory = None if cache_inventory_json is None else PackageInventory.create(
        json.loads(cache_inventory_json.read_text(encoding="utf-8-sig"))
    )
    identities = json.loads(article_identities_json.read_text(encoding="utf-8-sig"))
    identity_by_package = {item["virtualPackage"].casefold(): item for item in identities}
    reward_locations = {slot.original_blood_code_package.casefold(): slot for slot in catalog.slots}
    reward_identities = {slot.original_blood_code_package.casefold() for slot in catalog.slots}
    lou_01 = "/game/blueprint/article/bloodcode/da_bloodcode_lou_01"
    assignment_document = {} if assignment_json is None else json.loads(assignment_json.read_text(encoding="utf-8-sig"))
    assigned_by_slot = assignment_document.get("assignments", {})
    reopened = bool(assignment_document.get("reopened", assignment_document.get("blood_code_reopened", False)))

    articles = []
    for package, identity in sorted(identity_by_package.items()):
        original = identity["virtualPackage"]
        asset = original.rsplit("/", 1)[-1]
        if "/debug/" in package:
            status, reason = "UNUSED/UNOBTAINABLE", "Debug-only Blood Code Article; no player reward location"
        elif asset == "DA_BloodCode_Lou_01":
            status, reason = (
                "PROTECTED_STARTING_UNRESOLVED",
                "The true New Game owner is unproven; changing DA_InitialInventory_Player.BloodCodes adds a second Blood Code.",
            )
        elif package in reward_identities:
            status, reason = f"OBTAINABLE_{_content_class(original)}", "At least one confirmed reward location"
        else:
            status, reason = "UNKNOWN_BUT_VALID", "Installed Article lacks a confirmed player-facing reward owner"
        articles.append({
            "logical_article_name": asset,
            "literal_fname": identity["literalFName"],
            "fname_number": identity["fNameNumber"],
            "resolved_package": original,
            "resolved_object_path": f"{original}.{identity['literalFName']}",
            "content_class": _content_class(original),
            "installed": inventory.contains(original),
            "cache_available": None if cache_inventory is None else cache_inventory.contains(original),
            "status": status,
            "reason": reason,
            "reward_location_count": sum(slot.original_blood_code_package.casefold() == package for slot in catalog.slots),
        })

    locations = []
    for slot in catalog.slots:
        locations.append(_location_record(
            {
                "slot_id": slot.slot_id,
                "location_kind": slot.location_kind,
                "content_class": slot.content_class,
                "virtual_package": slot.virtual_package,
                "export_index": slot.export_index,
                "export_name": slot.export_name,
                "bulk_index": slot.bulk_index,
                "reward_index": slot.reward_index,
                "reference_index": slot.reference_index,
                "reference_property": slot.reference_property,
                "original_blood_code_package": slot.original_blood_code_package,
                "original_blood_code_asset": slot.original_blood_code_asset,
                "expected_asset_name_value": slot.expected_asset_name_value,
                "expected_asset_name_number": slot.expected_asset_name_number,
                "object_reference": None if slot.object_reference is None else {
                    "expected_import_asset": slot.object_reference.expected_import_asset,
                    "expected_asset_name_number": slot.object_reference.expected_asset_name_number,
                },
                "display_name": slot.display_name,
                "mirrored_references": [
                    {"virtual_package": target.virtual_package}
                    for target in slot.mirrored_references
                ],
            },
            inventory,
            cache_inventory,
            assigned_by_slot.get(slot.slot_id),
            reopened,
        ))

    seed_matrix: dict[str, dict[str, dict[str, Any]]] = {}
    anchors = {
        "Noah_01": lambda slot: slot.location_kind == "buddy_settings_reference" and slot.original_blood_code_asset == "DA_BloodCode_Noah_01",
        "Heart": lambda slot: slot.location_kind == "buddy_settings_reference" and slot.original_blood_code_asset == "DA_BloodCode_GuardianHeart",
        "Dullahan": lambda slot: slot.original_blood_code_asset == "DA_BloodCode_BossDullahan",
        "Noah_02": lambda slot: slot.original_blood_code_asset == "DA_BloodCode_Noah_02",
        "DLC_Harmony": lambda slot: slot.content_class == "DLC" and slot.original_blood_code_asset == "DA_BloodCode_GQImogen",
    }
    for seed in ("bc-audit-20261004-1", "bc-audit-20261004-2", "bc-audit-20261004-3", "bc-audit-20261004-4", "bc-audit-20261004-5"):
        plan = build_blood_code_plan(catalog, seed, mix_postgame_dlc_with_base=True)
        by_name = {}
        for label, predicate in anchors.items():
            assignments = [item for item in plan.assignments if predicate(item.destination)]
            if len(assignments) != 1:
                by_name[label] = {"found": len(assignments)}
                continue
            assignment = assignments[0]
            by_name[label] = {
                "location": assignment.destination.slot_id,
                "vanilla": assignment.destination.original_blood_code_asset,
                "assigned": assignment.source.original_blood_code_asset,
                "self_map": assignment.destination.original_blood_code_package.casefold() == assignment.source.original_blood_code_package.casefold(),
                "patched": assignment.destination.original_blood_code_package.casefold() != assignment.source.original_blood_code_package.casefold(),
            }
        seed_matrix[seed] = by_name

    source_counts = Counter(row["source_group"] for row in locations)
    status_counts = Counter(row["status"] for row in articles)
    unknown = status_counts.get("UNKNOWN_BUT_VALID", 0)
    unsupported_locations = sum(not row["installed"] or row["cache_available"] is False for row in locations)
    assigned_rows = tuple(assigned_by_slot.values())
    assignment_vanilla = Counter(str(row.get("vanilla_package", "")).casefold() for row in assigned_rows)
    assignment_sources = Counter(str(row.get("assigned_package", "")).casefold() for row in assigned_rows)
    report = {
        "schema": "cv2-blood-code-reward-audit-v1",
        "installed_blood_code_articles": len(articles),
        "obtainable_blood_codes": sum(row["status"].startswith("OBTAINABLE_") for row in articles),
        "starting_lou_01_randomized_locations": sum(
            slot.original_blood_code_asset == "DA_BloodCode_Lou_01" for slot in catalog.slots
        ),
        "debug_unobtainable": sum(row["status"] == "UNUSED/UNOBTAINABLE" for row in articles),
        "unknown_but_valid": unknown,
        "unresolved_articles": unknown,
        "reward_locations_discovered": len(locations),
        "reward_locations_eligible": sum(row["eligible_destination"] for row in locations),
        "protected_locations": 0,
        "unsupported_locations": unsupported_locations,
        "unexpected_missing": unsupported_locations + unknown,
        "planner_assignments_per_seed": len(catalog.slots),
        "changed": None if not assignment_document else sum(bool(row["patched"]) for row in locations),
        "self_maps": None if not assignment_document else sum(
            bool(row.get("self_map", False)) for row in assignment_document.get("assignments", {}).values()
        ),
        "patched": None if not assignment_document else sum(bool(row["patched"]) for row in locations),
        "reopened_valid": reopened if assignment_document else None,
        "source_group_counts": dict(source_counts),
        "location_kind_counts": dict(Counter(slot.location_kind for slot in catalog.slots)),
        "article_status_counts": dict(status_counts),
        "content_class_counts": dict(Counter(row["content_class"] for row in articles)),
        "lou_01_source_enabled": any(
            str(row.get("assigned_package", "")).casefold() == lou_01 for row in assigned_rows
        ) if assignment_document else any(
            slot.original_blood_code_package.casefold() == lou_01 for slot in catalog.slots
        ),
        "lou_01_destination_enabled": any(
            str(row.get("vanilla_package", "")).casefold() == lou_01 for row in assigned_rows
        ) if assignment_document else any(
            slot.original_blood_code_package.casefold() == lou_01 for slot in catalog.slots
        ),
        "blood_code_only_pool": all(
            str(row.get("assigned_package", "")).casefold().startswith("/game/blueprint/article/bloodcode/")
            for row in assigned_rows
        ) if assignment_document else True,
        "assignment_multiset_preserved": assignment_vanilla == assignment_sources if assignment_document else None,
        "installed_cache_missing_articles": [
            row["logical_article_name"] for row in articles
            if row["installed"] and row["cache_available"] is False
        ] if cache_inventory is not None else [],
        "seed_regression_matrix": seed_matrix,
        "articles": articles,
        "locations": locations,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if locations:
        with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(locations[0]))
            writer.writeheader()
            writer.writerows(locations)
    return report


# Start the application
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-json", type=Path, required=True)
    parser.add_argument("--article-identities-json", type=Path, required=True)
    parser.add_argument("--cache-inventory-json", type=Path)
    parser.add_argument("--assignment-json", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("workspace"))
    args = parser.parse_args()
    report = build_audit(
        catalog_dir=Path("data/blood_codes/current"),
        inventory_json=args.inventory_json,
        article_identities_json=args.article_identities_json,
        cache_inventory_json=args.cache_inventory_json,
        assignment_json=args.assignment_json,
        output_json=args.output_dir / "blood_code_reward_audit.json",
        output_csv=args.output_dir / "blood_code_reward_audit.csv",
    )
    print(json.dumps({key: report[key] for key in (
        "installed_blood_code_articles", "obtainable_blood_codes", "starting_lou_01_randomized_locations",
        "reward_locations_discovered", "reward_locations_eligible", "unexpected_missing",
    )}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
