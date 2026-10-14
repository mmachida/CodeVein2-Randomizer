from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from cv2randomizer.game.catalogs import CatalogStore, MaterialLocation, RewardPayload
from cv2randomizer.game.renewable_ingredients import renewable_material_locations
from cv2randomizer.randomizer.materials import (
    build_material_randomization_plan,
    material_slot,
)


# Build a test location record
def _location(
    index: int,
    class_number: int,
    *,
    protected: bool = False,
    reason: str | None = None,
    source_file: str | None = None,
    actor_class: str | None = None,
) -> MaterialLocation:
    package_leaf = f"BP_Material_{class_number}"
    return MaterialLocation(
        package_path=f"/Game/Map/MainWorld/MainWorld/_Generated_/Map_{index // 10}",
        source_file=source_file or f"Map_{index // 10}.umap",
        export_index=index + 1,
        export_name=f"{package_leaf}_C_UAID_{index}",
        actor_class=actor_class or package_leaf + "_C",
        resource_class_package=f"/Game/Environment/Item/{package_leaf}",
        resource_class_name=package_leaf + "_C",
        reward=RewardPayload(f"/Game/Blueprint/Article/Item/Material/DA_Item_{class_number}", 1, True),
        save_id=f"save-{index}",
        protected=protected,
        protection_reason=reason,
    )


class MaterialRandomizerTests(unittest.TestCase):
    # Check crafting material families are ingredient
    def test_crafting_material_families_are_in_the_ingredient_pool(self) -> None:
        root = Path(__file__).resolve().parents[1]
        catalogs = CatalogStore(root / "data" / "catalogs").load("current")
        locations = renewable_material_locations(catalogs.world_loot)
        target_classes = {
            "/Game/Environment/Item/it20/it20_001/BP_it20_001_A1_0",
            "/Game/Environment/Item/it20/it20_001/BP_it20_001_B1_0",
            "/Game/Environment/Item/it20/it20_001/BP_it20_001_C1_0",
            "/Game/Environment/Item/it20/it20_001/BP_it20_001_C2_0",
            "/Game/Environment/Item/it21/it21_008/BP_it21_008_A_0",
            "/Game/Environment/Item/it21/it21_008/BP_it21_008_A_1",
            "/Game/Environment/Item/it21/it21_012/BP_it21_012_A_0",
            "/Game/Environment/Item/it22/it22_004/BP_it22_004_X1_0",
        }
        target_locations = [
            item for item in locations
            if item.resource_class_package.casefold() in {value.casefold() for value in target_classes}
        ]
        self.assertEqual(len(target_locations), 198)
        self.assertEqual(
            {item.resource_class_package.casefold() for item in target_locations},
            {value.casefold() for value in target_classes},
        )

        # Extract supplies the validated class set to this planner in Generate.
        plan = build_material_randomization_plan(
            locations,
            "craft-material-catalog-check",
            validated_class_packages={item.resource_class_package for item in locations},
            enabled=True,
        )
        target_assignments = [
            item for item in plan.assignments
            if item.destination.resource_class_package.casefold() in {value.casefold() for value in target_classes}
        ]
        self.assertEqual(len(target_assignments), len(target_locations))
        self.assertTrue(all(
            item.destination.resource_class_package.casefold() != item.source.resource_class_package.casefold()
            for item in target_assignments
        ))

    # Check deterministic global type mapping applies
    def test_deterministic_global_type_mapping_applies_to_every_pickup(self) -> None:
        locations = [
            _location(0, 1), _location(1, 1), _location(10, 1),
            _location(2, 2), _location(12, 2),
            _location(3, 3), _location(13, 3),
        ]
        valid = {item.resource_class_package for item in locations}
        first = build_material_randomization_plan(
            locations, "materials-seed", validated_class_packages=valid,
        )
        second = build_material_randomization_plan(
            locations, "materials-seed", validated_class_packages=valid,
        )

        self.assertEqual(
            [(material_slot(item.destination), material_slot(item.source)) for item in first.assignments],
            [(material_slot(item.destination), material_slot(item.source)) for item in second.assignments],
        )
        mapping = {}
        for assignment in first.assignments:
            original = assignment.destination.resource_class_package
            assigned = assignment.source.resource_class_package
            self.assertEqual(mapping.setdefault(original, assigned), assigned)
        self.assertEqual(set(mapping), set(mapping.values()))
        self.assertTrue(all(original != assigned for original, assigned in mapping.items()))

    # Check valid material reset profiles enter
    def test_only_valid_material_reset_profiles_enter_the_pool(self) -> None:
        material_a, material_b = _location(0, 1), _location(1, 2)
        unknown_reset = _location(2, 3)
        chest_class = _location(3, 4)
        protected = _location(4, 5, protected=True, reason="catalog protection")
        unsafe = _location(5, 6, source_file="unsafe.umap")
        renamed_instance = replace(
            _location(6, 7), export_name="BP_Chest_C_UAID_6",
        )
        locations = [material_a, material_b, unknown_reset, chest_class, protected, unsafe, renamed_instance]
        plan = build_material_randomization_plan(
            locations,
            14,
            validated_class_packages={
                material_a.resource_class_package,
                material_b.resource_class_package,
                renamed_instance.resource_class_package,
                protected.resource_class_package,
                unsafe.resource_class_package,
            },
            invalid_class_reasons={
                unknown_reset.resource_class_package.casefold(): "bResetOnYadorigiRest is unvalidated",
                chest_class.resource_class_package.casefold(): "class is not a material actor",
            },
            unsafe_packages={"unsafe.umap"},
        )

        self.assertEqual(len(plan.assignments), 3)
        self.assertEqual(
            {item.location for item in plan.locked_locations},
            {unknown_reset, chest_class, protected, unsafe},
        )
        reasons = {item.location: item.reason for item in plan.locked_locations}
        self.assertEqual(reasons[unknown_reset], "bResetOnYadorigiRest is unvalidated")
        self.assertEqual(reasons[chest_class], "class is not a material actor")
        self.assertEqual(reasons[unsafe], "unsafe package")
        self.assertNotIn(renamed_instance, reasons)

    # Check instance export name prefix does
    def test_instance_export_name_prefix_does_not_override_structural_class(self) -> None:
        first = _location(0, 1)
        variant = replace(first, export_index=2, export_name="BP_Material_DisplayVariant_C_UAID_1")
        second = _location(2, 2)
        valid = {first.resource_class_package, second.resource_class_package}

        plan = build_material_randomization_plan(
            (first, variant, second), "ingredient-actor-variant", validated_class_packages=valid,
        )

        self.assertEqual(len(plan.assignments), 3)
        self.assertFalse(plan.locked_locations)
        variant_assignment = next(item for item in plan.assignments if item.destination == variant)
        self.assertEqual(
            variant_assignment.source.resource_class_package,
            next(item for item in plan.assignments if item.destination == first).source.resource_class_package,
        )
        self.assertEqual(variant.actor_class_package, variant.resource_class_package)

    # Check world loot export prefix is
    def test_world_loot_export_prefix_is_replaced_with_structural_class(self) -> None:
        root = Path(__file__).resolve().parents[1]
        catalogs = CatalogStore(root / "data" / "catalogs").load("current")
        world_location = next(
            item for item in catalogs.world_loot
            if item.package_path.endswith("/21EL2QE3B3183JGSUQFWDD08O") and item.export_index == 24
        )
        material = renewable_material_locations((world_location,))[0]

        self.assertEqual(world_location.actor_class, "BP_it23_001_A_16_C")
        self.assertEqual(material.resource_class_name, "BP_it23_001_A_12_C")
        self.assertEqual(material.actor_class, material.resource_class_name)
        self.assertEqual(material.actor_class_package, material.resource_class_package)

    # Check renewable mushroom food pickups all
    def test_renewable_mushroom_and_food_pickups_all_receive_one_global_replacement(self) -> None:
        root = Path(__file__).resolve().parents[1]
        catalogs = CatalogStore(root / "data" / "catalogs").load("current")
        locations = catalogs.materials
        targets = {
            "DA_Item_IngredientMushroom_1",
            "DA_Item_IngredientOnion_1",
            "DA_Item_IngredientRice_1",
            "DA_Item_IngredientVegetablesC_1",
        }
        target_locations = [
            item for item in locations
            if item.reward.article_path.rsplit("/", 1)[-1] in targets
        ]
        validated_classes = {
            package
            for item in locations
            for package in (item.resource_class_package, item.actor_class_package)
            if package
        }

        plan = build_material_randomization_plan(
            locations,
            "renewable-food-ingredient-coverage",
            validated_class_packages=validated_classes,
        )

        assignments_by_article = {}
        for assignment in plan.assignments:
            article = assignment.destination.reward.article_path.rsplit("/", 1)[-1]
            if article in targets:
                assignments_by_article.setdefault(article, []).append(assignment)
        self.assertEqual(set(assignments_by_article), targets)
        for article in targets:
            expected_count = sum(
                item.reward.article_path.rsplit("/", 1)[-1] == article
                for item in target_locations
            )
            assignments = assignments_by_article[article]
            self.assertEqual(len(assignments), expected_count)
            self.assertEqual(len({item.source.resource_class_package for item in assignments}), 1)
            self.assertTrue(all(
                item.destination.resource_class_package != item.source.resource_class_package
                for item in assignments
            ))

    # Check material type mapping is global
    def test_material_type_mapping_is_global_across_maps(self) -> None:
        locations = [
            _location(0, 1), _location(10, 1),
            _location(1, 2), _location(11, 2),
        ]
        plan = build_material_randomization_plan(
            locations, 5, validated_class_packages={item.resource_class_package for item in locations},
        )

        mapping = {}
        for assignment in plan.assignments:
            mapping.setdefault(
                assignment.destination.resource_class_package,
                assignment.source.resource_class_package,
            )
            self.assertEqual(
                mapping[assignment.destination.resource_class_package],
                assignment.source.resource_class_package,
            )
        self.assertNotEqual(mapping[locations[0].resource_class_package], locations[0].resource_class_package)
        self.assertNotEqual(mapping[locations[2].resource_class_package], locations[2].resource_class_package)

    # Check shared reward article gets one
    def test_shared_reward_article_gets_one_replacement_across_blueprints(self) -> None:
        mushroom_a = _location(0, 1)
        mushroom_b = _location(1, 2)
        onion = _location(2, 3)
        mushroom_article = "/Game/Blueprint/Article/Item/Material/DA_Item_IngredientMushroom_1"
        onion_article = "/Game/Blueprint/Article/Item/Material/DA_Item_IngredientOnion_1"
        mushroom_a = replace(mushroom_a, reward=RewardPayload(mushroom_article, 1, True))
        mushroom_b = replace(mushroom_b, reward=RewardPayload(mushroom_article, 1, True))
        onion = replace(onion, reward=RewardPayload(onion_article, 1, True))
        locations = (mushroom_a, mushroom_b, onion)
        plan = build_material_randomization_plan(
            locations,
            "shared-mushroom-reward",
            validated_class_packages={item.resource_class_package for item in locations},
        )

        mapped_by_class = {
            assignment.destination.resource_class_package: assignment.source.reward.article_path
            for assignment in plan.assignments
        }
        self.assertEqual(mapped_by_class[mushroom_a.resource_class_package], mapped_by_class[mushroom_b.resource_class_package])
        self.assertNotEqual(mapped_by_class[mushroom_a.resource_class_package].casefold(), mushroom_article.casefold())
        self.assertNotEqual(mapped_by_class[onion.resource_class_package].casefold(), onion_article.casefold())

    # Check reward shuffle can cross blueprint
    def test_reward_shuffle_can_cross_blueprint_parent_families(self) -> None:
        locations = [_location(index, index + 1) for index in range(2)]
        families = {
            locations[0].resource_class_package: "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base",
            locations[1].resource_class_package: "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_ItemIngredient",
        }
        # These two locations can have unrelated visual Blueprint parents; the
        # shuffle changes their shared CDO reward and never swaps placed actors.
        plan = build_material_randomization_plan(
            locations, 5,
            validated_class_packages={item.resource_class_package for item in locations},
        )

        mapping = {}
        self.assertTrue(all(
            assignment.destination.resource_class_package != assignment.source.resource_class_package
            for assignment in plan.assignments
        ))
        crossed_family = False
        for assignment in plan.assignments:
            destination = assignment.destination.resource_class_package
            source = assignment.source.resource_class_package
            crossed_family = crossed_family or families[destination] != families[source]
            self.assertEqual(mapping.setdefault(destination, source), source)
        self.assertEqual(set(mapping), set(mapping.values()))
        self.assertTrue(crossed_family)

    # Check mirrored map rows follow one
    def test_mirrored_map_rows_follow_one_canonical_ingredient_assignment(self) -> None:
        first, second = _location(0, 1), _location(1, 2)
        first_mirror = replace(
            first,
            package_path="/Game/Map/MainWorld/DlcWorld/_Generated_/MirrorA",
            source_file="MirrorA.umap",
            export_index=101,
            export_name=first.export_name,
            mirror_of_package_path=first.package_path,
            mirror_of_export_index=first.export_index,
        )
        second_mirror = replace(
            second,
            package_path="/Game/Map/MainWorld/DlcWorld/_Generated_/MirrorB",
            source_file="MirrorB.umap",
            export_index=102,
            export_name=second.export_name,
            mirror_of_package_path=second.package_path,
            mirror_of_export_index=second.export_index,
        )
        locations = (first, second, first_mirror, second_mirror)
        plan = build_material_randomization_plan(
            locations,
            "mirrored-ingredients",
            validated_class_packages={item.resource_class_package for item in locations},
        )

        assigned = {material_slot(item.destination): item.source for item in plan.assignments}
        self.assertEqual(len(plan.assignments), 4)
        self.assertEqual(assigned[material_slot(first_mirror)], assigned[material_slot(first)])
        self.assertEqual(assigned[material_slot(second_mirror)], assigned[material_slot(second)])
        self.assertEqual(
            sorted(assigned[material_slot(item)].resource_class_package for item in (first, second)),
            sorted(item.resource_class_package for item in (first, second)),
        )

    # Check mirror different reward fail closed
    def test_mirror_with_different_reward_fails_closed(self) -> None:
        canonical = _location(0, 1)
        mirror = replace(
            canonical,
            package_path="/Game/Map/MainWorld/DlcWorld/_Generated_/MirrorA",
            source_file="MirrorA.umap",
            export_index=101,
            mirror_of_package_path=canonical.package_path,
            mirror_of_export_index=canonical.export_index,
            reward=RewardPayload("/Game/Blueprint/Article/Item/Material/DA_Item_2", 1, True),
        )
        with self.assertRaisesRegex(ValueError, "mirror does not match"):
            build_material_randomization_plan(
                (canonical, mirror), 7,
                validated_class_packages={canonical.resource_class_package},
            )

    # Check global mapping can use class
    def test_global_mapping_can_use_a_class_absent_from_a_destination_map(self) -> None:
        first, second = _location(0, 1), _location(1, 2)
        first_mirror = replace(
            first,
            package_path="/Game/Map/MainWorld/DlcWorld/_Generated_/MirrorA",
            source_file="MirrorA.umap",
            export_index=101,
            mirror_of_package_path=first.package_path,
            mirror_of_export_index=first.export_index,
        )
        # Generate stages and validates the assigned class/CDO import even if
        # this map has no existing pickup of the target ingredient type.
        plan = build_material_randomization_plan(
            (first, second, first_mirror),
            "mirror-class-import-boundary",
            validated_class_packages={first.resource_class_package, second.resource_class_package},
        )

        assigned = {material_slot(item.destination): item.source for item in plan.assignments}
        self.assertEqual(assigned[material_slot(first)], assigned[material_slot(first_mirror)])
        self.assertNotEqual(
            assigned[material_slot(first)].resource_class_package,
            first.resource_class_package,
        )
        self.assertEqual(
            assigned[material_slot(first)].resource_class_package,
            second.resource_class_package,
        )

    # Check one invalid location locks all
    def test_one_invalid_location_locks_all_instances_of_its_type(self) -> None:
        valid_a = _location(0, 1)
        protected_a = _location(1, 1, protected=True)
        valid_b = _location(2, 2)
        plan = build_material_randomization_plan(
            (valid_a, protected_a, valid_b), 8,
            validated_class_packages={valid_a.resource_class_package, valid_b.resource_class_package},
        )

        self.assertEqual(len(plan.assignments), 1)
        self.assertEqual(
            plan.assignments[0].destination.resource_class_package,
            plan.assignments[0].source.resource_class_package,
        )
        self.assertEqual(
            {item.location for item in plan.locked_locations},
            {valid_a, protected_a},
        )

    # Check disabled feature single class pool
    def test_disabled_feature_and_single_class_pool_make_no_edits(self) -> None:
        locations = [_location(index, 1) for index in range(3)]
        valid = {locations[0].resource_class_package}
        disabled = build_material_randomization_plan(
            locations, 1, validated_class_packages=valid,
            enabled=False,
        )
        self.assertFalse(disabled.assignments)
        self.assertEqual(set(disabled.disabled_locations), set(locations))

        enabled = build_material_randomization_plan(
            locations, 1, validated_class_packages=valid,
        )
        self.assertEqual(len(enabled.assignments), len(locations))
        self.assertTrue(all(
            item.destination.resource_class_package == item.source.resource_class_package
            for item in enabled.assignments
        ))

    # Check duplicate invalid catalog slots fail
    def test_duplicate_or_invalid_catalog_slots_fail_closed(self) -> None:
        first = _location(0, 1)
        with self.assertRaisesRegex(ValueError, "Duplicate material"):
            build_material_randomization_plan(
                [first, first], 3, validated_class_packages={first.resource_class_package},
            )
        invalid_index = replace(first, export_index=0)
        with self.assertRaisesRegex(ValueError, "positive Unreal"):
            build_material_randomization_plan(
                [invalid_index], 3, validated_class_packages={first.resource_class_package},
            )


if __name__ == "__main__":
    unittest.main()
