"""DEV import of structurally verified companion grants; never scans during Generate."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


# Start the application
def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    research = repo / "workspace" / "blood-code-runtime-audit"
    data = repo / "data" / "blood_codes" / "current"
    catalog = json.loads((data / "blood_code_grants.json").read_text(encoding="utf-8"))
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    groups: dict[str, list[dict]] = defaultdict(list)
    for path in sorted((research / "inspection" / "Cinematics").rglob("*.uasset.json")):
        if "/Blueprint/BloodCode/" not in path.as_posix():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        for export in document["exports"]:
            fields = export["data"].get("Data")
            if not export["exportName"].startswith("Default__") or not isinstance(fields, list):
                continue
            bulk = [field for field in fields if field.get("Name") == "ArticleBulkInfo"]
            if not bulk:
                continue
            imports = document["imports"]
            cls = document["exports"][export["data"]["ClassIndex"] - 1]["data"]
            parent = cls.get("SuperStruct", 0)
            if parent >= 0 or imports[-parent - 1]["objectName"] != "BP_SmallScenarioGainBloodCodeBase_C":
                raise ValueError(f"Unconfirmed grant parent: {path}")
            arrays = [field for field in bulk[0]["Value"] if field["Name"] == "Articles"]
            if len(bulk) != 1 or len(arrays) != 1 or len(arrays[0]["Value"]) != 1:
                raise ValueError(f"Unsupported grant structure: {path}")
            reward = arrays[0]["Value"][0]
            soft = next(field["Value"] for field in reward["Value"] if field["Name"] == "ArticleDataPath")
            package = soft["AssetPath"]["PackageName"]
            asset = soft["AssetPath"]["AssetName"]
            if not package.startswith("/Game/Blueprint/Article/BloodCode/"):
                continue
            if package.rsplit("/", 1)[-1] != asset or soft.get("SubPathString"):
                raise ValueError(f"Unsupported Blood Code identity: {path}")
            # Group only the explicit alternate scenario of the same named event,
            # never different unlock events that happen to award the same Article.
            relative = path.relative_to(research / "inspection").as_posix().removesuffix(".uasset.json")
            event = relative.removesuffix("_Yadorigi")
            flags = {field["Name"]: field["Value"] for field in fields
                     if field["Name"] in {"FlagLabel", "FlagValueBeforeGain", "FlagValueAfterGain", "FlagAfterLabelOverride"}}
            groups[event].append({"virtual_package": "/Game/" + relative,
                                  "export_index": export["exportIndex"], "export_name": export["exportName"],
                                  "package": package, "asset": asset, "flags": flags,
                                  "count": next(field["Value"] for field in reward["Value"] if field["Name"] == "Count"),
                                  "notification": next(field["Value"] for field in reward["Value"] if field["Name"] == "bNotification")})
    existing = [slot for slot in catalog["slots"] if slot.get("location_kind", "gain_item") != "scenario_reward"]
    for event, physical in sorted(groups.items()):
        primary = physical[0]
        for mirror in physical[1:]:
            if (mirror["package"], mirror["flags"]) != (primary["package"], primary["flags"]):
                raise ValueError(f"Alternate variants differ semantically: {event}")
        identity = {"reference_property": "ArticleBulkInfo.Articles", "reference_index": 0,
                    "expected_array_count": 1, "expected_package_name_value": primary["package"],
                    "expected_package_name_number": 0, "expected_asset_name_value": primary["asset"],
                    "expected_asset_name_number": 0}
        slot = {"slot_id": "companion:" + event, "location_kind": "scenario_reward",
                "content_class": "DLC" if "/DLC/" in primary["package"] else "BASE",
                "virtual_package": primary["virtual_package"], "export_index": primary["export_index"],
                "export_name": primary["export_name"], "bulk_index": None, "reward_index": None,
                "original_blood_code_package": primary["package"], "original_blood_code_asset": primary["asset"],
                "count": None, "b_notification": None,
                "display_name": "Companion Blood Code - " + primary["asset"].removeprefix("DA_BloodCode_"),
                **identity,
                "mirrored_references": [{key: mirror[key] for key in ("virtual_package", "export_index", "export_name")}
                                        for mirror in physical[1:]]}
        existing.append(slot)
        for target in physical:
            header = research / "source" / "Content" / (target["virtual_package"][6:] + ".uasset")
            manifest["source_package_sha256"][target["virtual_package"]] = {
                suffix: hashlib.sha256(header.with_suffix(suffix).read_bytes()).hexdigest()
                for suffix in (".uasset", ".uexp")}
    for name in ("BP_SmallScenarioBase", "BP_SmallScenarioGainBloodCodeBase"):
        package = "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Base/" + name
        header = research / "source" / "Content" / (package[6:] + ".uasset")
        manifest["source_package_sha256"][package] = {
            suffix: hashlib.sha256(header.with_suffix(suffix).read_bytes()).hexdigest()
            for suffix in (".uasset", ".uexp")}
    catalog["slots"] = existing
    payload = (json.dumps(catalog, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    (data / "blood_code_grants.json").write_bytes(payload)
    manifest["files"]["blood_code_grants.json"] = {"count": len(existing), "sha256": hashlib.sha256(payload).hexdigest()}
    manifest["content_class_counts"] = dict(Counter(slot["content_class"] for slot in existing))
    manifest["source_scan"]["companion_logical_events"] = len(groups)
    manifest["source_scan"]["companion_physical_packages"] = sum(map(len, groups.values()))
    (data / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Imported {len(groups)} companion events; {len(existing)} total logical locations.")


if __name__ == "__main__":
    main()
