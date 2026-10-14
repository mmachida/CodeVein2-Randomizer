from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.material_serialization import (
    MaterialCdoRewardPatch,
    MaterialSerializationError,
    MaterialSerializer,
)
from cv2randomizer.game.world_loot_serialization import ArticleReward, WorldLootSerializationError, WorldLootSerializer


class FakeWorldLootRunner:
    # Initialize this object
    def __init__(self) -> None:
        self.manifest: dict[str, object] | None = None

    # Call the wrapped operation
    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.manifest = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        results = []
        for package in self.manifest["packages"]:
            input_path = Path(package["inputFile"])
            output_path = Path(package["outputFile"])
            output_path.write_bytes(b"patched Blueprint header")
            output_path.with_suffix(".uexp").write_bytes(b"patched Blueprint exports")
            patches = []
            for patch in package["patches"]:
                patches.append({
                    "exportIndex": 2,
                    "exportName": patch["exportName"],
                    "rewardIndex": patch["rewardIndex"],
                    "uniqueSaveId": "",
                    "original": patch["expected"],
                    "assigned": patch["assigned"],
                    "outputFile": str(output_path),
                    "reopenedAndValidated": True,
                    "rawPayloadPreserved": True,
                })
            results.append({
                "inputFile": str(input_path),
                "outputFile": str(output_path),
                "patches": patches,
                "reopenedAndValidated": True,
                "rawPayloadPreserved": True,
            })
        return subprocess.CompletedProcess(command, 0, json.dumps(results), "")


class MaterialSerializationTests(unittest.TestCase):
    # Prepare the package test fixture
    def _setup(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path, FakeWorldLootRunner]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        work = root / "work"
        package_dir = work / "source" / "Content" / "Environment" / "Item"
        package_dir.mkdir(parents=True)
        source = package_dir / "BP_Material_Test.uasset"
        source.write_bytes(b"original Blueprint header")
        source.with_suffix(".uexp").write_bytes(b"original Blueprint exports")
        output = package_dir / "BP_Material_Test.cv2-material-patched.uasset"
        mappings = root / "CV2.usmap"
        mappings.write_bytes(b"mapping")
        helper = root / "helper.csproj"
        helper.write_text("test helper", encoding="utf-8")
        runner = FakeWorldLootRunner()
        return temporary, work, source, mappings, runner

    # Check patches article data path named
    def test_patches_only_article_data_path_on_named_shared_cdo(self) -> None:
        temporary, work, source, mappings, runner = self._setup()
        try:
            output = source.with_name(source.stem + ".cv2-material-patched.uasset")
            serializer = MaterialSerializer(world_loot_serializer=WorldLootSerializer(
                helper_project=work.parent / "helper.csproj",
                runner=runner,
            ))
            result = serializer.patch_packages(
                work_root=work,
                mappings_path=mappings,
                packages=(MaterialCdoRewardPatch(
                    "/Game/Environment/Item/BP_Material_Test",
                    "BP_Material_Test_C",
                    source,
                    output,
                    ArticleReward("/Game/Blueprint/Article/Item/Material/DA_Old", 1, True),
                    ArticleReward("/Game/Blueprint/Article/Item/Material/DA_New", 1, True),
                ),),
            )
            request = runner.manifest["packages"][0]
            self.assertEqual(request["blueprintSchemaPackage"], "/Game/Environment/Item/BP_Material_Test")
            self.assertEqual(request["patches"][0]["exportIndex"], 0)
            self.assertEqual(request["patches"][0]["exportName"], "Default__BP_Material_Test_C")
            self.assertFalse(request["patches"][0]["requireUniqueSaveId"])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].patches[0].export_index, 2)
            self.assertEqual(result[0].patches[0].original.article_data_path,
                             "/Game/Blueprint/Article/Item/Material/DA_Old")
            self.assertEqual(result[0].patches[0].assigned.article_data_path,
                             "/Game/Blueprint/Article/Item/Material/DA_New")
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_suffix(".uexp").is_file())
            self.assertTrue(source.with_suffix(".uexp").read_bytes() == b"original Blueprint exports")
        finally:
            temporary.cleanup()

    # Check refuses change count notification
    def test_refuses_to_change_count_or_notification(self) -> None:
        temporary, work, source, mappings, runner = self._setup()
        try:
            serializer = MaterialSerializer(world_loot_serializer=WorldLootSerializer(
                helper_project=work.parent / "helper.csproj",
                runner=runner,
            ))
            output = source.with_name(source.stem + ".cv2-material-patched.uasset")
            with self.assertRaisesRegex(MaterialSerializationError, "ArticleDataPath only"):
                serializer.patch_packages(
                    work_root=work,
                    mappings_path=mappings,
                    packages=(MaterialCdoRewardPatch(
                        "/Game/Environment/Item/BP_Material_Test",
                        "BP_Material_Test_C", source, output,
                        ArticleReward("/Game/Blueprint/Article/Item/Material/DA_Old", 1, True),
                        ArticleReward("/Game/Blueprint/Article/Item/Material/DA_New", 2, True),
                    ),),
                )
            self.assertIsNone(runner.manifest)
        finally:
            temporary.cleanup()

    # Check zero export index require matching
    def test_zero_export_index_requires_matching_registered_cdo_name(self) -> None:
        temporary, work, source, mappings, runner = self._setup()
        try:
            package = source.with_name(source.stem + ".cv2-material-patched.uasset")
            serializer = WorldLootSerializer(
                helper_project=work.parent / "helper.csproj",
                runner=runner,
            )
            from cv2randomizer.game.world_loot_serialization import WorldLootPackagePatch, WorldLootTuplePatch

            with self.assertRaisesRegex(WorldLootSerializationError, "must match its registered schema package"):
                serializer.patch_packages(
                    work_root=work,
                    mappings_path=mappings,
                    packages=(WorldLootPackagePatch(
                        source,
                        package,
                        (WorldLootTuplePatch(
                            0, 0, "Default__Wrong_C",
                            ArticleReward("/Game/Blueprint/Article/Item/Material/DA_Old", 1, True),
                            ArticleReward("/Game/Blueprint/Article/Item/Material/DA_New", 1, True),
                            False,
                        ),),
                        "/Game/Environment/Item/BP_Material_Test",
                    ),),
                )
            self.assertIsNone(runner.manifest)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
