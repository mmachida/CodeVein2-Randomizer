from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.catalogs import RewardPayload, WorldLootLocation
from cv2randomizer.game.fname_identity import SoftObjectPathIdentity
from cv2randomizer.game.world_loot_dependencies import (
    WorldLootDependencyResolver,
    _healing_instance_locations_by_map,
    _run_batch_with_failure_isolation,
    _uassetapi_build_command,
)
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.core.memory_budget import GIB, MemoryBudgetScheduler
from cv2randomizer.generation.pipeline import _validate_world_loot_prepared_descriptors, _world_reward_fname_identity


class FakeExtractor:
    # Initialize this object
    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests: list[tuple[str, ...]] = []

    # Extract requested game assets
    def extract_assets(self, game_root: Path, virtual_paths: object, *, game_version: str, data_revision: str, progress: object = None) -> tuple[ExtractedAsset, ...]:
        paths = tuple(virtual_paths)
        self.requests.append(paths)
        results = []
        for virtual_path in paths:
            parts = virtual_path.split("/")[2:]
            stem = parts[-1]
            extension = ".umap" if virtual_path.startswith("/Game/Map/") else ".uasset"
            directory = self.root.joinpath(*parts[:-1], stem)
            directory.mkdir(parents=True, exist_ok=True)
            header = directory / (stem + extension)
            sidecar = directory / (stem + ".uexp")
            header.write_bytes((virtual_path + extension).encode())
            sidecar.write_bytes(b"export data")
            results.append(ExtractedAsset(virtual_path, (header, sidecar)))
        return tuple(results)


class DependencyInspectionRunner:
    # Initialize this object
    def __init__(self, export_name: str) -> None:
        self.export_name = export_name
        self.commands: list[list[str]] = []

    # Call the wrapped operation
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if "inspect-map-batch" in command:
            manifest = json.loads(Path(command[-3]).read_text(encoding="utf-8"))
            results = []
            for request in manifest["maps"]:
                results.append({
                    "inputFile": request["inputFile"],
                    "classes": [
                        {
                            "exportName": self.export_name,
                            "exportIndex": index,
                            "className": "BlueprintGeneratedClass",
                            "packagePath": "/Game/Blueprint/Pickup/BP_Pickup",
                        }
                        for index in request["exportIndices"]
                    ],
                })
            stdout = json.dumps(results)
        elif "blueprint-parents" in command:
            arguments = command[command.index("--") + 1:]
            stdout = json.dumps([
                {
                    "classExportName": Path(path).stem + "_C",
                    "parentClassName": "BlueprintGeneratedClass" if Path(path).stem == "BP_Pickup" else "Actor",
                    "parentPackage": "/Game/Blueprint/Base/BP_Base" if Path(path).stem == "BP_Pickup" else None,
                }
                for path in arguments[4:]
            ])
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout + "\n", "")


class PerMapClassInspectionRunner:
    # Initialize this object
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    # Call the wrapped operation
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if "inspect-map-batch" in command:
            manifest = json.loads(Path(command[-3]).read_text(encoding="utf-8"))
            results = []
            for request in manifest["maps"]:
                map_name = Path(request["inputFile"]).stem
                package = "/Game/Blueprint/Pickup/BP_Generic" if map_name == "Map001" else "/Game/Blueprint/Pickup/BP_FlagChange"
                results.append({
                    "inputFile": request["inputFile"],
                    "classes": [
                        {
                            "exportName": f"BP_Pickup_C_UAID_{index}",
                            "exportIndex": index,
                            "className": "BlueprintGeneratedClass",
                            "packagePath": package,
                        }
                        for index in request["exportIndices"]
                    ],
                })
            stdout = json.dumps(results)
        elif "blueprint-parents" in command:
            arguments = command[command.index("--") + 1:]
            stdout = json.dumps([
                {
                    "classExportName": Path(path).stem + "_C",
                    "parentClassName": "Actor",
                    "parentPackage": None,
                }
                for path in arguments[4:]
            ])
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout + "\n", "")


class UnexpectedMapInspectionRunner:
    # Call the wrapped operation
    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"prepared map should not be reopened for discovery: {command}")


# Build a test location record
def _location() -> WorldLootLocation:
    return WorldLootLocation(
        package_path="/Game/Map/MainWorld/MainWorld/_Generated_/Map001",
        source_file="Map001.umap",
        export_index=8,
        export_name="BP_Pickup_C_UAID_example",
        actor_class="BP_Pickup_C",
        actor_category="GROUND_PICKUP",
        save_id="example-save-id",
        protected=False,
        protection_reasons=(),
        rewards=(RewardPayload(
            "/Game/Blueprint/Article/Item/Support/DA_Item_1", 1, True,
            soft_object_path_identity=SoftObjectPathIdentity.from_literal_path(
                "/Game/Blueprint/Article/Item/Support/DA_Item_1",
            ),
        ),),
    )


class WorldLootDependencyResolverTests(unittest.TestCase):
    # Check namemap preparation runs independent batches
    def test_namemap_preparation_runs_independent_batches_under_memory_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_root = root / "work"
            work_root.mkdir()
            mapping = root / "CV2.usmap"
            mapping.write_bytes(b"map")
            package_requests = []
            for index in range(6):
                package_path = f"/Game/Map/MainWorld/MainWorld/_Generated_/Map{index:03d}"
                source = work_root / f"Map{index:03d}.umap"
                source.write_bytes(f"header-{index}".encode())
                source.with_suffix(".uexp").write_bytes(f"payload-{index}".encode())
                package_requests.append({
                    "packagePath": package_path,
                    "inputFile": str(source),
                    "rewards": [{
                        "exportIndex": 1,
                        "rewardIndex": 0,
                        "exportName": "Pickup_C",
                        "expected": {
                            "path": "/Game/Blueprint/Article/Item/DA_Item_1",
                            "count": 1,
                            "notification": True,
                            "softObjectPathIdentity": {"packageName": {}, "assetName": {}},
                        },
                    }],
                })

            lock = threading.Lock()
            active = peak_active = 0

            # Run the requested operation
            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal active, peak_active
                manifest_path = Path(command[-3])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                rows = []
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                try:
                    time.sleep(0.03)
                    for request in manifest["packages"]:
                        header = Path(request["inputFile"])
                        sidecar = header.with_suffix(".uexp")
                        rows.append({
                            "packagePath": request["packagePath"],
                            "status": "raw_binary_candidate",
                            "reopenedAndValidated": True,
                            "rawUexpByteIdentical": True,
                            "fields": [{"exportIndex": 1, "rewardIndex": 0}],
                            "nameMapIndices": {"DA_Item_1": 0},
                            "nameMapCountBefore": 1,
                            "nameMapCountAfter": 1,
                            "namesAdded": [],
                            "preparationMilliseconds": 30,
                            "workingSetBytes": 20 * 1024 * 1024,
                            "preparedUassetSha256": "unused-for-raw-candidate",
                            "preparedUexpSha256": "unused-for-raw-candidate",
                            "uassetSizeAfter": header.stat().st_size,
                            "uexpSizeAfter": sidecar.stat().st_size,
                            "outputFiles": [],
                        })
                finally:
                    with lock:
                        active -= 1
                return subprocess.CompletedProcess(command, 0, json.dumps(rows) + "\n", "")

            scheduler = MemoryBudgetScheduler(
                memory_reader=lambda: SimpleNamespace(total=32 * GIB, available=20 * GIB),
                maximum_workers=3,
            )
            resolver = WorldLootDependencyResolver(helper_project=root / "helper.csproj", runner=runner)
            worker_mappings = (mapping, mapping, mapping)
            progress: list[tuple[int, int]] = []
            profile: list[tuple[str, dict[str, object]]] = []

            with patch("cv2randomizer.game.world_loot_dependencies._WORLD_LOOT_NAMEMAP_BATCH_SIZE", 1):
                results = resolver._prepare_world_loot_name_maps(
                    work_root,
                    mapping,
                    package_requests,
                    ("DA_Item_1",),
                    prepared_cache_root=root / "prepared",
                    memory_scheduler=scheduler,
                    worker_mappings=worker_mappings,
                    progress=lambda _stage, current, total: progress.append((current, total)),
                    diagnostics_callback=None,
                    profile_callback=lambda operation, **fields: profile.append((operation, fields)),
                )

        self.assertEqual(len(results), 6)
        self.assertGreaterEqual(peak_active, 2)
        self.assertLessEqual(peak_active, 3)
        self.assertEqual(progress[-1], (6, 6))
        launches = [fields for operation, fields in profile if operation == "helper_launch"]
        self.assertEqual(len(launches), 6)
        self.assertTrue(all(item["worker_limit"] == 3 for item in launches))

    # Check healing template preparation exclude non
    def test_healing_template_preparation_excludes_non_healing_instance_overrides(self) -> None:
        base = _location()
        healing_seed = replace(
            base,
            destination_kind="INSTANCE_ARTICLE_BULK_OVERRIDE",
            location_type="HEALING_UPGRADE_GOLDEN_SEED",
        )
        healing_bead = replace(
            base,
            export_index=9,
            destination_kind="INSTANCE_ARTICLE_BULK_OVERRIDE",
            location_type="HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
        )
        normal_override = replace(
            base,
            export_index=10,
            destination_kind="INSTANCE_ARTICLE_BULK_OVERRIDE",
            location_type="NORMAL_WORLD_LOOT",
        )
        ordinary_world_loot = replace(
            base,
            export_index=11,
            location_type="HEALING_UPGRADE_GOLDEN_SEED",
        )

        grouped = _healing_instance_locations_by_map({
            base.package_path: [healing_seed, healing_bead, normal_override, ordinary_world_loot],
        })

        self.assertEqual(
            [item.export_index for item in grouped[base.package_path.casefold()]],
            [8, 9],
        )

    # Check batch failure is bisected unsafe
    def test_batch_failure_is_bisected_and_only_unsafe_item_falls_back(self) -> None:
        calls: list[tuple[int, ...]] = []
        failures: list[tuple[int, str]] = []
        completed: list[int] = []

        # Run batch
        def run_batch(items: tuple[int, ...]) -> tuple[int, ...]:
            calls.append(items)
            if 3 in items:
                raise ValueError("unserializable item 3")
            return items

        result = _run_batch_with_failure_isolation(
            tuple(range(1, 9)),
            run_batch,
            lambda item, error: failures.append((item, str(error))),
            completed.append,
        )

        self.assertEqual(result, (1, 2, 4, 5, 6, 7, 8))
        self.assertEqual(failures, [(3, "unserializable item 3")])
        self.assertEqual(sum(completed), 8)
        self.assertLess(len(calls), 8)

    # Check helper build command use dotnet
    def test_helper_build_command_uses_dotnet_build_project_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "tools" / "UAssetAPIWorldLoot.csproj"
            helper.parent.mkdir(parents=True)
            helper.write_text("test project", encoding="utf-8")
            assets = helper.parent / "obj" / "project.assets.json"
            assets.parent.mkdir(parents=True)
            assets.write_text("{}", encoding="utf-8")
            self.assertEqual(_uassetapi_build_command("dotnet", helper), [
                "dotnet", "build", str(helper), "--configuration", "Release", "--no-restore",
            ])

    # Check generate cache descriptor must match
    def test_generate_cache_descriptor_must_match_catalog_identity_and_full_reward_tuple(self) -> None:
        location = _location()
        reward = location.rewards[0]
        inventory = PackageInventory.create((location.package_path, reward.article_path))
        reward_descriptor = {
            "reward_index": 0,
            "article_path": reward.article_path,
            "count": reward.count,
            "notification": reward.notification,
            "soft_object_path_identity": _world_reward_fname_identity(location, 0),
            "article_package_available": True,
        }
        key = f"{location.package_path.casefold()}#{location.export_index}"
        descriptor = {
            "package_path": location.package_path,
            "export_index": location.export_index,
            "export_name": location.export_name,
            "actor_class": location.actor_class,
            "actor_category": location.actor_category,
            "destination_kind": location.destination_kind,
            "location_type": location.location_type,
            "structural_class_package": location.structural_class_package,
            "instance_override_source": None,
            "destination_id": location.destination_id,
            "protected": location.protected,
            "protection_reasons": [],
            "physical_instance_count": location.physical_instance_count,
            "parent_blueprint_packages": [],
            "package_available": True,
            "rewards": [reward_descriptor],
            "patch_descriptor": {
                "serializer": location.destination_kind,
                "expected_rewards": [reward_descriptor],
            },
        }
        index = {"world_loot_location_descriptors": {key: descriptor}}

        _validate_world_loot_prepared_descriptors((location,), index, inventory)

        descriptor["rewards"][0]["count"] = 2
        with self.assertRaisesRegex(RuntimeError, "differs from the active catalog"):
            _validate_world_loot_prepared_descriptors((location,), index, inventory)

    # Check cached class index skips repeating
    def test_cached_class_index_skips_repeating_map_and_blueprint_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            helper = root / "helper.csproj"
            helper.write_text("test project", encoding="utf-8")
            extractor = FakeExtractor(root / "cache")
            runner = DependencyInspectionRunner("must-not-be-called")
            resolver = WorldLootDependencyResolver(
                extractor=extractor,
                helper_project=helper,
                runner=runner,
            )
            location = _location()
            reward = location.rewards[0]
            fname_identity = reward.soft_object_path_identity
            assert fname_identity is not None
            location_key = f"{location.package_path.casefold()}#{location.export_index}"
            dependency_index = {
                "schema_version": 1,
                "game_version": "test-build",
                "catalog_revision": "current",
                "blueprint_packages": ["/Game/Blueprint/Pickup/BP_Pickup"],
                "maps": {
                    location.package_path: {
                        str(location.export_index): {
                            "export_name": location.export_name,
                            "class_name": "BlueprintGeneratedClass",
                            "class_package": "/Game/Blueprint/Pickup/BP_Pickup",
                        },
                    },
                },
                "world_loot_location_descriptors": {
                    location_key: {
                        "package_path": location.package_path,
                        "export_index": location.export_index,
                        "export_name": location.export_name,
                        "actor_class": location.actor_class,
                        "actor_category": location.actor_category,
                        "destination_kind": location.destination_kind,
                        "location_type": location.location_type,
                        "structural_class_package": location.structural_class_package,
                        "instance_override_source": None,
                        "protected": location.protected,
                        "protection_reasons": [],
                        "physical_instance_count": location.physical_instance_count,
                        "parent_blueprint_packages": [],
                        "package_available": True,
                        "prepared": True,
                        "rewards": [{
                            "reward_index": 0,
                            "article_path": reward.article_path,
                            "count": reward.count,
                            "notification": reward.notification,
                            "soft_object_path_identity": {
                                "package_name": {"value": fname_identity.package_name.value, "number": fname_identity.package_name.number},
                                "asset_name": {"value": fname_identity.asset_name.value, "number": fname_identity.asset_name.number},
                            },
                        }],
                    },
                },
            }

            result = resolver.stage_for_locations(
                game_root=root / "game",
                locations=(location,),
                work_root=root / "generation",
                mappings_path=mappings,
                game_version="test-build",
                data_revision="current",
                dependency_index=dependency_index,
            )

            self.assertEqual(extractor.requests, [
                (location.package_path,),
                ("/Game/Blueprint/Pickup/BP_Pickup",),
            ])
            self.assertEqual(result.class_packages, ("/Game/Blueprint/Pickup/BP_Pickup",))
            self.assertEqual(len(result.blueprint_packages), 1)
            self.assertEqual(runner.commands, [])

    # Check cached class index reject different
    def test_cached_class_index_rejects_a_different_expected_reward_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            helper = root / "helper.csproj"
            helper.write_text("test project", encoding="utf-8")
            resolver = WorldLootDependencyResolver(
                extractor=FakeExtractor(root / "cache"),
                helper_project=helper,
                runner=DependencyInspectionRunner("must-not-be-called"),
            )
            location = _location()
            reward = location.rewards[0]
            identity = reward.soft_object_path_identity
            assert identity is not None
            dependency_index = {
                "schema_version": 1,
                "game_version": "test-build",
                "catalog_revision": "current",
                "blueprint_packages": [],
                "maps": {
                    location.package_path: {
                        str(location.export_index): {
                            "export_name": location.export_name,
                            "class_name": "BlueprintGeneratedClass",
                            "class_package": "/Game/Blueprint/Pickup/BP_Pickup",
                        },
                    },
                },
                "world_loot_location_descriptors": {
                    f"{location.package_path.casefold()}#{location.export_index}": {
                        "package_path": location.package_path,
                        "export_index": location.export_index,
                        "export_name": location.export_name,
                        "actor_class": location.actor_class,
                        "actor_category": location.actor_category,
                        "destination_kind": location.destination_kind,
                        "location_type": location.location_type,
                        "structural_class_package": location.structural_class_package,
                        "instance_override_source": None,
                        "protected": False,
                        "protection_reasons": [],
                        "physical_instance_count": 1,
                        "parent_blueprint_packages": [],
                        "package_available": True,
                        "prepared": True,
                        "rewards": [{
                            "reward_index": 0,
                            "article_path": "/Game/Blueprint/Article/Item/Support/DA_Different",
                            "count": reward.count,
                            "notification": reward.notification,
                            "soft_object_path_identity": {
                                "package_name": {"value": identity.package_name.value, "number": identity.package_name.number},
                                "asset_name": {"value": identity.asset_name.value, "number": identity.asset_name.number},
                            },
                        }],
                    },
                },
            }
            with self.assertRaisesRegex(RuntimeError, "reward descriptor differs from the catalog"):
                resolver.stage_for_locations(
                    game_root=root / "game",
                    locations=(location,),
                    work_root=root / "generation",
                    mappings_path=mappings,
                    game_version="test-build",
                    data_revision="current",
                    dependency_index=dependency_index,
                )

    # Check materializes map direct class recursive
    def test_materializes_map_direct_class_and_recursive_blueprint_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            helper = root / "helper.csproj"
            helper.write_text("test project", encoding="utf-8")
            extractor = FakeExtractor(root / "cache")
            runner = DependencyInspectionRunner("BP_Pickup_C_UAID_example")
            resolver = WorldLootDependencyResolver(
                extractor=extractor,
                helper_project=helper,
                runner=runner,
            )
            dlc_location = replace(
                _location(),
                package_path="/Game/Map/MainWorld/DlcWorld/_Generated_/Map001",
            )
            result = resolver.stage_for_locations(
                game_root=root / "game",
                locations=(dlc_location,),
                work_root=root / "generation",
                mappings_path=mappings,
                game_version="test-build",
                data_revision="current",
            )
            self.assertEqual(result.class_packages, (
                "/Game/Blueprint/Base/BP_Base",
                "/Game/Blueprint/Pickup/BP_Pickup",
            ))
            self.assertEqual(
                result.dependency_index["maps"][dlc_location.package_path]["8"],
                {
                    "export_name": "BP_Pickup_C_UAID_example",
                    "class_name": "BlueprintGeneratedClass",
                    "class_package": "/Game/Blueprint/Pickup/BP_Pickup",
                },
            )
            self.assertTrue((result.content_root / "Map" / "MainWorld" / "DlcWorld" / "_Generated_" / "Map001.umap").is_file())
            self.assertTrue((result.content_root / "Blueprint" / "Pickup" / "BP_Pickup.uasset").is_file())
            self.assertTrue((result.content_root / "Blueprint" / "Base" / "BP_Base.uasset").is_file())
            self.assertEqual(len(extractor.requests), 3)
            self.assertEqual([command[command.index("--") + 1] for command in runner.commands], [
                "inspect-map-batch", "blueprint-parents", "blueprint-parents",
            ])

    # Check same actor class keep map
    def test_same_actor_class_keeps_map_specific_blueprint_schema_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            helper = root / "helper.csproj"
            helper.write_text("test project", encoding="utf-8")
            runner = PerMapClassInspectionRunner()
            resolver = WorldLootDependencyResolver(
                extractor=FakeExtractor(root / "cache"),
                helper_project=helper,
                runner=runner,
            )
            first = replace(
                _location(),
                package_path="/Game/Map/MainWorld/MainWorld/_Generated_/Map001",
                export_name="BP_Pickup_C_UAID_8",
                actor_class="BP_Pickup_C",
                export_index=8,
            )
            second = replace(
                first,
                package_path="/Game/Map/MainWorld/DlcWorld/_Generated_/Map002",
                export_name="BP_Pickup_C_UAID_11",
                export_index=11,
            )

            result = resolver.stage_for_locations(
                game_root=root / "game",
                locations=(first, second),
                work_root=root / "generation",
                mappings_path=mappings,
                game_version="test-build",
                data_revision="current",
            )

            index = result.dependency_index
            assert index is not None
            self.assertEqual(
                index["maps"][first.package_path]["8"]["class_package"],
                "/Game/Blueprint/Pickup/BP_Generic",
            )
            self.assertEqual(
                index["maps"][second.package_path]["11"]["class_package"],
                "/Game/Blueprint/Pickup/BP_FlagChange",
            )
            self.assertEqual(result.class_packages, (
                "/Game/Blueprint/Pickup/BP_FlagChange",
                "/Game/Blueprint/Pickup/BP_Generic",
            ))
            self.assertEqual(sum("inspect-map-batch" in command for command in runner.commands), 1)

    # Check name map preparation class results
    def test_name_map_preparation_class_results_replace_a_second_map_discovery_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            helper = root / "helper.csproj"
            helper.write_text("test project", encoding="utf-8")
            location = replace(_location(), destination_kind="ARTICLE_BULK")
            resolver = WorldLootDependencyResolver(
                extractor=FakeExtractor(root / "cache"),
                helper_project=helper,
                runner=UnexpectedMapInspectionRunner(),
            )

            # Prepare map
            def prepare_map(*_args: object, class_results: dict[str, list[dict[str, object]]], **_kwargs: object) -> dict[str, object]:
                class_results[location.package_path.casefold()] = [{
                    "exportIndex": location.export_index,
                    "exportName": location.export_name,
                    "className": "Actor",
                    "packagePath": "/Script/Engine.Actor",
                }]
                return {}

            profile: list[tuple[str, dict[str, object]]] = []
            with (
                patch.object(resolver, "_ensure_helper_ready"),
                patch.object(resolver, "_prepare_worker_mappings", return_value=(mappings,)),
                patch.object(resolver, "_prepare_binary_map_descriptors", side_effect=prepare_map),
                patch("cv2randomizer.game.world_loot_dependencies.MemoryBudgetScheduler.recommended_worker_count", return_value=1),
            ):
                result = resolver.stage_for_locations(
                    game_root=root / "game",
                    locations=(location,),
                    work_root=root / "generation",
                    mappings_path=mappings,
                    game_version="test-build",
                    data_revision="current",
                    candidate_name_values=("DA_Item_1",),
                    prepared_cache_root=root / "prepared",
                    profile_callback=lambda operation, **fields: profile.append((operation, fields)),
                )

            self.assertEqual(result.dependency_index["maps"][location.package_path]["8"]["class_name"], "Actor")
            single_pass = [fields for operation, fields in profile
                           if operation == "stage" and fields.get("stage") == "single_pass_map_discovery_and_preparation"]
            self.assertEqual(len(single_pass), 1)
            self.assertEqual(single_pass[0]["prepared_discovery_maps"], 1)
            self.assertEqual(single_pass[0]["inspection_only_maps"], 0)


if __name__ == "__main__":
    unittest.main()
