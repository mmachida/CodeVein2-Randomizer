from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.bequeathed_formae_catalog import BequeathedFormaCatalogStore
from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.bequeathed_formae_generation import (
    BequeathedFormaGenerationError,
    apply_bequeathed_formae_plan,
    prepare_bequeathed_formae_plan,
)
from cv2randomizer.game.blood_code_generation import BloodCodeGrantSnapshot, BloodCodeSerializer
from cv2randomizer.randomizer.world_loot import build_world_loot_randomization_plan


ROOT = Path(__file__).resolve().parents[1]


class _Extractor:
    # Initialize this object
    def __init__(self, root: Path) -> None:
        self.root = root
        self.requested_packages: tuple[str, ...] = ()

    # Extract requested game assets
    def extract_assets(self, _game_root: Path, packages: tuple[str, ...], **_kwargs: Any) -> tuple[ExtractedAsset, ...]:
        self.requested_packages = tuple(packages)
        assets = []
        for package in packages:
            header = self.root / Path(*package.split("/")[2:]).with_suffix(".uasset")
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_bytes(b"fixture uasset " + package.encode("utf-8"))
            header.with_suffix(".uexp").write_bytes(b"fixture exports")
            assets.append(ExtractedAsset(package, (header, header.with_suffix(".uexp"))))
        return tuple(assets)


class _Serializer:
    # Initialize this object
    def __init__(self, catalog, *, bad_reward: bool = False, fail_package: str | None = None) -> None:
        self.catalog = catalog
        self.bad_reward = bad_reward
        self.fail_package = fail_package
        self.patch_requests: dict[str, tuple[Path, Path, tuple[dict[str, Any], ...]]] = {}
        self.successfully_patched_packages: list[str] = []

    # Inspect bequeathed formae packages
    def inspect_bequeathed_formae_packages(self, *, package_files, **_kwargs):
        snapshots = []
        grants = {
            grant.quest_asset.casefold(): (forma, grant)
            for forma in self.catalog.direct_rewards
            for grant in forma.direct_grants
        }
        for package in package_files:
            forma, grant = grants[package.casefold()]
            reward_path = forma.asset_path
            if self.bad_reward and forma.object == "DA_LouAxe":
                reward_path = self.catalog.direct_rewards[0].asset_path
            reward_package, _, reward_asset = reward_path.rpartition("/")
            snapshots.append(BloodCodeGrantSnapshot(
                package,
                grant.export_index,
                "QSTask_GainItem_1",
                0,
                0,
                reward_package + "/" + reward_asset,
                reward_asset,
                grant.count,
                grant.notification,
            ))
        return tuple(snapshots)

    # Patch bequeathed formae packages
    def patch_bequeathed_formae_packages(self, *, patch_packages, **_kwargs):
        return self._patch_packages(patch_packages)

    # Patch world loot quest packages
    def patch_world_loot_quest_packages(self, *, patch_packages, **_kwargs):
        return self._patch_packages(patch_packages)

    # Patch package reward data
    def _patch_packages(self, patch_packages):
        self.patch_requests = dict(patch_packages)
        results = []
        for virtual, (source, output, assignments) in patch_packages.items():
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(source.read_bytes() + b"patched")
            output.with_suffix(".uexp").write_bytes(source.with_suffix(".uexp").read_bytes() + b"patched")
            if self.fail_package is not None and self.fail_package.casefold() in virtual.casefold():
                raise RuntimeError("simulated serialization failure")
            self.successfully_patched_packages.append(virtual)
            results.append({
                "virtualPackage": virtual,
                "inputFile": str(source),
                "outputFile": str(output),
                "patchedSlots": len(assignments),
                "outputFiles": [str(output), str(output.with_suffix(".uexp"))],
            })
        return results


class BequeathedFormaGenerationTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.catalog = BequeathedFormaCatalogStore(ROOT / "data" / "bFormae" / "current").load()

    # Check single remaining forma identity does
    def test_single_remaining_forma_identity_does_not_run_a_separate_quest_shuffle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            serializer = _Serializer(self.catalog)
            extractor = _Extractor(root / "cache")
            preparation = prepare_bequeathed_formae_plan(
                self.catalog,
                "test-seed-17",
                game_root=root / "game",
                work_root=root,
                mappings_path=mappings,
                game_version="fixture-build",
                data_revision="current",
                extractor=extractor,
                serializer=serializer,
            )
            result = apply_bequeathed_formae_plan(
                preparation,
                work_root=root,
                output_content_root=root / "staging" / "Content",
                mappings_path=mappings,
                serializer=serializer,
            )
            self.assertEqual(preparation.plan.assignments, ())
            self.assertEqual(extractor.requested_packages, ())
            self.assertEqual(serializer.patch_requests, {})
            self.assertTrue(result.reopened)
            self.assertEqual(result.output_files, ())
            self.assertEqual(result.patched_packages, ())

    # Check shared gain item serializer use
    def test_shared_gain_item_serializer_uses_generic_article_patch_for_world_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            helper = root / "UAssetAPIWorldLoot.csproj"
            helper.write_text("fixture", encoding="utf-8")
            forma = self.catalog.by_object("DA_ExecutionSword")
            grant = forma.direct_grants[0]
            source = root / "source.uasset"
            source.write_bytes(b"fixture source")
            output = root / "output.uasset"
            seen_operations = []

            # Run the requested operation
            def runner(command, **_kwargs):
                operation = command[command.index("--") + 1]
                seen_operations.append(operation)
                if operation == "inspect-bequeathed-formae":
                    package, _, asset = forma.asset_path.rpartition("/")
                    payload = [{
                        "virtualPackage": grant.quest_asset,
                        "inputFile": str(source),
                        "grantCount": 1,
                        "grants": [{
                            "package": grant.quest_asset,
                            "exportIndex": grant.export_index,
                            "exportName": "QSTask_GainItem_0",
                            "bulkIndex": 0,
                            "rewardIndex": 0,
                            "rewardPackage": package + "/" + asset,
                            "rewardAsset": asset,
                            "count": grant.count,
                            "notification": grant.notification,
                        }],
                    }]
                else:
                    payload = [{
                        "virtualPackage": grant.quest_asset,
                        "inputFile": str(source),
                        "outputFile": str(output),
                        "patchedSlots": 1,
                        "outputFiles": [str(output)],
                    }]
                return CompletedProcess(command, 0, json.dumps(payload), "")

            serializer = BloodCodeSerializer(helper_project=helper, runner=runner)
            snapshots = serializer.inspect_bequeathed_formae_packages(
                work_root=root,
                mappings_path=mappings,
                package_files={grant.quest_asset: source},
            )
            self.assertEqual(snapshots[0].export_name, "QSTask_GainItem_0")
            serializer.patch_world_loot_quest_packages(
                work_root=root,
                mappings_path=mappings,
                patch_packages={grant.quest_asset: (source, output, ({"exportIndex": grant.export_index},))},
            )
            self.assertEqual(seen_operations, ["inspect-bequeathed-formae", "patch-world-loot-quest-articles"])

    # Check world loot executioner assignment is
    def test_world_loot_executioner_assignment_is_mirrored_to_alternative_quest_path(self) -> None:
        catalogs = CatalogStore(ROOT / "data" / "catalogs").load("current")
        world_plan = build_world_loot_randomization_plan(
            catalogs.world_loot,
            "executioner-alternative-route-test",
            unsafe_packages=catalogs.unsafe_packages,
        )
        assignment = next(
            item for item in world_plan.assignments
            if item.destination.destination_id == "bequeathed_executioner_lyle_quest"
        )
        self.assertNotEqual(
            assignment.assigned_rewards[0].article_path.casefold(),
            "/game/blueprint/article/gadget/standalone/da_executionsword",
        )
        self.assertFalse(
            assignment.assigned_rewards[0].article_path.casefold().startswith(
                "/game/blueprint/article/gadget/"
            ),
            "This seed intentionally exercises a non-Gadget World Loot Article assigned to the quest grant.",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            serializer = _Serializer(self.catalog)
            extractor = _Extractor(root / "cache")
            preparation = prepare_bequeathed_formae_plan(
                self.catalog,
                "executioner-alternative-route-test",
                game_root=root / "game",
                work_root=root,
                mappings_path=mappings,
                game_version="fixture-build",
                data_revision="current",
                extractor=extractor,
                serializer=serializer,
                world_loot_plan=world_plan,
            )
            result = apply_bequeathed_formae_plan(
                preparation,
                work_root=root,
                output_content_root=root / "staging" / "Content",
                mappings_path=mappings,
                serializer=serializer,
            )

            self.assertEqual(
                set(extractor.requested_packages),
                {grant.quest_asset for grant in self.catalog.by_object("DA_ExecutionSword").direct_grants},
            )
            self.assertEqual(
                tuple(serializer.patch_requests),
                ("/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00560",),
            )
            mirror_document = serializer.patch_requests[
                "/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00560"
            ][2][0]
            self.assertEqual(mirror_document["exportIndex"], 20)
            self.assertEqual(mirror_document["expectedPackage"], self.catalog.by_object("DA_ExecutionSword").asset_path)
            self.assertEqual(mirror_document["assignedPackage"], assignment.assigned_rewards[0].article_path)
            self.assertEqual(len(result.patched_packages), 1)


if __name__ == "__main__":
    unittest.main()
