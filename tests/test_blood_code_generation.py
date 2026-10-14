from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

from cv2randomizer.game.asset_extraction import MaterializedAsset
from cv2randomizer.game.blood_code_catalog import (
    BloodCodeObjectReferenceTarget,
    BloodCodeReferenceTarget,
)
from cv2randomizer.game.blood_code_generation import apply_blood_code_plan


class _RecordingSerializer:
    # Initialize this object
    def __init__(self) -> None:
        self.direct_requests = {}
        self.object_requests = {}
        self.reference_requests = {}

    # Patch package reward data
    def patch_packages(self, *, patch_packages, **_kwargs):
        self.direct_requests = patch_packages
        return self._write_outputs(patch_packages, "patchedSlots")

    # Patch object reference packages
    def patch_object_reference_packages(self, *, patch_packages, **_kwargs):
        self.object_requests = patch_packages
        return self._write_outputs(patch_packages, "patchedReferences")

    # Patch reference packages
    def patch_reference_packages(self, *, patch_packages, **_kwargs):
        self.reference_requests = patch_packages
        return self._write_outputs(patch_packages, "patchedReferences")

    # Write outputs
    @staticmethod
    def _write_outputs(patch_packages, count_key):
        results = []
        for virtual, (source, output, assignments) in patch_packages.items():
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
            output_files = [output]
            source_export = source.with_suffix(".uexp")
            if source_export.is_file():
                target_export = output.with_suffix(".uexp")
                shutil.copy2(source_export, target_export)
                output_files.append(target_export)
            results.append({
                "virtualPackage": virtual,
                "inputFile": str(source),
                "outputFile": str(output),
                count_key: len(assignments),
                "outputFiles": [str(path) for path in output_files],
            })
        return results


# Check buddy blood code assignment patches
def test_buddy_blood_code_assignment_patches_owner_and_linked_proficiency_packages(tmp_path: Path) -> None:
    original_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Noah_01"
    assigned_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_BossZenon"
    buddy_package = "/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/noah/DA_BuddySettings_Noah"
    quest_packages = (
        "/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10210",
        "/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10250",
    )
    buddy_target = BloodCodeObjectReferenceTarget(
        virtual_package=buddy_package,
        export_index=1,
        export_name="DA_BuddySettings_Noah",
        reference_property="BloodCode",
        property_schema_index=6,
        expected_import_package=original_package,
        expected_import_asset="DA_BloodCode_Noah_01",
        expected_package_name_number=0,
        expected_asset_name_number=0,
        expected_import_class_name="BloodCodeArticleData",
        expected_import_class_package="/Script/Extensions",
    )
    linked_targets = tuple(
        BloodCodeReferenceTarget(
            virtual_package=package,
            export_index=export_index,
            export_name="QSTask_BloodCodeAddProficiency_0",
            reference_property="BloodCodeArticleDatas",
            reference_index=1,
            expected_array_count=3,
            expected_package_name_value=original_package,
            expected_package_name_number=0,
            expected_asset_name_value="DA_BloodCode_Noah_01",
            expected_asset_name_number=0,
        )
        for package, export_index in zip(quest_packages, (40, 49), strict=True)
    )
    destination = SimpleNamespace(
        slot_id="noah_01_buddy_settings",
        location_kind="buddy_settings_reference",
        virtual_package=buddy_package,
        export_index=1,
        original_blood_code_package=original_package,
        original_blood_code_asset="DA_BloodCode_Noah_01",
        physical_object_reference_targets=(buddy_target,),
        physical_reference_targets=linked_targets,
    )
    source = SimpleNamespace(
        original_blood_code_package=assigned_package,
        original_blood_code_asset="DA_BloodCode_BossZenon",
    )
    assignment = SimpleNamespace(destination=destination, source=source)

    root = tmp_path / "work"
    source_root = root / "source" / "Content"
    assets = []
    for virtual in (buddy_package, *quest_packages):
        header = source_root.joinpath(*virtual.split("/")[2:]).with_suffix(".uasset")
        header.parent.mkdir(parents=True, exist_ok=True)
        sidecar = header.with_suffix(".uexp")
        header.write_bytes(b"header")
        sidecar.write_bytes(b"export")
        assets.append(MaterializedAsset(virtual, (header, sidecar)))
    root.mkdir(parents=True, exist_ok=True)
    mappings = root / "CV2.usmap"
    mappings.write_bytes(b"test mappings")
    output_root = root / "staging" / "Content"
    serializer = _RecordingSerializer()

    result = apply_blood_code_plan(
        SimpleNamespace(plan=SimpleNamespace(assignments=(assignment,)), source_assets=tuple(assets)),
        work_root=root,
        output_content_root=output_root,
        mappings_path=mappings,
        serializer=serializer,
    )

    assert set(serializer.object_requests) == {buddy_package}
    assert set(serializer.reference_requests) == set(quest_packages)
    assert set(result.patched_packages) == {buddy_package, *quest_packages}
    assert all(
        request[2][0]["assignedPackage"] == assigned_package
        for request in serializer.reference_requests.values()
    )


# Check lavinia tier one assignment patches
def test_lavinia_tier_one_assignment_patches_buddy_and_both_mag_mell_grants(tmp_path: Path) -> None:
    original_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lavinia_01"
    assigned_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_DeadSentiments"
    buddy_package = "/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/Lavinia/DA_BuddySettings_Lavinia"
    scenario_targets = (
        BloodCodeReferenceTarget(
            virtual_package="/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/CharacterEpisode/CharacterEpisodeLavinia/Present_c08010/BP_SmallScenario_Lavinia_Present_c08010_B",
            export_index=2,
            export_name="Default__BP_SmallScenario_Lavinia_Present_c08010_B_C",
            reference_property="ArticleBulkInfo.Articles",
            reference_index=0,
            expected_array_count=1,
            expected_package_name_value=original_package,
            expected_package_name_number=0,
            expected_asset_name_value="DA_BloodCode_Lavinia_01",
            expected_asset_name_number=0,
            expected_super_class="BP_SmallScenarioGainBoosterBase_C",
        ),
        BloodCodeReferenceTarget(
            virtual_package="/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/CharacterEpisode/CharacterEpisodeLavinia/Present_c08030/BP_SmallScenario_Lavinia_Present_c08030_B",
            export_index=2,
            export_name="Default__BP_SmallScenario_Lavinia_Present_c08030_B_C",
            reference_property="ArticleBulkInfo.Articles",
            reference_index=0,
            expected_array_count=1,
            expected_package_name_value=original_package,
            expected_package_name_number=0,
            expected_asset_name_value="DA_BloodCode_Lavinia_01",
            expected_asset_name_number=0,
            expected_super_class="BP_SmallScenarioBase_C",
        ),
    )
    buddy_target = BloodCodeObjectReferenceTarget(
        virtual_package=buddy_package,
        export_index=1,
        export_name="DA_BuddySettings_Lavinia",
        reference_property="BloodCode",
        property_schema_index=6,
        expected_import_package=original_package,
        expected_import_asset="DA_BloodCode_Lavinia_01",
        expected_package_name_number=0,
        expected_asset_name_number=0,
        expected_import_class_name="BloodCodeArticleData",
        expected_import_class_package="/Script/Extensions",
    )
    destination = SimpleNamespace(
        slot_id="buddy:/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/Lavinia/DA_BuddySettings_Lavinia::export=1::BloodCode",
        location_kind="buddy_settings_reference",
        virtual_package=buddy_package,
        export_index=1,
        original_blood_code_package=original_package,
        original_blood_code_asset="DA_BloodCode_Lavinia_01",
        physical_object_reference_targets=(buddy_target,),
        physical_reference_targets=scenario_targets,
    )
    source = SimpleNamespace(
        original_blood_code_package=assigned_package,
        original_blood_code_asset="DA_BloodCode_DeadSentiments",
    )
    root = tmp_path / "work"
    source_root = root / "source" / "Content"
    assets = []
    packages = (buddy_package, *(target.virtual_package for target in scenario_targets))
    for virtual in packages:
        header = source_root.joinpath(*virtual.split("/")[2:]).with_suffix(".uasset")
        header.parent.mkdir(parents=True, exist_ok=True)
        sidecar = header.with_suffix(".uexp")
        header.write_bytes(b"header")
        sidecar.write_bytes(b"export")
        assets.append(MaterializedAsset(virtual, (header, sidecar)))
    mappings = root / "CV2.usmap"
    mappings.write_bytes(b"test mappings")
    serializer = _RecordingSerializer()

    result = apply_blood_code_plan(
        SimpleNamespace(
            plan=SimpleNamespace(assignments=(SimpleNamespace(destination=destination, source=source),)),
            source_assets=tuple(assets),
        ),
        work_root=root,
        output_content_root=root / "staging" / "Content",
        mappings_path=mappings,
        serializer=serializer,
    )

    assert set(serializer.object_requests) == {buddy_package}
    assert set(serializer.reference_requests) == {target.virtual_package for target in scenario_targets}
    assert set(result.patched_packages) == set(packages)
    assert all(
        request[2][0]["assignedPackage"] == assigned_package
        and request[2][0]["referenceProperty"] == "ArticleBulkInfo.Articles"
        and request[2][0]["expectedSuperClass"] == target.expected_super_class
        for target in scenario_targets
        for request in (serializer.reference_requests[target.virtual_package],)
    )


# Check lou disp logical assignment patches
def test_lou_disp_logical_assignment_patches_quest_and_both_alternate_scenario_routes(tmp_path: Path) -> None:
    original_package = "/Game/Blueprint/Article/BloodCode/DLC/DA_BloodCode_LouDisp_02"
    assigned_package = "/Game/Blueprint/Article/BloodCode/DLC/DA_BloodCode_DeadSentiments"
    quest_package = "/Game/Quest/DLC/QST_MainEpisode_DLC_GainBC_LouDisp"
    scenario_targets = (
        BloodCodeReferenceTarget(
            virtual_package="/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/BloodCode/Past_a20180/BP_SmallScenario_Lou_Disp_Past_a20180_01",
            export_index=2,
            export_name="Default__BP_SmallScenario_Lou_Disp_Past_a20180_01_C",
            reference_property="ArticleBulkInfo.Articles",
            reference_index=0,
            expected_array_count=1,
            expected_package_name_value=original_package,
            expected_package_name_number=0,
            expected_asset_name_value="DA_BloodCode_LouDisp_02",
            expected_asset_name_number=0,
            expected_super_class="BP_SmallScenarioGainBloodCodeBase_C",
        ),
        BloodCodeReferenceTarget(
            virtual_package="/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/BloodCode/Past_a20180/BP_SmallScenario_Lou_Disp_Past_a20180_01_Yadorigi",
            export_index=2,
            export_name="Default__BP_SmallScenario_Lou_Disp_Past_a20180_01_Yadorigi_C",
            reference_property="ArticleBulkInfo.Articles",
            reference_index=0,
            expected_array_count=1,
            expected_package_name_value=original_package,
            expected_package_name_number=0,
            expected_asset_name_value="DA_BloodCode_LouDisp_02",
            expected_asset_name_number=0,
            expected_super_class="BP_SmallScenarioGainBloodCodeBase_C",
        ),
    )
    destination = SimpleNamespace(
        slot_id=f"{quest_package}::export=15::bulk=0::article=0",
        location_kind="gain_item",
        virtual_package=quest_package,
        export_index=15,
        export_name="QSTask_GainItem_0",
        bulk_index=0,
        reward_index=0,
        count=1,
        b_notification=True,
        original_blood_code_package=original_package,
        original_blood_code_asset="DA_BloodCode_LouDisp_02",
        physical_object_reference_targets=(),
        physical_reference_targets=scenario_targets,
    )
    source = SimpleNamespace(
        original_blood_code_package=assigned_package,
        original_blood_code_asset="DA_BloodCode_DeadSentiments",
    )
    root = tmp_path / "work"
    source_root = root / "source" / "Content"
    packages = (quest_package, *(target.virtual_package for target in scenario_targets))
    assets = []
    for virtual in packages:
        header = source_root.joinpath(*virtual.split("/")[2:]).with_suffix(".uasset")
        header.parent.mkdir(parents=True, exist_ok=True)
        sidecar = header.with_suffix(".uexp")
        header.write_bytes(b"header")
        sidecar.write_bytes(b"export")
        assets.append(MaterializedAsset(virtual, (header, sidecar)))
    mappings = root / "CV2.usmap"
    mappings.write_bytes(b"test mappings")
    serializer = _RecordingSerializer()

    result = apply_blood_code_plan(
        SimpleNamespace(
            plan=SimpleNamespace(assignments=(SimpleNamespace(destination=destination, source=source),)),
            source_assets=tuple(assets),
        ),
        work_root=root,
        output_content_root=root / "staging" / "Content",
        mappings_path=mappings,
        serializer=serializer,
    )

    assert set(serializer.direct_requests) == {quest_package}
    assert set(serializer.reference_requests) == {target.virtual_package for target in scenario_targets}
    assert set(result.patched_packages) == set(packages)
    assert all(
        request[2][0]["assignedPackage"] == assigned_package
        and request[2][0]["referenceProperty"] == "ArticleBulkInfo.Articles"
        for request in serializer.reference_requests.values()
    )
