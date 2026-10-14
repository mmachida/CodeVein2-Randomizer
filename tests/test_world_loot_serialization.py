from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from cv2randomizer.game.fname_identity import FNameIdentity, SoftObjectPathIdentity
from cv2randomizer.game.world_loot_serialization import (
    ArticleReward,
    HealingUpgradePackagePatch,
    HealingUpgradePatch,
    WorldLootSerializationError,
    WorldLootSerializer,
    WorldLootPackagePatch,
    WorldLootTuplePatch,
)


class FakeRunner:
    # Initialize this object
    def __init__(self, *, exit_code: int = 0, valid_result: bool = True, raw_payload_preserved: bool = True) -> None:
        self.exit_code = exit_code
        self.valid_result = valid_result
        self.raw_payload_preserved = raw_payload_preserved
        self.command: list[str] | None = None

    # Call the wrapped operation
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.command = command
        operation = command.index("patch")
        output = Path(command[operation + 5])
        assigned = {
            "path": command[operation + 12],
            "count": int(command[operation + 13]),
            "notification": command[operation + 14] == "true",
        }
        original = {
            "path": command[operation + 9],
            "count": int(command[operation + 10]),
            "notification": command[operation + 11] == "true",
        }
        output_path = output
        if self.exit_code == 0:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"validated-header")
            output.with_suffix(".uexp").write_bytes(b"validated-export")
        result = {
            "exportName": command[operation + 8],
            "exportIndex": int(command[operation + 6]),
            "rewardIndex": int(command[operation + 7]),
            "uniqueSaveId": "preserved-id",
            "original": original,
            "assigned": assigned,
            "outputFile": str(output_path),
            "reopenedAndValidated": self.valid_result,
            "rawPayloadPreserved": self.raw_payload_preserved,
        }
        return subprocess.CompletedProcess(
            command,
            self.exit_code,
            json.dumps(result) + "\n",
            "helper error" if self.exit_code else "",
        )


class FakeBatchRunner:
    # Initialize this object
    def __init__(self, *, exit_code: int = 0, raw_payload_preserved: bool = True) -> None:
        self.exit_code = exit_code
        self.raw_payload_preserved = raw_payload_preserved
        self.command: list[str] | None = None
        self.commands: list[list[str]] = []
        self.package_counts: list[int] = []
        self.patch_counts: list[int] = []
        self.manifests: list[dict[str, object]] = []
        self.progress_metadata: list[tuple[int, int]] = []
        self.helper_environments: list[dict[str, str]] = []

    # Call the wrapped operation
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.command = command
        self.commands.append(command)
        self.helper_environments.append(dict(kwargs["env"]))
        manifest = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        self.manifests.append(manifest)
        self.package_counts.append(len(manifest["packages"]))
        self.patch_counts.append(len(manifest["packages"][0]["patches"]))
        self.progress_metadata.append((
            manifest["packages"][0]["packageIndex"],
            manifest["packages"][0]["packageCount"],
        ))
        results = []
        for package in manifest["packages"]:
            output = Path(package["outputFile"])
            if self.exit_code == 0:
                output.write_bytes(b"validated header")
                output.with_suffix(".uexp").write_bytes(b"validated exports")
            patches = []
            for patch in package["patches"]:
                patches.append({
                    "exportName": patch["exportName"],
                    "exportIndex": patch["exportIndex"],
                    "rewardIndex": patch["rewardIndex"],
                    "uniqueSaveId": "unchanged-save-id" if patch.get("requireUniqueSaveId", True) else "",
                    "original": patch["expected"],
                    "assigned": patch["assigned"],
                    "outputFile": str(output),
                    "reopenedAndValidated": True,
                })
            results.append({
                "inputFile": package["inputFile"],
                "outputFile": str(output),
                "patches": patches,
                "reopenedAndValidated": True,
                "rawPayloadPreserved": self.raw_payload_preserved,
            })
        return subprocess.CompletedProcess(command, self.exit_code, json.dumps(results) + "\n", "batch rejected" if self.exit_code else "")


class FakeHealingRunner:
    # Initialize this object
    def __init__(self, *, validated: bool = True) -> None:
        self.validated = validated
        self.manifest: dict[str, object] | None = None
        self.command: list[str] | None = None

    # Call the wrapped operation
    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.command = command
        self.manifest = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        results = []
        for package in self.manifest["packages"]:
            output = Path(package["outputFile"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"validated map")
            output.with_suffix(".uexp").write_bytes(b"preserved map payload")
            clones = []
            for patch in package["patches"]:
                clone = Path(patch["blueprintOutputFile"])
                clone.parent.mkdir(parents=True, exist_ok=True)
                clone.write_bytes(b"validated blueprint")
                clone.with_suffix(".uexp").write_bytes(b"patched cdo payload")
                clones.append({
                    "inputFile": patch["blueprintInputFile"],
                    "outputFile": str(clone),
                    "clonePackagePath": patch["clonePackagePath"],
                    "original": patch["expected"],
                    "assigned": patch["assigned"],
                    "reopenedAndValidated": self.validated,
                    "rawPayloadPreserved": self.validated,
                })
            results.append({
                "inputFile": package["inputFile"],
                "outputFile": str(output),
                "patchCount": len(package["patches"]),
                "clones": clones,
                "classInspections": [
                    {
                        "exportIndex": row["exportIndex"],
                        "exportName": row["exportName"],
                        "className": "BlueprintGeneratedClass",
                        "packagePath": "/Game/Environment/BP_Healing",
                    }
                    for row in package.get("classExports", [])
                ],
                "worldLootFields": [
                    {
                        "exportIndex": row["exportIndex"],
                        "rewardIndex": row["rewardIndex"],
                        "exportName": row["exportName"],
                        "offset": 0,
                        "expectedBytesHex": "00" * 16,
                        "available": True,
                        "unavailableReason": None,
                    }
                    for row in package.get("worldLootFields", [])
                ],
                "discoveryOpenCount": 1,
                "preparationReloadCount": 0,
                "serializationReopenCount": 1,
                "validationReopenCount": 1,
                "reopenedAndValidated": self.validated,
                "rawPayloadPreserved": self.validated,
            })
        return subprocess.CompletedProcess(command, 0, json.dumps(results) + "\n", "")


class ConcurrentFakeBatchRunner(FakeBatchRunner):
    # Initialize this object
    def __init__(self) -> None:
        super().__init__()
        self._activity_lock = threading.Lock()
        self._active_calls = 0
        self.maximum_active_calls = 0
        self.mapping_paths: list[Path] = []
        self.mapping_payloads: list[bytes] = []

    # Call the wrapped operation
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        with self._activity_lock:
            self._active_calls += 1
            self.maximum_active_calls = max(self.maximum_active_calls, self._active_calls)
            self.mapping_paths.append(Path(command[-3]))
            self.mapping_payloads.append(Path(command[-3]).read_bytes())
        try:
            time.sleep(0.05)
            return super().__call__(command, **kwargs)
        finally:
            with self._activity_lock:
                self._active_calls -= 1


class SelectiveFailureBatchRunner(FakeBatchRunner):
    # Call the wrapped operation
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        manifest = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        if manifest["packages"][0]["packageIndex"] > 1:
            return subprocess.CompletedProcess(command, 1, "", "deliberate test failure")
        return super().__call__(command, **kwargs)


class WorldLootSerializationTests(unittest.TestCase):
    # Prepare the package test fixture
    def _setup(self, root: Path) -> tuple[Path, Path, Path, Path, FakeRunner, WorldLootSerializer]:
        staging = root / "staging"
        package_dir = staging / "Content" / "Map" / "Generated"
        package_dir.mkdir(parents=True)
        source = package_dir / "sample.umap"
        source.write_bytes(b"input package")
        mappings = root / "CV2.usmap"
        mappings.write_bytes(b"mapping placeholder")
        project = root / "helper.csproj"
        project.write_text("test helper project", encoding="utf-8")
        runner = FakeRunner()
        serializer = WorldLootSerializer(helper_project=project, runner=runner)
        return staging, source, package_dir / "patched.umap", mappings, runner, serializer

    # List reward
    def _reward(self, name: str) -> ArticleReward:
        return ArticleReward(
            f"/Game/Blueprint/Article/Item/Support/{name}",
            1,
            True,
        )

    # List healing reward
    def _healing_reward(self, package: str, asset: str) -> ArticleReward:
        path = f"{package}_{1}"
        asset_name = f"{asset}_1"
        identity = SoftObjectPathIdentity(
            FNameIdentity(package, 2), FNameIdentity(asset, 2),
        )
        return ArticleReward(path, 1, True, identity)

    # Check healing helper require exactly validated
    def test_healing_helper_requires_exactly_validated_blueprint_clone_and_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            blueprint = staging / "Content" / "Environment" / "BP_Healing.uasset"
            blueprint.parent.mkdir(parents=True)
            blueprint.write_bytes(b"source blueprint")
            blueprint.with_suffix(".uexp").write_bytes(b"source payload")
            map_output = source.with_name("sample.cv2-healing.umap")
            clone_output = staging / "output" / "CV2Randomizer" / "Healing" / "sample_E8" / "BP_Healing.uasset"
            expected = self._healing_reward("/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryCountUp", "DA_Item_MatBasicRecoveryCountUp")
            assigned = self._healing_reward("/Game/Blueprint/Article/Item/Support/DA_Item_ChemicalLight", "DA_Item_ChemicalLight")
            patch = HealingUpgradePatch(
                8, "BP_Healing_C_UAID_example", "/Game/Environment/BP_Healing", "BP_Healing_C",
                2, "Default__BP_Healing_C", blueprint, clone_output,
                "/Game/CV2Randomizer/Healing/sample_E8/BP_Healing", expected, assigned,
            )
            runner = FakeHealingRunner()
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)

            result = serializer.patch_healing_upgrades(
                work_root=staging,
                mappings_path=mappings,
                packages=(HealingUpgradePackagePatch(
                    source, map_output, (patch,),
                    map_name_map_values=("/Game/Article/DA_Assigned", "DA_Assigned"),
                ),),
            )

            self.assertEqual(runner.command[runner.command.index("patch-healing-upgrades")], "patch-healing-upgrades")
            request = runner.manifest["packages"][0]
            self.assertEqual(request["mapNameMapValues"], ["/Game/Article/DA_Assigned", "DA_Assigned"])
            self.assertEqual(request["patches"][0]["expected"]["softObjectPathIdentity"]["packageName"]["number"], 2)
            self.assertEqual(result[0].patch_count, 1)
            self.assertTrue(result[0].reopened_and_validated)
            self.assertTrue(result[0].raw_payload_preserved)
            self.assertEqual(result[0].clones[0].assigned, assigned)
            self.assertTrue(map_output.is_file())
            self.assertTrue(clone_output.is_file())

    # Check healing helper discards outputs without
    def test_healing_helper_discards_outputs_without_reopen_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            blueprint = staging / "Content" / "Environment" / "BP_Healing.uasset"
            blueprint.parent.mkdir(parents=True)
            blueprint.write_bytes(b"source blueprint")
            map_output = source.with_name("sample.cv2-healing.umap")
            clone_output = staging / "output" / "BP_Healing.uasset"
            expected = self._healing_reward("/Game/Article/DA_Original", "DA_Original")
            assigned = self._healing_reward("/Game/Article/DA_Assigned", "DA_Assigned")
            patch = HealingUpgradePatch(
                8, "BP_Healing_C_UAID_example", "/Game/Environment/BP_Healing", "BP_Healing_C",
                2, "Default__BP_Healing_C", blueprint, clone_output,
                "/Game/CV2Randomizer/Healing/sample_E8/BP_Healing", expected, assigned,
            )
            serializer = WorldLootSerializer(
                helper_project=root / "helper.csproj", runner=FakeHealingRunner(validated=False),
            )

            with self.assertRaisesRegex(WorldLootSerializationError, "did not validate"):
                serializer.patch_healing_upgrades(
                    work_root=staging,
                    mappings_path=mappings,
                    packages=(HealingUpgradePackagePatch(source, map_output, (patch,)),),
                )

            self.assertFalse(map_output.exists())
            self.assertFalse(map_output.with_suffix(".uexp").exists())
            self.assertFalse(clone_output.exists())
            self.assertFalse(clone_output.with_suffix(".uexp").exists())

    # Check passes verified catalog identity complete
    def test_passes_verified_catalog_identity_and_complete_tuples_to_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, output, mappings, runner, serializer = self._setup(root)
            expected = self._reward("DA_Item_ChemicalLight_1")
            assigned = ArticleReward(
                "/Game/Blueprint/Article/Item/Growth/DA_Item_ExpA_12",
                3,
                False,
            )

            result = serializer.patch_reward(
                work_root=staging,
                input_asset=source,
                output_asset=output,
                mappings_path=mappings,
                export_index=8,
                reward_index=0,
                expected_export_name="BP_PickupItem_C_UAID_example",
                expected_original=expected,
                assigned=assigned,
            )

            assert runner.command is not None
            patch_index = runner.command.index("patch")
            self.assertEqual(runner.command[patch_index + 6:patch_index + 15], [
                "8", "0", "BP_PickupItem_C_UAID_example",
                expected.article_data_path, "1", "true",
                assigned.article_data_path, "3", "false",
            ])
            self.assertEqual(result.original, expected)
            self.assertEqual(result.assigned, assigned)
            self.assertEqual(result.export_index, 8)
            self.assertEqual(result.output_path, output.resolve())
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_suffix(".uexp").is_file())

    # Check reject any input output outside
    def test_rejects_any_input_or_output_outside_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, output, mappings, runner, serializer = self._setup(root)
            outside = root / "outside.umap"
            outside.write_bytes(b"outside")

            with self.assertRaisesRegex(WorldLootSerializationError, "inside the supplied staging root"):
                serializer.patch_reward(
                    work_root=staging,
                    input_asset=outside,
                    output_asset=output,
                    mappings_path=mappings,
                    export_index=1,
                    reward_index=0,
                    expected_export_name="pickup",
                    expected_original=self._reward("DA_Item_ChemicalLight_1"),
                    assigned=self._reward("DA_Item_ExpA_12"),
                )
            self.assertIsNone(runner.command)

    # Check refuses existing output bad catalog
    def test_refuses_existing_output_and_bad_catalog_indices_before_running_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, output, mappings, runner, serializer = self._setup(root)
            output.write_bytes(b"do not overwrite")
            with self.assertRaisesRegex(WorldLootSerializationError, "overwrite"):
                serializer.patch_reward(
                    work_root=staging,
                    input_asset=source,
                    output_asset=output,
                    mappings_path=mappings,
                    export_index=1,
                    reward_index=0,
                    expected_export_name="pickup",
                    expected_original=self._reward("DA_Item_ChemicalLight_1"),
                    assigned=self._reward("DA_Item_ExpA_12"),
                )
            self.assertIsNone(runner.command)
            output.unlink()
            with self.assertRaisesRegex(WorldLootSerializationError, "positive Unreal 1-based"):
                serializer.patch_reward(
                    work_root=staging,
                    input_asset=source,
                    output_asset=output,
                    mappings_path=mappings,
                    export_index=0,
                    reward_index=0,
                    expected_export_name="pickup",
                    expected_original=self._reward("DA_Item_ChemicalLight_1"),
                    assigned=self._reward("DA_Item_ExpA_12"),
                )
            self.assertIsNone(runner.command)

    # Check helper failure is reported fail
    def test_helper_failure_is_reported_as_fail_closed_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, output, mappings, _, _ = self._setup(root)
            project = root / "helper.csproj"
            runner = FakeRunner(exit_code=1)
            serializer = WorldLootSerializer(helper_project=project, runner=runner)
            with self.assertRaisesRegex(WorldLootSerializationError, "helper rejected"):
                serializer.patch_reward(
                    work_root=staging,
                    input_asset=source,
                    output_asset=output,
                    mappings_path=mappings,
                    export_index=8,
                    reward_index=0,
                    expected_export_name="pickup",
                    expected_original=self._reward("DA_Item_ChemicalLight_1"),
                    assigned=self._reward("DA_Item_ExpA_12"),
                )

    # Check reject malformed helper validation result
    def test_rejects_malformed_helper_validation_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, output, mappings, _, _ = self._setup(root)
            runner = FakeRunner(valid_result=False)
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)
            with self.assertRaisesRegex(WorldLootSerializationError, "invalid validation output"):
                serializer.patch_reward(
                    work_root=staging,
                    input_asset=source,
                    output_asset=output,
                    mappings_path=mappings,
                    export_index=8,
                    reward_index=0,
                    expected_export_name="pickup",
                    expected_original=self._reward("DA_Item_ChemicalLight_1"),
                    assigned=self._reward("DA_Item_ExpA_12"),
                )
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".uexp").exists())

    # Check single patch reject helper without
    def test_single_patch_rejects_helper_without_raw_payload_preservation_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, output, mappings, _, _ = self._setup(root)
            runner = FakeRunner(raw_payload_preserved=False)
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)

            with self.assertRaisesRegex(WorldLootSerializationError, "invalid validation output"):
                serializer.patch_reward(
                    work_root=staging,
                    input_asset=source,
                    output_asset=output,
                    mappings_path=mappings,
                    export_index=8,
                    reward_index=0,
                    expected_export_name="pickup",
                    expected_original=self._reward("DA_Item_ChemicalLight_1"),
                    assigned=self._reward("DA_Item_ExpA_12"),
                )

            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".uexp").exists())

    # Check reward tuple require canonical positive
    def test_reward_tuple_requires_canonical_positive_data(self) -> None:
        with self.assertRaises(ValueError):
            ArticleReward("/Game/Item/../Unsafe", 1, True)
        with self.assertRaises(ValueError):
            ArticleReward("/Game/Item/DA_Item", 0, True)
        with self.assertRaises(ValueError):
            ArticleReward("/Game/Item/DA_Item", 1, 1)  # type: ignore[arg-type]

    # Check patches all requested exports one
    def test_patches_all_requested_exports_in_one_package_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            output = source.with_name("sample.cv2-patched.umap")
            runner = FakeBatchRunner()
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)
            original = self._reward("DA_Item_ChemicalLight_1")
            assigned = self._reward("DA_Item_ExpA_12")
            package_result = serializer.patch_packages(
                work_root=staging,
                mappings_path=mappings,
                packages=(WorldLootPackagePatch(source, output, (
                    WorldLootTuplePatch(8, 0, "BP_PickupItem_C_UAID_example", original, assigned),
                    WorldLootTuplePatch(9, 1, "BP_PickupItem_C_UAID_second", original, assigned),
                )),),
            )[0]
            self.assertEqual(package_result.input_path, source)
            self.assertEqual(package_result.output_path, output)
            self.assertEqual(len(package_result.patches), 2)
            self.assertTrue(all(patch.original == original for patch in package_result.patches))
            self.assertTrue(all(patch.assigned == assigned for patch in package_result.patches))
            self.assertEqual(source.read_bytes(), b"input package")
            self.assertFalse(list(staging.glob(".cv2-world-loot-patch-*.json")))
            self.assertEqual(runner.command[runner.command.index("patch-packages")], "patch-packages")
            self.assertIn("Release", runner.command)
            self.assertEqual(runner.package_counts, [1])
            self.assertEqual(runner.patch_counts, [2])

    # Check shop origin FName identity is
    def test_shop_origin_fname_identity_is_sent_without_suffix_inference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            output = source.with_name("sample.cv2-patched.umap")
            runner = FakeBatchRunner()
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)
            path = "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpJailA_07"
            identity = SoftObjectPathIdentity(
                FNameIdentity(path, 0),
                FNameIdentity("DA_Item_MatLevelUpJailA_07", 0),
            )
            assigned = ArticleReward(path, 1, True, identity)
            original = self._reward("DA_Item_ChemicalLight_1")

            result = serializer.patch_packages(
                work_root=staging,
                mappings_path=mappings,
                packages=(WorldLootPackagePatch(source, output, (
                    WorldLootTuplePatch(8, 0, "BP_PickupItem_C_UAID_example", original, assigned),
                )),),
            )

            manifest = runner.manifests[0]
            assigned_json = manifest["packages"][0]["patches"][0]["assigned"]
            self.assertEqual(
                assigned_json["softObjectPathIdentity"],
                {
                    "packageName": {"value": path, "number": 0},
                    "assetName": {"value": "DA_Item_MatLevelUpJailA_07", "number": 0},
                },
            )
            self.assertEqual(result[0].patches[0].assigned, assigned)

    # Check blueprint CDO patch allows absent
    def test_blueprint_cdo_patch_allows_absent_unique_save_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            source = source.with_suffix(".uasset")
            source.write_bytes(b"cdo header")
            source.with_suffix(".uexp").write_bytes(b"cdo exports")
            output = source.with_name("sample.cv2-patched.uasset")
            runner = FakeBatchRunner()
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)
            identity = SoftObjectPathIdentity(
                FNameIdentity("/Game/Blueprint/Article/Item/Material/DA_Item_MatCommonStone", 2),
                FNameIdentity("DA_Item_MatCommonStone", 2),
            )
            original = ArticleReward(
                "/Game/Blueprint/Article/Item/Material/DA_Item_MatCommonStone_1", 1, True, identity,
            )
            assigned = ArticleReward("/Game/Blueprint/Article/Item/Material/DA_Item_MatCommonFire_1", 1, True,
                SoftObjectPathIdentity(
                    FNameIdentity("/Game/Blueprint/Article/Item/Material/DA_Item_MatCommonFire", 2),
                    FNameIdentity("DA_Item_MatCommonFire", 2),
                ))

            result = serializer.patch_packages(
                work_root=staging,
                mappings_path=mappings,
                packages=(WorldLootPackagePatch(source, output, (
                    WorldLootTuplePatch(
                        3, 0, "Default__BP_it23_001_A_0_C", original, assigned,
                        require_unique_save_id=False,
                    ),
                )),),
            )

            request = runner.manifests[0]["packages"][0]["patches"][0]
            self.assertFalse(request["requireUniqueSaveId"])
            self.assertEqual(result[0].patches[0].unique_save_id, "")
            self.assertEqual(result[0].patches[0].assigned, assigned)
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_suffix(".uexp").is_file())

    # Check helper batches packages reports batch
    def test_helper_batches_packages_and_reports_batch_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            second_source = source.with_name("second.uasset")
            second_source.write_bytes(b"second original")
            second_source.with_suffix(".uexp").write_bytes(b"second exports")
            first_output = source.with_name("sample.cv2-patched.umap")
            second_output = second_source.with_name("second.cv2-patched.uasset")
            runner = FakeBatchRunner()
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)
            original = self._reward("DA_Item_ChemicalLight_1")
            assigned = self._reward("DA_Item_ExpA_12")
            patch = WorldLootTuplePatch(8, 0, "BP_PickupItem_C_UAID_example", original, assigned)
            progress: list[tuple[int, int]] = []

            results = serializer.patch_packages(
                work_root=staging,
                mappings_path=mappings,
                packages=(
                    WorldLootPackagePatch(source, first_output, (
                        patch,
                        WorldLootTuplePatch(9, 1, "BP_PickupItem_C_UAID_second", original, assigned),
                    )),
                    WorldLootPackagePatch(second_source, second_output, (patch,)),
                ),
                progress=lambda current, total: progress.append((current, total)),
            )

            self.assertEqual(len(results), 2)
            self.assertEqual(runner.package_counts, [2])
            self.assertEqual(runner.patch_counts, [2])
            self.assertEqual(runner.progress_metadata, [(1, 2)])
            self.assertEqual(
                [environment["DOTNET_GCHeapHardLimit"] for environment in runner.helper_environments],
                ["0x80000000"],
            )
            self.assertEqual(progress, [(2, 2)])
            self.assertEqual(source.read_bytes(), b"input package")
            self.assertEqual(second_source.read_bytes(), b"second original")
            self.assertTrue(all(result.output_path.is_file() for result in results))

    # Check package batches run parallel bounded
    def test_package_batches_run_in_parallel_with_bounded_workers_and_stable_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            runner = ConcurrentFakeBatchRunner()
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)
            original = self._reward("DA_Item_ChemicalLight_1")
            assigned = self._reward("DA_Item_ExpA_12")
            packages = []
            for index in range(33):
                package_source = source.with_name(f"source-{index:03}.umap")
                package_source.write_bytes(f"source {index}".encode())
                package_output = package_source.with_name(f"source-{index:03}.cv2-patched.umap")
                packages.append(WorldLootPackagePatch(package_source, package_output, (
                    WorldLootTuplePatch(8, 0, f"pickup-{index}", original, assigned),
                )))
            progress: list[tuple[int, int]] = []

            with (
                patch(
                    "cv2randomizer.game.world_loot_serialization.MemoryBudgetScheduler.recommended_worker_count",
                    return_value=2,
                ),
                patch(
                    "cv2randomizer.game.world_loot_serialization.MemoryBudgetScheduler.job",
                    side_effect=lambda *_args, **_kwargs: nullcontext(),
                ),
            ):
                results = serializer.patch_packages(
                    work_root=staging,
                    mappings_path=mappings,
                    packages=packages,
                    progress=lambda current, total: progress.append((current, total)),
                )

            self.assertEqual(runner.maximum_active_calls, 2)
            self.assertEqual(len(runner.commands), 2)
            self.assertEqual(len(set(runner.mapping_paths)), 2)
            self.assertEqual(runner.mapping_payloads, [b"mapping placeholder", b"mapping placeholder"])
            self.assertTrue(all(not path.exists() for path in runner.mapping_paths))
            self.assertEqual(mappings.read_bytes(), b"mapping placeholder")
            self.assertEqual([result.input_path for result in results], [item.input_asset for item in packages])
            self.assertEqual(len(progress), 2)
            self.assertEqual(progress[-1], (33, 33))
            self.assertEqual([current for current, _ in progress], sorted(current for current, _ in progress))
            self.assertFalse(list(staging.glob(".cv2-world-loot-patch-*.json")))
            self.assertTrue(all(result.output_path.is_file() for result in results))

    # Check parallel batch failure removes outputs
    def test_parallel_batch_failure_removes_outputs_from_successful_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            runner = SelectiveFailureBatchRunner()
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)
            original = self._reward("DA_Item_ChemicalLight_1")
            assigned = self._reward("DA_Item_ExpA_12")
            packages = []
            for index in range(33):
                package_source = source.with_name(f"source-{index:03}.umap")
                package_source.write_bytes(f"source {index}".encode())
                package_output = package_source.with_name(f"source-{index:03}.cv2-patched.umap")
                packages.append(WorldLootPackagePatch(package_source, package_output, (
                    WorldLootTuplePatch(8, 0, f"pickup-{index}", original, assigned),
                )))

            with (
                patch(
                    "cv2randomizer.game.world_loot_serialization.MemoryBudgetScheduler.recommended_worker_count",
                    return_value=2,
                ),
                patch(
                    "cv2randomizer.game.world_loot_serialization.MemoryBudgetScheduler.job",
                    side_effect=lambda *_args, **_kwargs: nullcontext(),
                ),
            ):
                with self.assertRaisesRegex(WorldLootSerializationError, r"batch \(exit 1\)"):
                    serializer.patch_packages(
                        work_root=staging,
                        mappings_path=mappings,
                        packages=packages,
                    )

            self.assertFalse(list(staging.glob("*.cv2-patched.umap")))
            self.assertFalse(list(staging.glob("*.cv2-patched.uexp")))
            self.assertFalse(list(staging.glob(".cv2-world-loot-patch-*.json")))
            self.assertFalse(list(staging.glob(".cv2-world-loot-mappings-*.usmap")))
            self.assertTrue(all(item.input_asset.read_bytes() == f"source {index}".encode()
                                for index, item in enumerate(packages)))

    # Check batch failure leaves no partial
    def test_batch_failure_leaves_no_partial_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            output = source.with_name("sample.cv2-patched.umap")
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=FakeBatchRunner(exit_code=1))
            patch = WorldLootTuplePatch(8, 0, "BP_PickupItem_C_UAID_example", self._reward("DA_Item_ChemicalLight_1"), self._reward("DA_Item_ExpA_12"))
            with self.assertRaisesRegex(WorldLootSerializationError, r"batch \(exit 1\)"):
                serializer.patch_packages(
                    work_root=staging,
                    mappings_path=mappings,
                    packages=(WorldLootPackagePatch(source, output, (patch,)),),
                )
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".uexp").exists())
            self.assertEqual(source.read_bytes(), b"input package")

    # Check batch reject helper without raw
    def test_batch_rejects_helper_without_raw_payload_preservation_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, source, _, mappings, _, _ = self._setup(root)
            output = source.with_name("sample.cv2-patched.umap")
            runner = FakeBatchRunner(raw_payload_preserved=False)
            serializer = WorldLootSerializer(helper_project=root / "helper.csproj", runner=runner)
            patch = WorldLootTuplePatch(
                8, 0, "BP_PickupItem_C_UAID_example",
                self._reward("DA_Item_ChemicalLight_1"), self._reward("DA_Item_ExpA_12"),
            )

            with self.assertRaisesRegex(WorldLootSerializationError, "raw preservation"):
                serializer.patch_packages(
                    work_root=staging,
                    mappings_path=mappings,
                    packages=(WorldLootPackagePatch(source, output, (patch,)),),
                )

            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".uexp").exists())
            self.assertEqual(source.read_bytes(), b"input package")


if __name__ == "__main__":
    unittest.main()
