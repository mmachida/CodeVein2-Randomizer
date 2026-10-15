from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.game.article_reward_catalog import CanonicalArticleRewardCatalogStore
from cv2randomizer.game.catalogs import CatalogStore, RewardPayload
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.game.field_enemy_catalog import FieldEnemyCatalogError, FieldEnemyCatalogStore
from cv2randomizer.game.jail_catalog import JailCatalogStore
from cv2randomizer.game.raw_article_grant_generation import (
    RawArticleGrantGenerationError,
    _validate_quest_source_fingerprint,
)
from cv2randomizer.randomizer.article_rewards import canonical_article_world_candidates
from cv2randomizer.randomizer.defensive_gadgets import defensive_gadget_world_candidates
from cv2randomizer.randomizer.jails import jail_world_candidates
from cv2randomizer.randomizer.world_loot import WorldLootRewardCandidate, build_world_loot_randomization_plan
from cv2randomizer.generation.pipeline import _field_enemy_manifest_counts
from tests._content_class_fixtures import classified_world_fixture


_ROOT = Path(__file__).resolve().parents[1]
_FIELD_ROOT = _ROOT / "data" / "field_enemy"


class FieldEnemyCatalogTests(unittest.TestCase):
    # Prepare the test fixture
    def setUp(self) -> None:
        self.runtime = CatalogStore(_ROOT / "data" / "catalogs").load("current")
        self.catalog = FieldEnemyCatalogStore(_FIELD_ROOT).load(
            "current", important_item_exclusions=self.runtime.important_item_exclusions,
        )

    # Check archive line endings validate
    def test_field_enemy_store_accepts_lf_catalog_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied_root = Path(temporary) / "field_enemy"
            shutil.copytree(_FIELD_ROOT, copied_root)
            current_dir = copied_root / "current"
            for filename in ("field_enemy_direct_rewards.json", "field_enemy_key_exclusions.json"):
                path = current_dir / filename
                path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
            loaded = FieldEnemyCatalogStore(copied_root).load(
                "current", important_item_exclusions=self.runtime.important_item_exclusions,
            )
            self.assertEqual(len(loaded.locations), 24)
            self.assertEqual(len(loaded.protected_exclusions), 10)

    # Check catalog keep all 24 where
    def test_catalog_keeps_all_24_where_slots_and_duplicate_rewards(self) -> None:
        self.assertEqual(len(self.catalog.locations), 24)
        self.assertEqual(len(self.catalog.protected_exclusions), 10)
        self.assertEqual(self.catalog.category_counts, (
            ("DEFENSIVE_GADGET", 1),
            ("ENHANCEMENT_MATERIAL", 5),
            ("GADGET", 17),
            ("WEAPON", 1),
        ))
        self.assertEqual(self.catalog.quest_group_counts, (
            ("UNI_C", 7), ("UNI_FO", 3), ("UNI_P", 9), ("UNI_VO", 5),
        ))
        self.assertTrue(all(slot.content_class == "DLC" for slot in self.catalog.locations))
        self.assertTrue(all(slot.destination_kind == "QUEST_ARTICLE_GRANT" for slot in self.catalog.locations))
        self.assertEqual(len(self.catalog.destination_ids), 24)
        self.assertEqual(
            Counter(slot.rewards[0].article_path for slot in self.catalog.locations),
            Counter({
                "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpWeaponA_10": 2,
                "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpWeaponB_05": 2,
                **{
                    slot.rewards[0].article_path: 1 for slot in self.catalog.locations
                    if slot.rewards[0].article_path.rsplit("/", 1)[-1]
                    not in {"DA_Item_MatLevelUpWeaponA_10", "DA_Item_MatLevelUpWeaponB_05"}
                },
            }),
        )

    # Check protected field enemy keys match
    def test_protected_field_enemy_keys_match_global_exact_key_identities(self) -> None:
        exclusions = self.runtime.important_item_exclusions
        assert exclusions is not None
        self.assertEqual(len(self.catalog.protected_exclusions), 10)
        self.assertTrue(all(
            item.article_path.casefold() in exclusions.key_identities
            for item in self.catalog.protected_exclusions
        ))
        self.assertTrue(self.catalog.destination_ids.isdisjoint(
            item.destination_id for item in self.catalog.protected_exclusions
        ))
        self.assertNotIn(
            "/Game/Blueprint/Article/Item/Important/DA_Item_Important_DLC_ForestKey02".casefold(),
            {slot.rewards[0].article_path.casefold() for slot in self.catalog.locations},
        )

    # Check field enemy use each available
    def test_field_enemy_uses_each_available_source_package_independently(self) -> None:
        self.assertTrue(all(slot.rewards[0].content_class == "UNKNOWN" for slot in self.catalog.locations))
        inventory = PackageInventory.create(slot.package_path for slot in self.catalog.locations)

        self.assertEqual(len(self.catalog.required_package_paths(inventory)), 24)
        self.assertEqual(len(self.catalog.eligible_locations(inventory)), 24)
        self.assertEqual(self.catalog.required_package_paths(PackageInventory.create(())), ())
        partial = PackageInventory.create(slot.package_path for slot in self.catalog.locations[:-1])
        self.assertEqual(len(self.catalog.required_package_paths(partial)), 23)
        self.assertEqual(len(self.catalog.eligible_locations(partial)), 23)

    # Check field enemy rewards preserve researched
    def test_field_enemy_rewards_preserve_the_researched_serialized_fname_identity(self) -> None:
        for slot in self.catalog.locations:
            reward = slot.rewards[0]
            layout = slot.quest_article_layout
            self.assertIsNotNone(reward.soft_object_path_identity)
            self.assertIsNotNone(layout)
            identity = reward.soft_object_path_identity
            assert identity is not None and layout is not None
            self.assertEqual(identity.package_name.value, layout.expected_package_name_value)
            self.assertEqual(identity.package_name.number, layout.expected_package_name_number)
            self.assertEqual(identity.asset_name.value, layout.expected_asset_name_value)
            self.assertEqual(identity.asset_name.number, layout.expected_asset_name_number)
            self.assertEqual(identity.resolved_path, reward.article_path)
            self.assertEqual(identity.resolved_asset_name, reward.article_path.rsplit("/", 1)[-1])

    # Check catalog fail closed manifest tampering
    def test_catalog_fails_closed_on_manifest_tampering_and_missing_global_key_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied_root = Path(temporary) / "field_enemy"
            shutil.copytree(_FIELD_ROOT, copied_root)
            direct = copied_root / "current" / "field_enemy_direct_rewards.json"
            document = json.loads(direct.read_text(encoding="utf-8"))
            document["locations"][0]["rewards"][0]["article_path"] = "/Game/Blueprint/Article/Item/Support/DA_Changed"
            direct.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(FieldEnemyCatalogError, "signature"):
                FieldEnemyCatalogStore(copied_root).load(
                    "current", important_item_exclusions=self.runtime.important_item_exclusions,
                )

        exclusions = self.runtime.important_item_exclusions
        assert exclusions is not None
        missing_identity = self.catalog.protected_exclusions[0].article_path.casefold()
        reduced = replace(
            exclusions,
            keys=tuple(path for path in exclusions.keys if path.casefold() != missing_identity),
        )
        with self.assertRaisesRegex(FieldEnemyCatalogError, "global exact Key exclusion"):
            FieldEnemyCatalogStore(_FIELD_ROOT).load("current", important_item_exclusions=reduced)

    # Check world pool preserve duplicate field
    def test_world_pool_preserves_duplicate_field_enemy_where_slots_and_seed_determinism(self) -> None:
        # Runtime FieldEnemy WHEREs can use a replacement pool even though their
        # vanilla WHAT classification remains UNKNOWN.
        locations = classified_world_fixture((*self.runtime.world_loot, *self.catalog.locations))
        kwargs = {
            "unsafe_packages": self.runtime.unsafe_packages,
            "supported_quest_grant_destinations": self.catalog.destination_ids,
        }
        first = build_world_loot_randomization_plan(locations, "field-enemy-seed", **kwargs)
        repeated = build_world_loot_randomization_plan(locations, "field-enemy-seed", **kwargs)
        self.assertEqual(first, repeated)
        field_assignments = [
            assignment for assignment in first.assignments
            if assignment.destination.destination_id in self.catalog.destination_ids
        ]
        self.assertEqual(len(field_assignments), 24)
        canonical_assignments = [
            item for item in first.assignments
            if item.destination.mirror_of_package_path is None
        ]
        self.assertEqual(
            Counter(reward.article_path.casefold() for item in canonical_assignments for reward in item.assigned_rewards),
            Counter(first.source_article_paths),
        )
        recipe_prefix = "/game/blueprint/article/item/important/da_item_important_recipe_"
        recipe_articles = [path for path in first.source_article_paths if path.startswith(recipe_prefix)]
        self.assertEqual(len(recipe_articles), len(set(recipe_articles)))

    # Check healing assignments keep exact FName
    def test_healing_assignments_keep_exact_fname_when_world_sources_mix(self) -> None:
        gadgets = DefensiveGadgetCatalogStore(_ROOT / "data" / "reward_pools" / "current").load("current")
        jails = JailCatalogStore(_ROOT / "data" / "jails" / "current").load()
        articles = CanonicalArticleRewardCatalogStore(_ROOT / "data" / "reward_pools" / "current").load("current")
        candidates = (
            *defensive_gadget_world_candidates(gadgets),
            *jail_world_candidates(jails, world_loot=self.runtime.world_loot),
            *canonical_article_world_candidates(articles, include_boosters=False),
        )
        plan = build_world_loot_randomization_plan(
            (*self.runtime.world_loot, *self.catalog.locations),
            "healing-all-shared-world-20261005",
            unsafe_packages=self.runtime.unsafe_packages,
            supported_quest_grant_destinations=self.catalog.destination_ids,
            catalog_candidates=candidates,
        )
        healing = [
            item for item in plan.assignments
            if item.destination.location_type.startswith("HEALING_UPGRADE_")
        ]
        self.assertEqual(len(healing), 116)
        self.assertTrue(all(item.assigned_rewards[0].soft_object_path_identity is not None for item in healing))
        self.assertTrue(all(
            item.assigned_rewards[0].article_path.casefold() != item.destination.rewards[0].article_path.casefold()
            for item in healing
        ))

    # Check field enemy zenon source is
    def test_field_enemy_zenon_source_is_counted_as_one_physical_gadget_occurrence(self) -> None:
        gadgets = DefensiveGadgetCatalogStore(_ROOT / "data" / "reward_pools" / "current").load("current")
        jails = JailCatalogStore(_ROOT / "data" / "jails" / "current").load()
        articles = CanonicalArticleRewardCatalogStore(_ROOT / "data" / "reward_pools" / "current").load("current")
        candidates = (
            *defensive_gadget_world_candidates(gadgets),
            *jail_world_candidates(jails),
            *canonical_article_world_candidates(articles, include_boosters=False),
        )
        plan = build_world_loot_randomization_plan(
            classified_world_fixture((*self.runtime.world_loot, *self.catalog.locations)),
            "field-enemy-gadget-dedup",
            unsafe_packages=self.runtime.unsafe_packages,
            catalog_candidates=candidates,
            supported_quest_grant_destinations=self.catalog.destination_ids,
        )
        zenon = "/Game/Blueprint/Article/Gadget/DLC/DA_DefGadget_ZenonGreatShield"
        assigned = Counter(
            reward.article_path.casefold()
            for assignment in plan.assignments
            if assignment.destination.mirror_of_package_path is None
            for reward in assignment.assigned_rewards
        )
        self.assertEqual(assigned[zenon.casefold()], 1)
        expected_gadget_occurrences = sum(
            1
            for location in classified_world_fixture((*self.runtime.world_loot, *self.catalog.locations))
            if location.mirror_of_package_path is None and not location.protected
            for reward in location.rewards
            if gadgets.resolve(reward.article_path) is not None
        )
        self.assertEqual(plan.candidate_stats("defensive_gadget").canonical_candidates, expected_gadget_occurrences)
        self.assertEqual(plan.candidate_stats("defensive_gadget").injected_count, 0)

    # Check manifest reports active field enemy
    def test_manifest_reports_active_field_enemy_slots(self) -> None:
        on_plan = build_world_loot_randomization_plan(
            classified_world_fixture((*self.runtime.world_loot, *self.catalog.locations)), "field-enemy-manifest-on",
            unsafe_packages=self.runtime.unsafe_packages,
            supported_quest_grant_destinations=self.catalog.destination_ids,
        )
        enabled = _field_enemy_manifest_counts(
            self.catalog, on_plan, options=GenerationOptions(randomize_world_loot=True),
        )
        self.assertEqual(enabled["catalog_eligible_locations"], 24)
        self.assertEqual(enabled["eligible_where_destinations"], 24)
        self.assertEqual(enabled["randomizable_destinations"], 24)
        self.assertEqual(enabled["protected_exclusions"], 10)
        self.assertEqual(enabled["disabled_due_to_missing_sources"], 0)
        self.assertEqual(enabled["protected_exclusions_extracted"], 0)

    # Check unknown vanilla what does not
    def test_unknown_vanilla_what_does_not_disable_field_enemy_where(self) -> None:
        self.assertTrue(all(
            slot.rewards[0].content_class == "UNKNOWN"
            for slot in self.catalog.locations
        ))
        canonical_candidates = tuple(
            WorldLootRewardCandidate(
                candidate_id=f"fixture-article-{index:02d}",
                rewards=(RewardPayload(
                    f"/Game/Blueprint/Article/Item/Support/FieldEnemyFixture_{index:02d}",
                    1,
                    True,
                    "BASE",
                ),),
                candidate_category="fixture",
                canonical_identity=f"/Game/Blueprint/Article/Item/Support/FieldEnemyFixture_{index:02d}",
            )
            for index in range(24)
        )
        plan = build_world_loot_randomization_plan(
            self.catalog.locations,
            "field-enemy-unknown-what",
            catalog_candidates=canonical_candidates,
            supported_quest_grant_destinations=self.catalog.destination_ids,
        )
        assigned_ids = {
            item.destination.destination_id for item in plan.assignments
            if item.destination.destination_id in self.catalog.destination_ids
        }
        self.assertEqual(assigned_ids, self.catalog.destination_ids)
        counts = _field_enemy_manifest_counts(
            self.catalog,
            plan,
            options=GenerationOptions(randomize_world_loot=True),
        )
        self.assertEqual(counts["where_content_classes"], {"DLC": 24})
        self.assertEqual(counts["what_content_classes"], {"UNKNOWN": 24})
        self.assertEqual(counts["unknown_what_locations"], 24)
        self.assertEqual(counts["eligible_where_destinations"], 24)
        self.assertEqual(counts["randomizable_destinations"], 24)
        changed_packages = {
            assignment.destination.package_path.casefold()
            for assignment in plan.assignments
            if assignment.destination.destination_id in self.catalog.destination_ids
            and assignment.assigned_rewards != assignment.destination.rewards
        }
        self.assertEqual(counts["required_quest_packages_for_patch"], len(changed_packages))


    # Check fingerprint bound quest patch check
    def test_fingerprint_bound_quest_patch_checks_both_original_sidecars(self) -> None:
        layout = self.catalog.locations[0].quest_article_layout
        assert layout is not None
        with tempfile.TemporaryDirectory() as temporary:
            primary = Path(temporary) / "Quest.uasset"
            sidecar = primary.with_suffix(".uexp")
            primary.write_bytes(b"researched header")
            sidecar.write_bytes(b"researched export data")
            bound = replace(
                layout,
                source_uasset_sha256=hashlib.sha256(primary.read_bytes()).hexdigest(),
                source_uexp_sha256=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            )
            _validate_quest_source_fingerprint(primary, bound, self.catalog.locations[0].package_path)
            sidecar.write_bytes(b"changed export data")
            with self.assertRaisesRegex(RawArticleGrantGenerationError, "fingerprint mismatch"):
                _validate_quest_source_fingerprint(primary, bound, self.catalog.locations[0].package_path)


if __name__ == "__main__":
    unittest.main()
