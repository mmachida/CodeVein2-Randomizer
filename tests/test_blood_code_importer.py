from __future__ import annotations

import json
from pathlib import Path

from tools.import_buddy_blood_code_rewards import import_buddy_rewards


# Check importer promotes proficiency reference actual
def test_importer_promotes_proficiency_reference_to_actual_buddy_owner(tmp_path: Path) -> None:
    reward = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Noah_01"
    quest_10210 = "/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10210"
    quest_10250 = "/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10250"
    buddy_package = "/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/noah/DA_BuddySettings_Noah"
    content_root = tmp_path / "Content"
    buddy_files = content_root / "Blueprint" / "Characters" / "NPC" / "Buddy" / "BuddyCharacters" / "noah"
    buddy_files.mkdir(parents=True)
    (buddy_files / "DA_BuddySettings_Noah.uasset").write_bytes(b"buddy-header")
    (buddy_files / "DA_BuddySettings_Noah.uexp").write_bytes(b"buddy-export")

    catalog_path = tmp_path / "blood_code_grants.json"
    manifest_path = tmp_path / "manifest.json"
    inspection_path = tmp_path / "buddy-settings-imports.json"
    inventory_path = tmp_path / "installed-blood-code-packages.json"
    placeholder = {
        "slot_id": "beginning_noah_proficiency",
        "location_kind": "proficiency_reference",
        "content_class": "BASE",
        "virtual_package": quest_10250,
        "export_index": 49,
        "export_name": "QSTask_BloodCodeAddProficiency_0",
        "bulk_index": None,
        "reward_index": None,
        "original_blood_code_package": reward,
        "original_blood_code_asset": "DA_BloodCode_Noah_01",
        "count": None,
        "b_notification": None,
        "display_name": "Starting Blood Code - Noah",
        "reference_property": "BloodCodeArticleDatas",
        "reference_index": 1,
        "expected_array_count": 3,
        "expected_package_name_value": reward,
        "expected_package_name_number": 0,
        "expected_asset_name_value": "DA_BloodCode_Noah_01",
        "expected_asset_name_number": 0,
        "mirrored_references": [{
            "virtual_package": quest_10210,
            "export_index": 40,
            "export_name": "QSTask_BloodCodeAddProficiency_0",
        }],
    }
    catalog_path.write_text(json.dumps({"catalog_revision": "current", "schema": "cv2-blood-code-grants-v1", "slots": [placeholder]}), encoding="utf-8")
    manifest_path.write_text(json.dumps({
        "schema": "cv2-blood-code-catalog-v1",
        "catalog_revision": "current",
        "files": {catalog_path.name: {"count": 1, "sha256": ""}},
        "content_class_counts": {"BASE": 1},
        "source_package_sha256": {quest_10210: {}, quest_10250: {}},
        "source_scan": {},
    }), encoding="utf-8")
    inspection_path.write_text(json.dumps([{
        "package": buddy_package,
        "exportIndex": 1,
        "exportName": "DA_BuddySettings_Noah",
        "className": "BuddySettings",
        "bloodCodeImports": [{
            "objectName": "DA_BloodCode_Noah_01",
            "objectNameNumber": 0,
            "className": "BloodCodeArticleData",
            "classPackage": "/Script/Extensions",
            "outerIndex": -1,
            "package": reward,
        }],
        "dependentExports": [{
            "exportIndex": 1,
            "exportName": "DA_BuddySettings_Noah",
            "dependencies": [-1],
        }],
    }]), encoding="utf-8")
    inventory_path.write_text(json.dumps([reward]), encoding="utf-8")

    converted = import_buddy_rewards(
        catalog_path=catalog_path,
        manifest_path=manifest_path,
        inspection_path=inspection_path,
        installed_inventory_path=inventory_path,
        content_root=content_root,
    )

    document = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert converted == 1
    assert len(document["slots"]) == 1
    slot = document["slots"][0]
    assert slot["location_kind"] == "buddy_settings_reference"
    assert slot["virtual_package"] == buddy_package
    assert slot["object_reference"]["expected_import_package"] == reward
    assert slot["display_name"] == "Noah_01 quest reward"
    linked = slot["linked_proficiency_references"]
    assert {(item["virtual_package"], item["reference_index"]) for item in linked} == {
        (quest_10210, 1), (quest_10250, 1),
    }
    assert all(item["reference_property"] == "BloodCodeArticleDatas" for item in linked)
    assert all(item["expected_array_count"] == 3 for item in linked)
    assert import_buddy_rewards(
        catalog_path=catalog_path,
        manifest_path=manifest_path,
        inspection_path=inspection_path,
        installed_inventory_path=inventory_path,
        content_root=content_root,
    ) == 0
