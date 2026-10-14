from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cv2randomizer.game.blood_code_catalog import BloodCodeObjectReferenceTarget, BloodCodeReferenceTarget
from cv2randomizer.game.blood_code_generation import (
    BloodCodeGrantSnapshot,
    BloodCodeObjectReferenceSnapshot,
    BloodCodeReferenceSnapshot,
)
from cv2randomizer.generation.pipeline import GenerationError, _validate_final_blood_code_artifact


class _Serializer:
    # Initialize this object
    def __init__(self, snapshots: tuple[BloodCodeGrantSnapshot, ...]) -> None:
        self.snapshots = snapshots

    # Inspect packages
    def inspect_packages(self, **_kwargs):
        return self.snapshots


class _DirectAndLinkedReferenceSerializer(_Serializer):
    # Initialize this object
    def __init__(self, snapshots, reference_snapshots) -> None:
        super().__init__(snapshots)
        self.reference_snapshots = reference_snapshots
        self.reference_targets = ()

    # Inspect reference packages
    def inspect_reference_packages(self, **kwargs):
        self.reference_targets = kwargs["targets"]
        return self.reference_snapshots


class _ObjectReferenceSerializer:
    # Initialize this object
    def __init__(self, snapshot: BloodCodeObjectReferenceSnapshot) -> None:
        self.snapshot = snapshot
        self.targets = ()

    # Inspect object reference packages
    def inspect_object_reference_packages(self, **kwargs):
        self.targets = kwargs["targets"]
        return (self.snapshot,)


class _LinkedReferenceSerializer:
    # Initialize this object
    def __init__(self, object_snapshot, reference_snapshots) -> None:
        self.object_snapshot = object_snapshot
        self.reference_snapshots = reference_snapshots
        self.object_targets = ()
        self.reference_targets = ()

    # Inspect object reference packages
    def inspect_object_reference_packages(self, **kwargs):
        self.object_targets = kwargs["targets"]
        return (self.object_snapshot,)

    # Inspect reference packages
    def inspect_reference_packages(self, **kwargs):
        self.reference_targets = kwargs["targets"]
        return self.reference_snapshots


# Read fixture
def _fixture(tmp_path: Path, reward_package: str, reward_asset: str):
    virtual_package = "/Game/Quest/Beginning/QST_StartingBloodCode"
    assigned_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_BossZenon"
    assigned_asset = "DA_BloodCode_BossZenon"
    destination = SimpleNamespace(
        slot_id="starting_blood_code",
        location_kind="gain_item",
        virtual_package=virtual_package,
        export_index=7,
        original_blood_code_package="/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01",
        original_blood_code_asset="DA_BloodCode_Lou_01",
        bulk_index=0,
        reward_index=0,
        count=1,
        b_notification=True,
    )
    source = SimpleNamespace(
        original_blood_code_package=assigned_package,
        original_blood_code_asset=assigned_asset,
    )
    assignment = SimpleNamespace(destination=destination, source=source)
    result = SimpleNamespace(
        plan=SimpleNamespace(assignments=(assignment,)),
        patched_packages=(virtual_package,),
    )
    output_root = tmp_path / "staging" / "Content"
    header = output_root / "Quest" / "Beginning" / "QST_StartingBloodCode.uasset"
    export = header.with_suffix(".uexp")
    header.parent.mkdir(parents=True)
    header.write_bytes(b"uasset")
    export.write_bytes(b"uexp")
    snapshot = BloodCodeGrantSnapshot(
        virtual_package=virtual_package,
        export_index=7,
        export_name="QSTask_GainItem_0",
        bulk_index=0,
        reward_index=0,
        reward_package=reward_package,
        reward_asset=reward_asset,
        count=1,
        notification=True,
        package_name_value=reward_package,
        package_name_number=0,
        asset_name_value=reward_asset,
        asset_name_number=0,
        sub_path_string="",
        articles_count=1,
    )
    return result, output_root, (header, export), snapshot


# Check final blood code validation reopens
def test_final_blood_code_validation_reopens_randomized_staged_grant(tmp_path: Path) -> None:
    expected_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_BossZenon"
    expected_asset = "DA_BloodCode_BossZenon"
    result, output_root, output_files, snapshot = _fixture(tmp_path, expected_package, expected_asset)

    validation = _validate_final_blood_code_artifact(
        result,
        output_files=output_files,
        output_content_root=output_root,
        work_root=tmp_path,
        mappings_path=tmp_path / "CV2.usmap",
        serializer=_Serializer((snapshot,)),
    )

    assert validation == {
        "logical_assignments": 1,
        "direct_slots": 1,
        "reference_slots": 0,
        "object_reference_slots": 0,
        "packages": 1,
    }


# Check final validation include linked routes
def test_final_validation_includes_linked_routes_of_changed_gain_item(tmp_path: Path) -> None:
    expected_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_BossZenon"
    expected_asset = "DA_BloodCode_BossZenon"
    result, output_root, output_files, grant_snapshot = _fixture(
        tmp_path, expected_package, expected_asset,
    )
    route_package = "/Game/Cinematics/Scenario/BP_LouDisp_Past"
    target = BloodCodeReferenceTarget(
        virtual_package=route_package,
        export_index=12,
        export_name="BP_LouDisp_Past",
        reference_property="Articles",
        reference_index=0,
        expected_array_count=1,
        expected_package_name_value="/Game/Blueprint/Article/BloodCode/DA_BloodCode_LouDisp_02",
        expected_package_name_number=0,
        expected_asset_name_value="DA_BloodCode_LouDisp_02",
        expected_asset_name_number=0,
    )
    result.plan.assignments[0].destination.physical_reference_targets = (target,)
    result.patched_packages = (
        result.plan.assignments[0].destination.virtual_package,
        route_package,
    )
    route_header = output_root.joinpath(*route_package.split("/")[2:]).with_suffix(".uasset")
    route_header.parent.mkdir(parents=True, exist_ok=True)
    route_header.write_bytes(b"route uasset")
    route_header.with_suffix(".uexp").write_bytes(b"route uexp")
    reference_snapshot = BloodCodeReferenceSnapshot(
        virtual_package=route_package,
        export_index=12,
        export_name="BP_LouDisp_Past",
        reference_property="Articles",
        reference_index=0,
        array_count=1,
        reward_package=expected_package,
        reward_asset=expected_asset,
        package_name_value=expected_package,
        package_name_number=0,
        asset_name_value=expected_asset,
        asset_name_number=0,
        sub_path_string="",
        package_name_map_index=4,
        asset_name_map_index=6,
    )
    serializer = _DirectAndLinkedReferenceSerializer((grant_snapshot,), (reference_snapshot,))

    validation = _validate_final_blood_code_artifact(
        result,
        output_files=(*output_files, route_header, route_header.with_suffix(".uexp")),
        output_content_root=output_root,
        work_root=tmp_path,
        mappings_path=tmp_path / "CV2.usmap",
        serializer=serializer,
    )

    assert serializer.reference_targets == (target,)
    assert validation == {
        "logical_assignments": 1,
        "direct_slots": 1,
        "reference_slots": 1,
        "object_reference_slots": 0,
        "packages": 2,
    }


# Check final blood code validation fail
def test_final_blood_code_validation_fails_if_later_stage_restored_vanilla(tmp_path: Path) -> None:
    expected_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_BossZenon"
    expected_asset = "DA_BloodCode_BossZenon"
    result, output_root, output_files, _snapshot = _fixture(
        tmp_path,
        "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01",
        "DA_BloodCode_Lou_01",
    )
    snapshot = BloodCodeGrantSnapshot(
        virtual_package="/Game/Quest/Beginning/QST_StartingBloodCode",
        export_index=7,
        export_name="QSTask_GainItem_0",
        bulk_index=0,
        reward_index=0,
        reward_package="/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01",
        reward_asset="DA_BloodCode_Lou_01",
        count=1,
        notification=True,
        package_name_value="/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01",
        package_name_number=0,
        asset_name_value="DA_BloodCode_Lou_01",
        asset_name_number=0,
        sub_path_string="",
        articles_count=1,
    )

    with pytest.raises(GenerationError, match="assignment was lost or changed"):
        _validate_final_blood_code_artifact(
            result,
            output_files=output_files,
            output_content_root=output_root,
            work_root=tmp_path,
            mappings_path=tmp_path / "CV2.usmap",
            serializer=_Serializer((snapshot,)),
        )


# Check final buddy blood code validation
def test_final_buddy_blood_code_validation_inspects_assigned_import(tmp_path: Path) -> None:
    virtual_package = "/Game/Blueprint/Character/Companion/DA_BuddySettings_Noah"
    original_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Noah_02"
    original_asset = "DA_BloodCode_Noah_02"
    assigned_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_BossZenon"
    assigned_asset = "DA_BloodCode_BossZenon"
    target = BloodCodeObjectReferenceTarget(
        virtual_package=virtual_package,
        export_index=4,
        export_name="DA_BuddySettings_Noah",
        reference_property="BloodCode",
        property_schema_index=6,
        expected_import_package=original_package,
        expected_import_asset=original_asset,
        expected_package_name_number=0,
        expected_asset_name_number=0,
        expected_import_class_name="BloodCodeArticleData",
        expected_import_class_package="/Script/Extensions",
    )
    destination = SimpleNamespace(
        slot_id="noah_tier_2",
        location_kind="buddy_settings_reference",
        virtual_package=virtual_package,
        export_index=4,
        original_blood_code_package=original_package,
        original_blood_code_asset=original_asset,
        physical_reference_targets=(),
        physical_object_reference_targets=(target,),
    )
    source = SimpleNamespace(
        original_blood_code_package=assigned_package,
        original_blood_code_asset=assigned_asset,
    )
    result = SimpleNamespace(
        plan=SimpleNamespace(assignments=(SimpleNamespace(destination=destination, source=source),)),
        patched_packages=(virtual_package,),
    )
    output_root = tmp_path / "staging" / "Content"
    header = output_root / "Blueprint" / "Character" / "Companion" / "DA_BuddySettings_Noah.uasset"
    export = header.with_suffix(".uexp")
    header.parent.mkdir(parents=True)
    header.write_bytes(b"uasset")
    export.write_bytes(b"uexp")
    snapshot = BloodCodeObjectReferenceSnapshot(
        virtual_package=virtual_package,
        export_index=4,
        export_name="DA_BuddySettings_Noah",
        reference_property="BloodCode",
        property_schema_index=6,
        import_index=5,
        package_import_index=2,
        import_class_name="BloodCodeArticleData",
        import_class_package="/Script/Extensions",
        reward_package=assigned_package,
        package_name_number=0,
        reward_asset=assigned_asset,
        asset_name_number=0,
        owner_exports=(4,),
    )
    serializer = _ObjectReferenceSerializer(snapshot)

    validation = _validate_final_blood_code_artifact(
        result,
        output_files=(header, export),
        output_content_root=output_root,
        work_root=tmp_path,
        mappings_path=tmp_path / "CV2.usmap",
        serializer=serializer,
    )

    assert serializer.targets[0].expected_import_package == assigned_package
    assert serializer.targets[0].expected_import_asset == assigned_asset
    assert validation == {
        "logical_assignments": 1,
        "direct_slots": 0,
        "reference_slots": 0,
        "object_reference_slots": 1,
        "packages": 1,
    }


# Check final early blood code validation
def test_final_early_blood_code_validation_reopens_owner_and_linked_proficiency_refs(tmp_path: Path) -> None:
    assigned_package = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_BossZenon"
    assigned_asset = "DA_BloodCode_BossZenon"
    buddy_package = "/Game/Blueprint/Characters/NPC/Buddy/BuddyCharacters/noah/DA_BuddySettings_Noah"
    article_original = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Noah_01"
    object_target = BloodCodeObjectReferenceTarget(
        virtual_package=buddy_package,
        export_index=1,
        export_name="DA_BuddySettings_Noah",
        reference_property="BloodCode",
        property_schema_index=6,
        expected_import_package=article_original,
        expected_import_asset="DA_BloodCode_Noah_01",
        expected_package_name_number=0,
        expected_asset_name_number=0,
        expected_import_class_name="BloodCodeArticleData",
        expected_import_class_package="/Script/Extensions",
    )
    linked_targets = tuple(BloodCodeReferenceTarget(
        virtual_package=f"/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_{episode}",
        export_index=export_index,
        export_name="QSTask_BloodCodeAddProficiency_0",
        reference_property="BloodCodeArticleDatas",
        reference_index=1,
        expected_array_count=3,
        expected_package_name_value=article_original,
        expected_package_name_number=0,
        expected_asset_name_value="DA_BloodCode_Noah_01",
        expected_asset_name_number=0,
    ) for episode, export_index in (("e10210", 40), ("e10250", 49)))
    destination = SimpleNamespace(
        slot_id="noah_01_buddy_settings",
        location_kind="buddy_settings_reference",
        virtual_package=buddy_package,
        export_index=1,
        original_blood_code_package=article_original,
        original_blood_code_asset="DA_BloodCode_Noah_01",
        physical_object_reference_targets=(object_target,),
        physical_reference_targets=linked_targets,
    )
    source = SimpleNamespace(
        original_blood_code_package=assigned_package,
        original_blood_code_asset=assigned_asset,
    )
    assignment = SimpleNamespace(destination=destination, source=source)
    packages = (buddy_package, *(target.virtual_package for target in linked_targets))
    result = SimpleNamespace(plan=SimpleNamespace(assignments=(assignment,)), patched_packages=packages)
    output_root = tmp_path / "staging" / "Content"
    output_files = []
    for virtual in packages:
        header = output_root.joinpath(*virtual.split("/")[2:]).with_suffix(".uasset")
        header.parent.mkdir(parents=True, exist_ok=True)
        sidecar = header.with_suffix(".uexp")
        header.write_bytes(b"uasset")
        sidecar.write_bytes(b"uexp")
        output_files.extend((header, sidecar))
    object_snapshot = BloodCodeObjectReferenceSnapshot(
        virtual_package=buddy_package,
        export_index=1,
        export_name="DA_BuddySettings_Noah",
        reference_property="BloodCode",
        property_schema_index=6,
        import_index=4,
        package_import_index=2,
        import_class_name="BloodCodeArticleData",
        import_class_package="/Script/Extensions",
        reward_package=assigned_package,
        package_name_number=0,
        reward_asset=assigned_asset,
        asset_name_number=0,
        owner_exports=(1,),
    )
    reference_snapshots = tuple(BloodCodeReferenceSnapshot(
        virtual_package=target.virtual_package,
        export_index=target.export_index,
        export_name=target.export_name,
        reference_property=target.reference_property,
        reference_index=target.reference_index,
        array_count=target.expected_array_count,
        reward_package=assigned_package,
        reward_asset=assigned_asset,
        package_name_value=assigned_package,
        package_name_number=0,
        asset_name_value=assigned_asset,
        asset_name_number=0,
        sub_path_string="",
        package_name_map_index=5,
        asset_name_map_index=7,
    ) for target in linked_targets)
    serializer = _LinkedReferenceSerializer(object_snapshot, reference_snapshots)

    validation = _validate_final_blood_code_artifact(
        result,
        output_files=tuple(output_files),
        output_content_root=output_root,
        work_root=tmp_path,
        mappings_path=tmp_path / "CV2.usmap",
        serializer=serializer,
    )

    assert len(serializer.object_targets) == 1
    assert len(serializer.reference_targets) == 2
    assert validation == {
        "logical_assignments": 1,
        "direct_slots": 0,
        "reference_slots": 2,
        "object_reference_slots": 1,
        "packages": 3,
    }
