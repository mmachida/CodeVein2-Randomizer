from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
from pathlib import Path

from cv2randomizer.game.asset_extraction import ExtractedAsset
from cv2randomizer.game.catalogs import MaterialLocation, RewardPayload
from cv2randomizer.game.material_dependencies import (
    MaterialClassProfile,
    MaterialClassValidation,
    MaterialDependencyResolver,
)


class MaterialClassProfileTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.resolver = MaterialDependencyResolver()

    # Check inherits true reset material ingredient
    def test_inherits_true_reset_from_material_ingredient_blueprint(self) -> None:
        metadata = {
            "/game/environment/item/bp_material": {
                "class_name": "BP_Material_C",
                "parent_package": "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_ItemIngredient",
                "reset": None,
                "parent_class_name": "BlueprintGeneratedClass",
            },
            "/game/blueprint/fieldgimmick/material/bp_pickupmaterial_itemingredient": {
                "class_name": "BP_PickupMaterial_ItemIngredient_C",
                "parent_package": "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base",
                "reset": True,
                "parent_class_name": "BlueprintGeneratedClass",
            },
            "/game/blueprint/fieldgimmick/material/bp_pickupmaterial_base": {
                "class_name": "BP_PickupMaterial_Base_C",
                "parent_package": None,
                "reset": None,
                "parent_class_name": "Class",
            },
        }
        profile = self.resolver._profile("/Game/Environment/Item/BP_Material", "BP_Material_C", metadata)

        self.assertTrue(profile.material_hierarchy)
        self.assertTrue(profile.effective_reset_on_yadorigi_rest)
        self.assertTrue(profile.eligible)
        self.assertIsNone(profile.reason)

    # Check does not guess absent reset
    def test_does_not_guess_absent_reset_value_or_accept_a_chest(self) -> None:
        unknown = {
            "/game/environment/item/bp_unknown": {
                "class_name": "BP_Unknown_C",
                "parent_package": "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base",
                "reset": None,
                "parent_class_name": "BlueprintGeneratedClass",
            },
            "/game/blueprint/fieldgimmick/material/bp_pickupmaterial_base": {
                "class_name": "BP_PickupMaterial_Base_C",
                "parent_package": None,
                "reset": None,
                "parent_class_name": "Class",
            },
        }
        profile = self.resolver._profile("/Game/Environment/Item/BP_Unknown", "BP_Unknown_C", unknown)
        self.assertTrue(profile.material_hierarchy)
        self.assertIsNone(profile.effective_reset_on_yadorigi_rest)
        self.assertFalse(profile.eligible)
        self.assertIn("absent", profile.reason or "")

        chest = {
            "/game/environment/item/bp_chest": {
                "class_name": "BP_Chest_C",
                "parent_package": "/Game/Blueprint/FieldGimmick/TreasureBox/BP_TreasureBox_Base",
                "reset": True,
                "parent_class_name": "BlueprintGeneratedClass",
            },
            "/game/blueprint/fieldgimmick/treasurebox/bp_treasurebox_base": {
                "class_name": "BP_TreasureBox_Base_C",
                "parent_package": None,
                "reset": None,
                "parent_class_name": "Class",
            },
        }
        profile = self.resolver._profile("/Game/Environment/Item/BP_Chest", "BP_Chest_C", chest)
        self.assertFalse(profile.material_hierarchy)
        self.assertFalse(profile.eligible)
        self.assertIn("outside", profile.reason or "")

    # Check nearest explicit false default stays
    def test_nearest_explicit_false_default_stays_locked(self) -> None:
        metadata = {
            "/game/environment/item/bp_material": {
                "class_name": "BP_Material_C",
                "parent_package": "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base",
                "reset": False,
                "parent_class_name": "BlueprintGeneratedClass",
            },
            "/game/blueprint/fieldgimmick/material/bp_pickupmaterial_base": {
                "class_name": "BP_PickupMaterial_Base_C",
                "parent_package": None,
                "reset": True,
                "parent_class_name": "Class",
            },
        }
        profile = self.resolver._profile("/Game/Environment/Item/BP_Material", "BP_Material_C", metadata)
        self.assertIs(False, profile.effective_reset_on_yadorigi_rest)
        self.assertFalse(profile.eligible)
        self.assertIn("false", profile.reason or "")

    # Check staging reuses prevalidated profiles reads
    def test_staging_reuses_prevalidated_profiles_and_only_reads_map_from_cache(self) -> None:
        class CacheExtractor:
            # Initialize this object
            def __init__(self, cache_root: Path) -> None:
                self.cache_root = cache_root
                self.requested: list[tuple[str, ...]] = []

            # Extract requested game assets
            def extract_assets(self, _game_root: Path, paths: object, **_kwargs: object) -> tuple[ExtractedAsset, ...]:
                requested = tuple(paths)
                self.requested.append(requested)
                result = []
                for package in requested:
                    name = package.rsplit("/", 1)[-1]
                    header = self.cache_root / f"{name}.umap"
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes(b"cached map header")
                    result.append(ExtractedAsset(package, (header,)))
                return tuple(result)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_root = root / "work"
            content_root = work_root / "source" / "Content"
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mappings")
            resource_package = "/Game/Environment/Item/BP_TestIngredient"
            location = MaterialLocation(
                package_path="/Game/Map/MainWorld/MainWorld/_Generated_/MaterialCacheTest",
                source_file="MaterialCacheTest.umap",
                export_index=5,
                export_name="BP_TestIngredient_C_UAID_123",
                actor_class="BP_TestIngredient_C",
                resource_class_package=resource_package,
                resource_class_name="BP_TestIngredient_C",
                reward=RewardPayload("/Game/Blueprint/Article/Item/Material/DA_TestIngredient", 1, True),
                save_id="test-save-id",
                protected=False,
                protection_reason=None,
            )
            profile = MaterialClassProfile(
                resource_package,
                location.resource_class_name,
                "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_ItemIngredient",
                True,
                True,
                True,
                None,
            )
            validation = MaterialClassValidation(content_root, (), (profile,))
            extractor = CacheExtractor(root / "cache")
            resolver = MaterialDependencyResolver(
                extractor=extractor,
                helper_project=root / "must-not-run.csproj",
            )

            staged = resolver.stage_for_locations(
                game_root=root / "game",
                locations=(location,),
                class_locations=(location,),
                work_root=work_root,
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                extractor=extractor,
                prevalidated_class_validation=validation,
            )
            self.assertTrue(staged.maps[0].primary_file.is_file())

        self.assertEqual(len(extractor.requested), 1)
        self.assertEqual(extractor.requested[0], (location.package_path,))
        self.assertEqual(staged.profiles, (profile,))
        self.assertEqual(staged.blueprint_packages, ())

    # Skip one incompatible class
    def test_class_validation_isolates_one_incompatible_blueprint(self) -> None:
        class Extractor:
            # Initialize this object
            def __init__(self, cache: Path) -> None:
                self.cache = cache

            # Extract requested packages
            def extract_assets(self, _game_root: Path, packages: object, **_kwargs: object) -> tuple[ExtractedAsset, ...]:
                results = []
                for package in packages:
                    package = str(package)
                    relative = Path(*package.split("/")[2:])
                    header = self.cache.joinpath(*relative.parts[:-1], relative.name + ".uasset")
                    header.parent.mkdir(parents=True, exist_ok=True)
                    header.write_bytes(package.encode("utf-8"))
                    results.append(ExtractedAsset(package, (header,)))
                return tuple(results)

        class Runner:
            # Initialize the incompatible class
            def __init__(self, bad_name: str) -> None:
                self.bad_name = bad_name.casefold()

            # Return Blueprint ancestry data
            def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                arguments = command[command.index("--") + 1:]
                files = arguments[4:]
                if any(Path(path).stem.casefold() == self.bad_name for path in files):
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        "",
                        "InvalidDataException: required Blueprint schemas could not be loaded",
                    )
                rows = []
                for path in files:
                    name = Path(path).stem
                    is_parent = name == "BP_PickupMaterial_ItemIngredient"
                    rows.append({
                        "classExportName": name + "_C",
                        "parentClassName": "Class" if is_parent else "BlueprintGeneratedClass",
                        "parentPackage": None if is_parent else "material parent path",
                        "resetOnYadorigiRest": True if is_parent else None,
                    })
                    if not is_parent:
                        rows[-1]["parentPackage"] = (
                            "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_ItemIngredient"
                        )
                return subprocess.CompletedProcess(command, 0, json.dumps(rows) + "\n", "")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "helper.csproj"
            helper.write_text("fixture", encoding="utf-8")
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            packages = (
                "/Game/Environment/Item/BP_TestIngredientGood",
                "/Game/Environment/Item/BP_TestIngredientBad",
            )
            locations = tuple(
                MaterialLocation(
                    package_path=f"/Game/Map/MainWorld/MainWorld/_Generated_/Map{index}",
                    source_file=f"Map{index}.umap",
                    export_index=index,
                    export_name=f"{Path(package).name}_UAID_{index}",
                    actor_class=Path(package).name + "_C",
                    resource_class_package=package,
                    resource_class_name=Path(package).name + "_C",
                    reward=RewardPayload(f"/Game/Blueprint/Article/Item/Material/DA_Test{index}", 1, True),
                    save_id=f"save-{index}",
                    protected=False,
                    protection_reason=None,
                )
                for index, package in enumerate(packages, start=1)
            )
            resolver = MaterialDependencyResolver(
                extractor=Extractor(root / "cache"),
                helper_project=helper,
                runner=Runner("BP_TestIngredientBad"),
            )

            validation = resolver.validate_classes(
                game_root=root / "game",
                locations=locations,
                work_root=root / "work",
                mappings_path=mappings,
                game_version="local-unverified-new-build",
                data_revision="current",
            )

        profiles = {profile.package_path: profile for profile in validation.profiles}
        self.assertTrue(profiles[packages[0]].eligible)
        self.assertFalse(profiles[packages[1]].eligible)
        self.assertIn("InvalidDataException", profiles[packages[1]].reason or "")


if __name__ == "__main__":
    unittest.main()
