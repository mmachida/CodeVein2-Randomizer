from __future__ import annotations

import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from cv2randomizer.game.asset_extraction import MaterializedAsset
from cv2randomizer.game.catalogs import MaterialLocation, RewardPayload
from cv2randomizer.game.material_dependencies import MaterialClassProfile, MaterialClassValidation
from cv2randomizer.game.material_generation import (
    MaterialGenerationError,
    MaterialPlanPreparation,
    apply_material_plan,
    prepare_material_plan,
)
from cv2randomizer.game.material_serialization import (
    MaterialCdoRewardPatchResult,
    MaterialPackagePatchResult,
)
from cv2randomizer.game.world_loot_serialization import ArticleReward
from cv2randomizer.randomizer.materials import MaterialAssignment, MaterialRandomizationPlan


# Build a test location record
def _location(index: int, class_name: str, article: str | None = None) -> MaterialLocation:
    package = f"/Game/Environment/Item/BP_Material_{class_name}"
    return MaterialLocation(
        "/Game/Map/MainWorld/MainWorld/_Generated_/Map001",
        "Map001.umap",
        index,
        f"BP_Material_{class_name}_C_UAID_{index}",
        f"BP_Material_{class_name}_C",
        package,
        f"BP_Material_{class_name}_C",
        RewardPayload(article or f"/Game/Blueprint/Article/Item/Material/DA_{class_name}", 1, True),
        f"save-{index}",
        False,
        None,
    )


# Build the source profile
def _profile(location: MaterialLocation, parent: str = "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base") -> MaterialClassProfile:
    return MaterialClassProfile(
        location.resource_class_package,
        location.resource_class_name,
        parent,
        True,
        True,
        True,
        None,
    )


class FakeResolver:
    # Initialize this object
    def __init__(self, profiles: tuple[MaterialClassProfile, ...]) -> None:
        self.profiles = profiles

    # Validate classes
    def validate_classes(self, *, locations: object, work_root: Path, **_kwargs: object) -> MaterialClassValidation:
        content = work_root / "source" / "Content"
        requested = {location.resource_class_package.casefold(): location for location in locations}
        assets = []
        profiles = []
        for profile in self.profiles:
            if profile.package_path.casefold() not in requested:
                continue
            relative = Path(*profile.package_path.split("/")[2:])
            header = content / relative.with_suffix(".uasset")
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_bytes(b"staged Blueprint header")
            header.with_suffix(".uexp").write_bytes(b"staged Blueprint exports")
            assets.append(MaterializedAsset(profile.package_path, (header, header.with_suffix(".uexp"))))
            profiles.append(profile)
        return MaterialClassValidation(content, tuple(assets), tuple(profiles))


class FakeSerializer:
    # Initialize this object
    def __init__(self) -> None:
        self.patches = []

    # Patch package reward data
    def patch_packages(self, *, packages: object, **_kwargs: object) -> tuple[MaterialPackagePatchResult, ...]:
        results = []
        for patch in packages:
            self.patches.append(patch)
            patch.output_asset.write_bytes(b"patched Blueprint header")
            patch.output_asset.with_suffix(".uexp").write_bytes(b"patched Blueprint exports")
            result = MaterialCdoRewardPatchResult(
                patch.class_package,
                patch.class_name,
                f"Default__{patch.class_name}",
                2,
                patch.expected_original,
                patch.assigned,
            )
            results.append(MaterialPackagePatchResult(
                patch.input_asset, patch.output_asset, (result,),
            ))
        return tuple(results)


class MaterialGenerationTests(unittest.TestCase):
    # Check prepare reuses extract time profiles
    def test_prepare_reuses_extract_time_profiles_without_rescanning(self) -> None:
        class ResolverThatMustNotValidate:
            # Validate classes
            def validate_classes(self, **_kwargs: object) -> MaterialClassValidation:
                raise AssertionError("complete Extract profiles should avoid a second planning scan")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = _location(1, "A"), _location(2, "B")
            profiles = (_profile(first), _profile(second))
            cached = {profile.package_path.casefold(): asdict(profile) for profile in profiles}
            prepared = prepare_material_plan(
                (first, second), "cache-profile-reuse",
                game_root=root / "game", work_root=root / "work", mappings_path=root / "CV2.usmap",
                game_version="test", data_revision="current",
                resolver=ResolverThatMustNotValidate(), prepared_class_profiles=cached,
            )

        self.assertEqual(prepared.class_validation.profiles, profiles)
        self.assertEqual(len(prepared.plan.assignments), 2)

    # Check extract physical reset audit promotes
    def test_extract_physical_reset_audit_promotes_missing_cdo_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            location = _location(1, "A")
            cached = asdict(MaterialClassProfile(
                location.resource_class_package, location.resource_class_name,
                "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base",
                True, None, False,
                "bResetOnYadorigiRest is absent from the staged Blueprint ancestry",
            ))
            cached.update({
                "catalogued_effective_reset_on_yadorigi_rest": True,
                "reset_validation_source": "catalogued_physical_audit",
            })
            prepared = prepare_material_plan(
                (location,), 4,
                game_root=root / "game", work_root=root / "work", mappings_path=root / "CV2.usmap",
                game_version="test", data_revision="current",
                resolver=object(),
                prepared_class_profiles={location.resource_class_package.casefold(): cached},
            )

        self.assertTrue(prepared.class_validation.profiles[0].eligible)
        self.assertTrue(prepared.class_validation.profiles[0].effective_reset_on_yadorigi_rest)
        self.assertEqual(len(prepared.plan.assignments), 1)

    # Check apply changes shared CDO rewards
    def test_apply_changes_shared_cdo_rewards_and_emits_no_map_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mappings")
            first, second = _location(1, "A"), _location(2, "B")
            profiles = (_profile(first), _profile(second))
            plan = MaterialRandomizationPlan(
                (MaterialAssignment(first, second), MaterialAssignment(second, first)), (), (),
            )
            serializer = FakeSerializer()
            result = apply_material_plan(
                MaterialPlanPreparation(plan, MaterialClassValidation(root / "work" / "source" / "Content", (), profiles)),
                game_root=root / "game",
                work_root=root / "work",
                output_content_root=root / "work" / "output" / "Content",
                mappings_path=mappings,
                game_version="test",
                data_revision="current",
                resolver=FakeResolver(profiles),
                serializer=serializer,
            )

            self.assertEqual(result.assignment_count, 2)
            self.assertEqual(result.cdo_reward_patch_count, 2)
            self.assertEqual(result.locked_count, 0)
            self.assertEqual(len(serializer.patches), 2)
            self.assertEqual({patch.class_package for patch in serializer.patches}, {
                first.resource_class_package, second.resource_class_package,
            })
            self.assertEqual({path.suffix for path in result.output_packages}, {".uasset", ".uexp"})
            self.assertFalse(any(path.suffix.casefold() in {".umap"} for path in result.output_packages))
            first_patch = next(patch for patch in serializer.patches if patch.class_package == first.resource_class_package)
            self.assertEqual(first_patch.expected_original.article_data_path, first.reward.article_path)
            self.assertEqual(first_patch.assigned.article_data_path, second.reward.article_path)
            self.assertEqual(first_patch.assigned.count, first_patch.expected_original.count)
            self.assertEqual(first_patch.assigned.b_notification, first_patch.expected_original.b_notification)

    # Check shared article patches every CDO
    def test_shared_article_patches_every_cdo_owner_with_one_consistent_reward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mappings")
            first = _location(1, "A", "/Game/Blueprint/Article/Item/Material/DA_Mushroom")
            shared_owner = _location(2, "B", "/Game/Blueprint/Article/Item/Material/DA_Mushroom")
            other = _location(3, "C", "/Game/Blueprint/Article/Item/Material/DA_Onion")
            profiles = tuple(_profile(item) for item in (first, shared_owner, other))
            plan = MaterialRandomizationPlan((
                MaterialAssignment(first, other),
                MaterialAssignment(shared_owner, other),
                MaterialAssignment(other, first),
            ), (), ())
            serializer = FakeSerializer()
            apply_material_plan(
                MaterialPlanPreparation(plan, MaterialClassValidation(root / "work" / "source" / "Content", (), profiles)),
                game_root=root / "game", work_root=root / "work",
                output_content_root=root / "work" / "output" / "Content",
                mappings_path=mappings, game_version="test", data_revision="current",
                resolver=FakeResolver(profiles), serializer=serializer,
            )

            mushroom_patches = [
                patch for patch in serializer.patches
                if patch.expected_original.article_data_path.endswith("DA_Mushroom")
            ]
            self.assertEqual(len(mushroom_patches), 2)
            self.assertEqual(
                {patch.assigned.article_data_path for patch in mushroom_patches},
                {other.reward.article_path},
            )

    # Check parent family does not limit
    def test_parent_family_does_not_limit_reward_shuffle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mappings")
            first, second = _location(1, "A"), _location(2, "B")
            profiles = (
                _profile(first, "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_Base"),
                _profile(second, "/Game/Blueprint/FieldGimmick/Material/BP_PickupMaterial_ItemIngredient"),
            )
            plan = MaterialRandomizationPlan(
                (MaterialAssignment(first, second), MaterialAssignment(second, first)), (), (),
            )
            result = apply_material_plan(
                MaterialPlanPreparation(plan, MaterialClassValidation(root / "work" / "source" / "Content", (), profiles)),
                game_root=root / "game", work_root=root / "work",
                output_content_root=root / "work" / "output" / "Content",
                mappings_path=mappings, game_version="test", data_revision="current",
                resolver=FakeResolver(profiles), serializer=FakeSerializer(),
            )

        self.assertEqual(result.cdo_reward_patch_count, 2)

    # Check reject one article mapping multiple
    def test_rejects_one_article_mapping_to_multiple_rewards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mappings")
            same_article_a = _location(1, "A", "/Game/Blueprint/Article/Item/Material/DA_Shared")
            same_article_b = _location(2, "B", "/Game/Blueprint/Article/Item/Material/DA_Shared")
            source_a = _location(3, "C")
            source_b = _location(4, "D")
            profiles = tuple(_profile(item) for item in (same_article_a, same_article_b, source_a, source_b))
            plan = MaterialRandomizationPlan((
                MaterialAssignment(same_article_a, source_a),
                MaterialAssignment(same_article_b, source_b),
            ), (), ())
            with self.assertRaisesRegex(MaterialGenerationError, "same renewable ingredient Article"):
                apply_material_plan(
                    MaterialPlanPreparation(plan, MaterialClassValidation(root / "work" / "source" / "Content", (), profiles)),
                    game_root=root / "game", work_root=root / "work",
                    output_content_root=root / "work" / "output" / "Content",
                    mappings_path=mappings, game_version="test", data_revision="current",
                    resolver=FakeResolver(profiles), serializer=FakeSerializer(),
                )


if __name__ == "__main__":
    unittest.main()
