from __future__ import annotations

import csv
import hashlib
import json
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.core.seed import normalize_seed
from cv2randomizer.core.workspace import CacheManager, WorkspaceManager
from cv2randomizer.game.catalogs import CatalogStore, MaterialLocation, RewardPayload, WorldLootLocation
from cv2randomizer.game.asset_extraction import (
    ExtractedAsset,
    _normalize_asset_path,
    archive_source_fingerprint,
)
from cv2randomizer.game.game_data_cache import GameDataCacheState, GameDataCacheStore
from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.dialogue_skip_generation import DialogueSkipError, DialogueSkipResult
from cv2randomizer.game.blood_code_catalog import BloodCodeCatalogStore
from cv2randomizer.game.blood_code_generation import BloodCodeGenerationResult, BloodCodeGrantSnapshot
from cv2randomizer.game.bequeathed_formae_catalog import BequeathedFormaCatalogStore
from cv2randomizer.game.bequeathed_formae_generation import (
    BequeathedFormaGenerationResult,
)
from cv2randomizer.game.booster_catalog import BoosterCatalogStore
from cv2randomizer.game.booster_generation import BoosterGenerationResult
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.game.jail_catalog import JailCatalogStore
from cv2randomizer.game.defensive_gadget_generation import DefensiveGadgetGenerationResult
from cv2randomizer.game.field_enemy_catalog import FieldEnemyCatalogStore
from cv2randomizer.game.shop_tables import ShopTableProduct
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES
from cv2randomizer.game.material_generation import MaterialGenerationResult
from cv2randomizer.packaging.installer import InstallManager
from cv2randomizer.packaging.retoc import PackageBuildError, PackageResult
from cv2randomizer.game.versioning import VersionDetection
from cv2randomizer.game.world_loot_generation import WorldLootGenerationResult
from cv2randomizer.game.world_loot_serialization import WorldLootPackagePatchResult
from cv2randomizer.game.world_loot_generation import WorldLootGenerationResult
from cv2randomizer.game.raw_article_grant_generation import RawArticleGrantSnapshot
from cv2randomizer.randomizer.materials import MaterialAssignment, MaterialRandomizationPlan
from cv2randomizer.randomizer.blood_codes import build_blood_code_plan
from cv2randomizer.randomizer.bequeathed_formae import build_bequeathed_formae_plan
from cv2randomizer.randomizer.boosters import build_booster_plan
from cv2randomizer.randomizer.defensive_gadgets import build_defensive_gadget_npc_plan
from cv2randomizer.randomizer.initial_inventory import InitialInventoryPlan, build_initial_inventory_plan
from cv2randomizer.randomizer.shops import build_global_article_mix_plans, build_shop_randomization_plan
from cv2randomizer.randomizer.world_loot import (
    WorldLootAssignment,
    WorldLootRewardCandidate,
    WorldLootRandomizationPlan,
    build_world_loot_randomization_plan,
)
from cv2randomizer.generation.pipeline import (
    GenerationEngine,
    GenerationError,
    GenerationRequest,
    _copy_world_outputs,
    _generation_error_cause,
    _handle_initial_inventory_preparation_failure,
    _known_source_packages,
    _remap_world_result,
    _simple_spoiler_entries,
    _spoiler_metadata,
    _spoiler_settings,
    _world_package_overrides,
    _validate_world_loot_prepared_descriptors,
    _write_manifest,
    _write_spoiler,
)
from cv2randomizer.generation.spoiler import write_spoiler


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CORE_PACKAGE = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"


class _ClassifiedFixtureCatalogStore:
    """Test-only catalog overlay representing explicitly reviewed BASE fixtures."""

    # Initialize this object
    def __init__(self, catalogs_dir: Path) -> None:
        self.store = CatalogStore(catalogs_dir)

    # Load and validate catalog data
    def load(self, revision: str):
        catalogs = self.store.load(revision)
        return replace(
            catalogs,
            world_loot=tuple(
                replace(
                    location,
                    content_class=("BASE" if location.content_class == "UNKNOWN" else location.content_class),
                    rewards=tuple(
                        replace(reward, content_class=("BASE" if reward.content_class == "UNKNOWN" else reward.content_class))
                        for reward in location.rewards
                    ),
                )
                for location in catalogs.world_loot
            ),
            materials=tuple(
                replace(
                    location,
                    where_content_class=("BASE" if location.where_content_class == "UNKNOWN" else location.where_content_class),
                    resource_content_class=("BASE" if location.resource_content_class == "UNKNOWN" else location.resource_content_class),
                    reward_content_class=("BASE" if location.reward_content_class == "UNKNOWN" else location.reward_content_class),
                )
                for location in catalogs.materials
            ),
            shops=tuple(
                replace(product, what_content_class=("BASE" if product.what_content_class == "UNKNOWN" else product.what_content_class))
                for product in catalogs.shops
            ),
        )


class _Detector:
    # Initialize this object
    def __init__(self, supported: bool = True, local_unverified: bool = False) -> None:
        self.supported = supported
        self.local_unverified = local_unverified

    # Detect the installed game version
    def detect(self, game_root: Path) -> VersionDetection:
        return VersionDetection(
            self.supported,
            "fixture-build" if self.supported else ("local-unverified-fixture" if self.local_unverified else None),
            "current" if self.supported or self.local_unverified else None,
            game_root / "CodeVein2.exe" if self.supported or self.local_unverified else None,
            "fixture build" if self.supported else (
                "local validation only" if self.local_unverified else "unsupported fixture build"
            ),
            (_CORE_PACKAGE,) if self.supported else (),
            self.local_unverified,
        )


class _FakePackager:
    # Initialize this object
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.called = False

    # Resolve the package path
    def package(self, content_root: Path, *, work_root: Path, output_directory: Path,
                input_files: tuple[Path, ...] | None = None, game_root: Path | None = None) -> PackageResult:
        self.called = True
        if self.fail:
            raise PackageBuildError("simulated container verification failure")
        output_directory.mkdir(parents=True, exist_ok=True)
        files = tuple(output_directory / f"CV2Randomizer_P{suffix}" for suffix in (".pak", ".ucas", ".utoc"))
        for path in files:
            path.write_bytes(b"validated container")
        return PackageResult("CV2Randomizer_P", files, ("../../../CodeVein2/Content/Fixture.umap",))


class _ForbiddenInstalledExtractor:
    # Initialize this object
    def __init__(self) -> None:
        self.calls = 0

    # Inspect package inventory
    def inspect_package_inventory(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("cache-backed Generate must not inspect the installed IoStore inventory")

    # Extract requested game assets
    def extract_assets(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("cache-backed Generate must not extract from the installed game")


# Read fake world patch
def _fake_world_patch(plan: object, **kwargs: object) -> WorldLootGenerationResult:
    location = next(
        assignment.destination for assignment in plan.assignments
        if assignment.destination.destination_kind == "ARTICLE_BULK"
    )
    relative = Path(*location.package_path.split("/")[2:]).with_suffix(".umap")
    header = kwargs["output_content_root"] / relative
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_bytes(b"staged fixture map")
    header.with_suffix(".uexp").write_bytes(b"staged fixture exports")
    return WorldLootGenerationResult(1, 1, (), (header, header.with_suffix(".uexp")))


class _FakeRawArticleExtractor:
    # Initialize this object
    def __init__(
        self,
        cache_root: Path,
        *,
        container_names: tuple[str, ...] = (),
        package_paths: tuple[str, ...] | None = None,
        include_core: bool = True,
    ) -> None:
        self.cache_root = cache_root
        self.container_names = container_names
        packages = _fixture_source_packages() if package_paths is None else package_paths
        self.package_paths = tuple(dict.fromkeys(((_CORE_PACKAGE,) if include_core else ()) + tuple(packages)))
        self.inventory_calls = 0
        self.extraction_calls = 0

    # Inspect package inventory
    def inspect_package_inventory(self, _game_root, *, policy=None):
        self.inventory_calls += 1
        return PackageInventory.create(self.package_paths, self.container_names)

    # Extract requested game assets
    def extract_assets(self, _game_root, package_paths, **_kwargs):
        self.extraction_calls += 1
        results = []
        for package_path in package_paths:
            relative = Path(*package_path.split("/")[2:])
            header = self.cache_root / relative.with_suffix(".uasset")
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_bytes(b"fixture source uasset")
            export = header.with_suffix(".uexp")
            export.write_bytes(b"fixture source exports")
            results.append(ExtractedAsset(package_path, (header, export)))
        return tuple(results)


_FIXTURE_PACKAGE_CACHE: tuple[str, ...] | None = None


# Build fixture source packages
def _fixture_source_packages() -> tuple[str, ...]:
    """Model every checked-in fixture package as present in the fake install."""
    global _FIXTURE_PACKAGE_CACHE
    if _FIXTURE_PACKAGE_CACHE is not None:
        return _FIXTURE_PACKAGE_CACHE
    directories = (
        _REPOSITORY_ROOT / "data" / "catalogs" / "current",
        _REPOSITORY_ROOT / "data" / "content_availability",
        _REPOSITORY_ROOT / "data" / "enemy_drops",
        _REPOSITORY_ROOT / "data" / "blood_codes" / "current",
        _REPOSITORY_ROOT / "data" / "boosters" / "current",
        _REPOSITORY_ROOT / "data" / "extractor" / "current",
        _REPOSITORY_ROOT / "data" / "defensive_gadgets" / "current",
        _REPOSITORY_ROOT / "data" / "jails" / "current",
        _REPOSITORY_ROOT / "data" / "field_enemy" / "current",
        _REPOSITORY_ROOT / "data" / "reward_pools" / "current",
        _REPOSITORY_ROOT / "data" / "bFormae" / "current",
    )
    packages: set[str] = set()

    # Visit the next classification node
    def visit(value: object) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif isinstance(value, str) and value.startswith("/Game/"):
            package = value.split("::", 1)[0].split(".", 1)[0].strip()
            if package.count("/") >= 3:
                packages.add(package)

    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            try:
                visit(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
    packages.update(
        f"/Game/Blueprint/GameSystem/Shop/{table}"
        for table in SHOP_TABLE_NAMES.values()
    )
    # These optional preorder Article packages are absent from the default
    # generation fixture; their dedicated behavior has focused unit coverage.
    packages.difference_update({
        "/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_JutoMasamune_DLC",
        "/Game/Blueprint/Article/Weapon/DualSword/DA_DualSword_Antique_DLC",
    })
    packages.add(_CORE_PACKAGE)
    _FIXTURE_PACKAGE_CACHE = tuple(sorted(packages, key=str.casefold))
    return _FIXTURE_PACKAGE_CACHE


class _FakeRawArticleGrantSerializer:
    # Inspect prepared game data
    def inspect(self, *, slot, input_file, **_kwargs):
        layout = slot.raw_article_layout
        return RawArticleGrantSnapshot(
            slot.package_path, input_file, slot.export_index, layout.export_name, layout.serial_size,
            layout.expected_path_name_index, layout.expected_vanilla_number,
            layout.expected_object_name_index, layout.expected_vanilla_number,
            layout.expected_path_name_map_value, layout.expected_object_name_map_value,
            layout.expected_vanilla_article_path,
        )

    # Patch the prepared asset package
    def patch(self, *, slot, input_file, output_file, assigned_article_path, **_kwargs):
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(b"fixture patched direct reward uasset")
        export = output_file.with_suffix(".uexp")
        export.write_bytes(b"fixture patched direct reward exports")
        layout = slot.raw_article_layout
        snapshot = RawArticleGrantSnapshot(
            slot.package_path, output_file, slot.export_index, layout.export_name, layout.serial_size,
            layout.expected_path_name_index, 0, layout.expected_object_name_index, 0,
            assigned_article_path, assigned_article_path.rsplit("/", 1)[-1], assigned_article_path,
        )
        return snapshot, (output_file, export)


class _FakeWorldQuestArticleSerializer:
    # Initialize this object
    def __init__(self) -> None:
        self.assigned_by_package: dict[str, str] = {}
        catalog = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        field_enemy = FieldEnemyCatalogStore(_REPOSITORY_ROOT / "data" / "field_enemy").load(
            "current", important_item_exclusions=catalog.important_item_exclusions,
        )
        self.slots_by_package: dict[str, tuple[object, ...]] = {}
        for slot in (*catalog.world_loot, *field_enemy.locations):
            if slot.destination_kind == "QUEST_ARTICLE_GRANT":
                self.slots_by_package[slot.package_path] = (
                    *self.slots_by_package.get(slot.package_path, ()), slot,
                )
        formae = BequeathedFormaCatalogStore(
            _REPOSITORY_ROOT / "data" / "bFormae" / "current",
        ).load()
        self.formae_grants_by_package: dict[str, tuple[tuple[object, object], ...]] = {}
        for item in formae.direct_rewards:
            for grant in item.direct_grants:
                self.formae_grants_by_package[grant.quest_asset.casefold()] = (
                    *self.formae_grants_by_package.get(grant.quest_asset.casefold(), ()),
                    (item, grant),
                )
        self.assigned_by_target: dict[tuple[str, int, int, int], str] = {}

    # Inspect bequeathed formae packages
    def inspect_bequeathed_formae_packages(self, *, package_files, **_kwargs):
        snapshots = []
        for virtual_package in package_files:
            for item, grant in self.formae_grants_by_package[virtual_package.casefold()]:
                snapshots.append(BloodCodeGrantSnapshot(
                    virtual_package=virtual_package,
                    export_index=grant.export_index,
                    export_name="QSTask_GainItem_Fixture",
                    bulk_index=0,
                    reward_index=0,
                    reward_package=item.asset_path,
                    reward_asset=item.asset_path.rsplit("/", 1)[-1],
                    count=grant.count,
                    notification=grant.notification,
                ))
        return tuple(snapshots)

    # Inspect world loot quest packages
    def inspect_world_loot_quest_packages(self, *, package_files, **_kwargs):
        snapshots = []
        for virtual_package, input_file in package_files.items():
            for slot in self.slots_by_package[virtual_package]:
                for layout in slot.quest_grant_layouts:
                    targets = (
                        (slot.export_index, layout.export_name),
                        *((mirror.export_index, mirror.export_name) for mirror in layout.mirrored_exports),
                    )
                    for export_index, export_name in targets:
                        key = (virtual_package.casefold(), export_index, layout.bulk_index, layout.reward_index)
                        assigned = self.assigned_by_target.get(key)
                        reward = assigned or layout.expected_article_path
                        if assigned is None:
                            package_name = layout.expected_package_name_value
                            package_number = layout.expected_package_name_number
                            asset_name = layout.expected_asset_name_value
                            asset_number = layout.expected_asset_name_number
                        else:
                            package_name = reward
                            package_number = 0
                            asset_name = reward.rsplit("/", 1)[-1]
                            asset_number = 0
                        snapshots.append(BloodCodeGrantSnapshot(
                            virtual_package=virtual_package,
                            export_index=export_index,
                            export_name=export_name,
                            bulk_index=layout.bulk_index,
                            reward_index=layout.reward_index,
                            reward_package=reward,
                            reward_asset=reward.rsplit("/", 1)[-1],
                            count=layout.count,
                            notification=layout.notification,
                            package_name_value=package_name,
                            package_name_number=package_number,
                            asset_name_value=asset_name,
                            asset_name_number=asset_number,
                            sub_path_string=layout.expected_sub_path_string,
                            articles_count=layout.expected_articles_count,
                        ))
        return tuple(snapshots)

    # Patch world loot quest packages
    def patch_world_loot_quest_packages(self, *, patch_packages, **_kwargs):
        results = []
        for virtual, (source, output, assignments) in patch_packages.items():
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(source.read_bytes() + b" patched")
            output.with_suffix(".uexp").write_bytes(source.with_suffix(".uexp").read_bytes() + b" patched")
            for assignment in assignments:
                self.assigned_by_target[(
                    virtual.casefold(), assignment["exportIndex"], assignment["bulkIndex"], assignment["rewardIndex"],
                )] = assignment["assignedPackage"]
            results.append({
                "virtualPackage": virtual,
                "inputFile": str(source),
                "outputFile": str(output),
                "patchedSlots": len(assignments),
                "outputFiles": [str(output), str(output.with_suffix(".uexp"))],
            })
        return results


class _FailingInstaller:
    # Initialize this object
    def __init__(self) -> None:
        self.called = False

    # Install the generated mod
    def install(
        self,
        package: PackageResult,
        *,
        game_root: Path,
        spoiler_path: Path | None = None,
        allow_unowned_replacement: bool = False,
    ) -> None:
        self.called = True
        raise RuntimeError("simulated install failure")


# Find spoiler product
def _spoiler_product(row: str, index: int, item: str, *, price: int, stock: int) -> ShopTableProduct:
    return ShopTableProduct(
        family="NORMAL",
        shop_row=row,
        product_index=index,
        article_path=f"/Game/Blueprint/Article/Item/{item}",
        item_object=item,
        article_path_offset=0,
        article_path_bytes=bytes([index + 1]) * 20,
        price=price,
        price_offset=20,
        price_bytes=struct.pack("<i", price),
        uses_stock=True,
        uses_stock_offset=24,
        uses_stock_bytes=b"\x01",
        stock=stock,
        stock_offset=25,
        stock_bytes=struct.pack("<i", stock),
        important=False,
        evaluation_formula="",
        evaluation_raw=b"",
        raw_bytes=b"",
    )


class GenerationPipelineTests(unittest.TestCase):
    # Preserve cancellation through optional wrappers
    def test_wrapped_generation_cancellation_is_not_treated_as_optional_failure(self) -> None:
        cancelled = GenerationError("Generation was cancelled by the user.")
        wrapped = RuntimeError("Optional dialogue patch failed.")
        wrapped.__cause__ = cancelled

        self.assertIs(_generation_error_cause(wrapped), cancelled)

    # Validate structural pickup profiles
    def test_extract_profiles_match_structural_class_package(self) -> None:
        from cv2randomizer.game.material_dependencies import MaterialClassProfile
        from cv2randomizer.game.renewable_ingredients import renewable_material_locations

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = self._engine(root / "data")
            catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
            location = next(
                item for item in catalogs.world_loot
                if item.location_type == "RENEWABLE_INGREDIENT" and item.structural_class_package
            )
            class_package = location.structural_class_package
            assert class_package is not None
            expected_class_name = f"{class_package.rsplit('/', 1)[-1]}_C"
            index = {
                "schema_version": 1,
                "game_version": "fixture-build",
                "catalog_revision": "current",
                "maps": {
                    location.package_path: {
                        str(location.export_index): {
                            "export_name": location.export_name,
                            "class_name": expected_class_name,
                            "class_package": class_package,
                        },
                    },
                },
                "blueprint_packages": [],
                "blueprint_cdo_defaults": {},
                "incompatible_world_loot_locations": [],
            }

            class WorldLootResolver:
                # Return prepared fixture index
                def stage_for_locations(self, **kwargs):
                    self.stage_kwargs = kwargs
                    return SimpleNamespace(dependency_index=index)

            class MaterialResolver:
                # Return validated fixture profile
                def validate_classes(self, *, locations, **_kwargs):
                    self.locations = tuple(locations)
                    return SimpleNamespace(profiles=(MaterialClassProfile(
                        package_path=class_package,
                        class_name=expected_class_name,
                        direct_parent_package=None,
                        material_hierarchy=True,
                        effective_reset_on_yadorigi_rest=True,
                        eligible=True,
                        reason=None,
                    ),))

            engine.world_loot_resolver = WorldLootResolver()
            engine.material_resolver = MaterialResolver()
            diagnostics = SimpleNamespace(
                set_job=lambda *_args, **_kwargs: None,
                record_operation=lambda *_args, **_kwargs: None,
                observe_helper=lambda *_args, **_kwargs: None,
            )
            result = engine._prefetch_cached_dependencies(
                catalogs=replace(catalogs, world_loot=(location,), unsafe_packages=frozenset()),
                installed_inventory=PackageInventory.create((
                    location.package_path,
                    class_package,
                )),
                game_root=root,
                mappings_path=root / "CV2.usmap",
                work_root=root / "work",
                game_version="fixture-build",
                data_revision="current",
                compatibility_profile=None,
                display_version="2.0.3.0",
                extractor=engine.extractor,
                material_locations=renewable_material_locations((location,)),
                progress=None,
                memory_scheduler=SimpleNamespace(),
                memory_diagnostics=diagnostics,
            )

        self.assertEqual(result["incompatible_world_loot_locations"], [])
        self.assertEqual(engine.world_loot_resolver.stage_kwargs["display_version"], "2.0.3.0")
        self.assertEqual(
            result["renewable_class_profiles"][class_package.casefold()]["class_name"],
            expected_class_name,
        )

    # Check extract source registry caches every
    def test_extract_source_registry_caches_every_renewable_blueprint_class(self) -> None:
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        sources = _known_source_packages(
            catalogs,
            enemy_drop_catalog=None,
            blood_code_catalog=None,
            booster_catalog=None,
            extractor_catalog=None,
            defensive_gadget_catalog=None,
            jail_catalog=None,
            field_enemy_catalog=None,
            canonical_article_catalog=None,
            bequeathed_formae_catalog=None,
        )
        expected = {
            location.structural_class_package
            for location in catalogs.world_loot
            if location.location_type == "RENEWABLE_INGREDIENT"
        }

        self.assertEqual(len(expected), 49)
        self.assertTrue(expected.issubset(set(sources["materials"])))

    # Check extract source registry include direct
    def test_extract_source_registry_includes_direct_quest_reward_packages(self) -> None:
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        sources = _known_source_packages(
            catalogs,
            enemy_drop_catalog=None,
            blood_code_catalog=None,
            booster_catalog=None,
            extractor_catalog=None,
            defensive_gadget_catalog=None,
            jail_catalog=None,
            field_enemy_catalog=None,
            canonical_article_catalog=None,
            bequeathed_formae_catalog=None,
        )
        direct_grant_packages = {
            location.package_path
            for location in catalogs.world_loot
            if location.destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}
        }
        hot_spring_packages = {
            location.package_path
            for location in catalogs.world_loot
            if (location.destination_id or "").startswith("hot_spring_")
        }

        self.assertEqual(len(hot_spring_packages), 10)
        self.assertTrue(direct_grant_packages.issubset(set(sources["world_loot"])))
        self.assertTrue(hot_spring_packages.issubset(set(sources["world_loot"])))

    # Prepare the test fixture
    def setUp(self) -> None:
        # These pipeline fixtures do not provide a parseable cooked Shop table
        # for the Josee alternate-route mirror. The linked-route behavior has
        # dedicated tests in test_linked_jail_rewards.py.
        route_patch = patch(
            "cv2randomizer.generation.pipeline.find_stinger_shop_product",
            return_value=None,
        )
        route_patch.start()
        self.addCleanup(route_patch.stop)
    # Read engine
    def _engine(
        self, root: Path, *, supported: bool = True, local_unverified: bool = False,
    ) -> GenerationEngine:
        return GenerationEngine(
            version_detector=_Detector(supported, local_unverified),
            catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
            workspace_manager=WorkspaceManager(root),
            extractor=_FakeRawArticleExtractor(root / "fake-extractor-cache"),
            raw_article_grant_serializer=_FakeRawArticleGrantSerializer(),
            world_quest_article_serializer=_FakeWorldQuestArticleSerializer(),
            bequeathed_formae_serializer=_FakeWorldQuestArticleSerializer(),
        )

    # Check spoiler metadata reports new unlock
    def test_spoiler_metadata_reports_new_unlock_options(self) -> None:
        options = GenerationOptions(
            unlock_npc_trader_shops=True,
            unlock_all_booster_slots=False,
            motorcycle_unlocked=True,
            second_weapon_unlocked=True,
            disable_tutorials=True,
            tap_to_skip_dialogue=True,
            cheaper_booster_extractor=True,
            low_blood_code_mastery_exp=True,
        )
        metadata = _spoiler_metadata(VersionDetection(
            supported=False,
            version_id="local-unverified-abc123",
            catalog_revision="current",
            executable_path=Path("CodeVein2.exe"),
            reason="local only",
            local_unverified=True,
            display_version="1.0.2.0",
        ))
        settings = _spoiler_settings(options)

        self.assertEqual(metadata, {
            "App Version": "v1.0.0",
            "Game Version": "1.0.2.0",
            "Version Validation": "Local, unverified",
        })
        self.assertEqual(settings[0], "Enabled World Loot (always on)")
        self.assertIn("Enabled Unlock all NPC Trader Shop Tiers", settings)
        self.assertIn("Disabled Unlock all 6 Booster Slots", settings)
        self.assertIn("Enabled Unlock Motorcycle at Start", settings)
        self.assertIn("Enabled Unlock Second Weapon Slot", settings)
        self.assertIn("Enabled Disable Tutorial Pop-ups", settings)
        self.assertIn("Enabled Tap Instead of Holding to Skip Dialogue", settings)
        skipped_settings = _spoiler_settings(
            options,
            DialogueSkipResult((), False, "unsupported layout"),
        )
        self.assertIn("Disabled Tap Instead of Holding to Skip Dialogue", skipped_settings)
        self.assertIn("Enabled Cheaper Booster Extractor", settings)
        self.assertIn("Enabled Unrestricted Blood Code Mastery", settings)
        self.assertIn("Disabled Booster Extractor", settings)
        self.assertIn("Disabled Mix Boosters with World Loot", settings)
        self.assertEqual(len(settings), 18)
        self.assertFalse(any("Pipe Shop" in line or "Pre-Order" in line for line in settings))

    # Check incompatible initial inventory preparation is skipped
    def test_initial_inventory_preparation_failure_is_recorded_as_a_skip(self) -> None:
        warnings: list[str] = []
        _handle_initial_inventory_preparation_failure(
            RuntimeError("source occurrence transfer failed"),
            availability_warnings=warnings,
        )
        self.assertEqual(warnings, [
            "Initial Inventory skipped: source occurrence transfer failed: source occurrence transfer failed",
        ])

    # Check world loot spoiler collects canonical
    def test_world_loot_spoiler_collects_canonical_destinations_including_self_maps(self) -> None:
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        destination = next(
            item for item in catalogs.world_loot
            if item.location_type == "NORMAL_WORLD_LOOT"
        )
        plan = SimpleNamespace(assignments=(WorldLootAssignment(destination, destination),))
        empty = SimpleNamespace(assignments=())

        entries = _simple_spoiler_entries(
            plan, empty, empty, None, None, None, None, None, None, None,
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].system, "World Loot")
        self.assertEqual(entries[0].vanilla, entries[0].randomized)

    # Check world loot spoiler collapses mirrored
    def test_world_loot_spoiler_collapses_mirrored_destinations(self) -> None:
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        canonical = next(
            item for item in catalogs.world_loot
            if item.location_type == "NORMAL_WORLD_LOOT" and item.mirror_of_package_path is None
        )
        mirror = replace(
            canonical,
            package_path=canonical.package_path + "_DlcMirror",
            export_index=canonical.export_index + 100000,
            mirror_of_package_path=canonical.package_path,
            mirror_of_export_index=canonical.export_index,
        )
        plan = SimpleNamespace(assignments=(
            WorldLootAssignment(canonical, canonical),
            WorldLootAssignment(mirror, canonical),
        ))
        empty = SimpleNamespace(assignments=())

        entries = _simple_spoiler_entries(
            plan, empty, empty, None, None, None, None, None, None, None,
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].destination.split(" (")[0], canonical.display_name or canonical.export_name)

    # Check world loot spoiler groups cross
    def test_world_loot_spoiler_groups_cross_pool_items_by_reward_category(self) -> None:
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        destinations = iter(
            item for item in catalogs.world_loot
            if item.location_type == "NORMAL_WORLD_LOOT"
            and item.mirror_of_package_path is None
            and len(item.rewards) == 1
        )
        category_items = (
            ("booster", "DA_Booster_MND_1", "Boosters"),
            ("defensive_gadget", "DA_DefGadget_Warp_2", "Defensive Gadgets"),
            ("jail", "DA_Jail_Hounds", "Jails"),
        )
        assignments = []
        for category, article, _section in category_items:
            destination = next(destinations)
            candidate_category = None if category == "booster" else category
            candidate = WorldLootRewardCandidate(
                candidate_id=f"spoiler-test:{category}",
                rewards=(RewardPayload(f"/Game/Blueprint/Article/Test/{article}", 1, True),),
                candidate_category=candidate_category,
                canonical_identity=article if candidate_category is not None else None,
            )
            assignments.append(WorldLootAssignment(destination, None, candidate))
        world_plan = SimpleNamespace(assignments=tuple(assignments))
        empty = SimpleNamespace(assignments=())

        entries = _simple_spoiler_entries(
            world_plan, empty, empty, None, None, None, None, None, None, None,
        )

        self.assertEqual(
            [entry.system for entry in entries],
            [section for _category, _article, section in category_items],
        )

    # Check renewable ingredient spoiler has own
    def test_renewable_ingredient_spoiler_has_its_own_category(self) -> None:
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        destination = next(
            item for item in catalogs.materials
            if item.reward.article_path.endswith("DA_Item_IngredientMushroom_1")
            and item.mirror_of_package_path is None
        )
        source = next(
            item for item in catalogs.materials
            if item.reward.article_path.endswith("DA_Item_IngredientRice_1")
            and item.mirror_of_package_path is None
        )
        material_plan = MaterialRandomizationPlan((MaterialAssignment(destination, source),), (), ())
        empty = SimpleNamespace(assignments=())

        entries = _simple_spoiler_entries(
            empty, empty, material_plan, None, None, None, None, None, None, None,
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].system, "Ingredients")
        with tempfile.TemporaryDirectory() as temporary:
            spoiler_path = Path(temporary) / "Spoiler.txt"
            write_spoiler(spoiler_path, entries, seed="ingredients-section")
            content = spoiler_path.read_text(encoding="utf-8")
        self.assertIn("=== Ingredients ===", content)
        self.assertIn("All 1 ground pickups: Ingredient Mushroom 1 -> Ingredient Rice 1", content)

    # Check blood code spoiler use friendly
    def test_blood_code_spoiler_uses_friendly_quest_location(self) -> None:
        destination = SimpleNamespace(
            display_name="",
            location_kind="gain_item",
            virtual_package="/Game/Quest/DLC/QST_MainEpisode_DLC_e90070",
            export_index=76,
            export_name="QSTask_GainItem_0",
            bulk_index=0,
            reward_index=0,
            reference_property="ArticleBulkInfo.Articles",
            original_blood_code_package="/Game/Blueprint/Article/BloodCode/DLC/DA_BloodCode_GQImogen",
        )
        source = SimpleNamespace(
            original_blood_code_package="/Game/Blueprint/Article/BloodCode/DLC/DA_BloodCode_Jadwiga_02",
        )
        blood_plan = SimpleNamespace(assignments=(SimpleNamespace(destination=destination, source=source),))
        empty = SimpleNamespace(assignments=())

        entries = _simple_spoiler_entries(
            empty, empty, empty, None, blood_plan, None, None, None, None, None,
        )

        self.assertEqual(entries[0].destination, "Quest Main Episode DLC, reward #76")
        self.assertNotIn("/Game/", entries[0].destination)
        self.assertNotIn("ArticleBulkInfo", entries[0].destination)
        with tempfile.TemporaryDirectory() as temporary:
            spoiler_path = Path(temporary) / "Spoiler.txt"
            write_spoiler(spoiler_path, entries, seed="friendly-blood-code-location")
            text = spoiler_path.read_text(encoding="utf-8")
        self.assertIn(
            "Quest Main Episode DLC, reward #76: Blood Code GQ Imogen -> Blood Code Jadwiga 02",
            text,
        )
        self.assertNotIn("/Game/Quest/", text)
        self.assertNotIn("ArticleBulkInfo", text)

    # Check multiple generations use cache without
    def test_multiple_generations_use_cache_without_calling_installed_extractor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = root / "game"
            pak_directory = game_root / "CodeVein2" / "Content" / "Paks"
            pak_directory.mkdir(parents=True)
            (pak_directory / "chunk.utoc").write_bytes(b"toc fixture")
            (pak_directory / "chunk.ucas").write_bytes(b"ucas fixture")
            executable = game_root / "CodeVein2.exe"
            executable.write_bytes(b"exe fixture")
            cache_store = GameDataCacheStore(CacheManager(root / "app-cache"))
            detection = VersionDetection(
                True, "fixture-build", "current", executable, "fixture build", (_CORE_PACKAGE,),
            )
            entry = cache_store.cache_entry_path(game_root, detection, create=True)
            _normalized, relative = _normalize_asset_path(_CORE_PACKAGE)
            package_root = entry / "assets" / Path(*relative.parts)
            package_root.mkdir(parents=True)
            payloads = (("DT_ShopLineup.uasset", b"cached header"), ("DT_ShopLineup.uexp", b"cached export"))
            for name, payload in payloads:
                (package_root / name).write_bytes(payload)
            (package_root / ".cv2randomizer-asset.json").write_text(
                json.dumps({
                    "virtual_path": _CORE_PACKAGE,
                    "files": [
                        {"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
                        for name, payload in payloads
                    ],
                }),
                encoding="utf-8",
            )
            cache_store.write_metadata(entry, cache_store.build_metadata(
                game_root=game_root,
                detection=detection,
                source_registry={"required_core": (_CORE_PACKAGE,)},
                required_sources=(_CORE_PACKAGE,),
            ))
            forbidden_extractor = _ForbiddenInstalledExtractor()
            workspace_manager = WorkspaceManager(root / "output")
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=workspace_manager,
                extractor=forbidden_extractor,
                game_data_cache_store=cache_store,
                packager=_FakePackager(),
                installer=InstallManager(root / "app-state"),
            )
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mappings")
            options = GenerationOptions(
                randomize_world_loot=True,
                randomize_materials=False,
                randomize_shops=False,
                randomize_npc_traders=False,
                randomize_enemy_drops=False,
                randomize_blood_codes=False,
                randomize_boosters=False,
                randomize_extractor_recipes=False,
                randomize_initial_inventory=False,
                randomize_defensive_gadget_npc_rewards=False,
                randomize_bequeathed_formae_quest_rewards=False,
                shops_normal=False,
                shops_luxury=False,
                shops_trade=False,
                install_after_generation=True,
            )
            before = {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads}
            stale = workspace_manager.create_generation(normalize_seed("previous-seed"))
            stale_payload = stale.staging / "Content" / "old-stage.bin"
            stale_payload.parent.mkdir(parents=True)
            stale_payload.write_bytes(b"old generation staging" * 256)
            first = engine.generate(GenerationRequest(
                game_root, mappings, "cache-seed-a", options,
                game_data_cache_entry=entry,
            ))

            self.assertEqual(forbidden_extractor.calls, 0)
            self.assertFalse(options.randomize_materials)
            self.assertIsNotNone(first.cleanup_report)
            self.assertGreater(first.cleanup_report.temporary_bytes_before, 0)
            self.assertGreater(first.cleanup_report.temporary_bytes_after, 0)
            self.assertGreater(len(first.cleanup_report.deleted_directories), 2)
            self.assertTrue(first.workspace.root.is_dir())
            self.assertTrue(first.workspace.logs.is_dir())
            self.assertFalse(first.workspace.staging.exists())
            self.assertTrue(stale.root.is_dir())
            self.assertTrue(stale.logs.is_dir())
            self.assertIsNone(first.spoiler_text_path)
            self.assertFalse((workspace_manager.root / "Spoiler.txt").exists())
            self.assertEqual(
                {name: hashlib.sha256((package_root / name).read_bytes()).hexdigest() for name, _ in payloads},
                before,
            )
            self.assertTrue(cache_store.inspect(game_root, detection).ready)
            retained_workspaces = list(workspace_manager.root.joinpath("generated").iterdir())
            self.assertEqual({path.name for path in retained_workspaces}, {first.workspace.root.name, stale.root.name})
            self.assertTrue(all((path / "logs").is_dir() for path in retained_workspaces))

            # A second normal Generate runs after the first cleanup using the
            # same READY vanilla cache and never invokes installed-game extraction.
            second = engine.generate(GenerationRequest(
                game_root, mappings, "cache-seed-b", options,
                game_data_cache_entry=entry,
            ))
            self.assertEqual(forbidden_extractor.calls, 0)
            self.assertIsNotNone(second.cleanup_report)
            self.assertTrue(second.workspace.root.is_dir())
            self.assertTrue(second.workspace.logs.is_dir())
            self.assertTrue(cache_store.inspect(game_root, detection).ready)
            self.assertIsNone(second.spoiler_text_path)
            self.assertFalse((workspace_manager.root / "Spoiler.txt").exists())

            cache_hash_before_failure = {
                name: hashlib.sha256((package_root / name).read_bytes()).hexdigest()
                for name, _ in payloads
            }
            with patch(
                "cv2randomizer.generation.pipeline.write_spoiler",
                side_effect=OSError("simulated spoiler write failure"),
            ):
                with self.assertRaisesRegex(GenerationError, "simulated spoiler write failure"):
                    engine.generate(GenerationRequest(
                        game_root, mappings, "cache-failure-seed", options,
                        game_data_cache_entry=entry,
                    ))
            self.assertTrue(cache_store.inspect(game_root, detection).ready)
            self.assertEqual(
                {
                    name: hashlib.sha256((package_root / name).read_bytes()).hexdigest()
                    for name, _ in payloads
                },
                cache_hash_before_failure,
            )
            failure_log = next(
                path / "logs" / "generation_failure.txt"
                for path in workspace_manager.root.joinpath("generated").iterdir()
                if (path / "logs" / "generation_failure.txt").is_file()
            )
            self.assertIn(
                "simulated spoiler write failure",
                failure_log.read_text(encoding="utf-8"),
            )

    # Check dry run writes deterministic assignments
    def test_dry_run_writes_deterministic_assignments_and_manifest_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = self._engine(root / "data")
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            options = GenerationOptions(
                randomize_materials=False,
                randomize_shops=False,
                install_after_generation=True,
                generate_spoiler_log=True,
            )
            request = GenerationRequest(Path("fixture-game"), mappings, "phase12-seed", options, dry_run=True)

            first = engine.generate(request)
            second = engine.generate(request)

            self.assertEqual(first.seed, second.seed)
            self.assertEqual(first.world_plan, second.world_plan)
            self.assertEqual(first.output_files, ())
            self.assertFalse(first.output_content_root.exists())
            document = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(document["status"], "assignments_only")
            self.assertTrue(document["dry_run"])
            self.assertEqual(document["seed"]["display"], "phase12-seed")
            self.assertEqual(document["app_version"], "1.0.0")
            self.assertTrue(document["generated_at"].endswith("Z"))
            self.assertEqual(document["errors"], 0)
            self.assertIn("world_loot", document["skipped"])
            self.assertEqual(document["spoiler_file"], "spoiler_log.csv")
            self.assertEqual(document["spoiler_txt_file"], "Spoiler.txt")
            self.assertEqual(document["installation"]["requested"], True)
            self.assertEqual(document["installation"]["status"], "not_performed_dry_run")
            self.assertEqual(document["output_filenames"], [])
            self.assertTrue(first.spoiler_path.is_file())
            self.assertTrue(first.workspace.spoiler_text.is_file())
            self.assertEqual(first.spoiler_text_path, first.workspace.spoiler_text)
            self.assertEqual(
                first.assignment_manifest_sha256,
                second.assignment_manifest_sha256,
            )
            with first.spoiler_path.open(encoding="utf-8-sig", newline="") as handle:
                spoiler_rows = list(csv.DictReader(handle))
            world_rows = {
                row["destination"]: (row["original"], row["assigned"])
                for row in spoiler_rows if row["feature"] == "WORLD_LOOT"
            }
            expected_rows = {
                (
                    item.destination.display_name
                    if item.destination.destination_id is not None
                    else f"{item.destination.package_path}#{item.destination.export_index}"
                ): (
                    ";".join(reward.article_path for reward in item.destination.rewards),
                    ";".join(reward.article_path for reward in item.assigned_rewards),
                ) for item in first.world_plan.assignments
            }
            self.assertEqual(world_rows, expected_rows)
            gadget_counts = document["counts"]["defensive_gadgets"]
            self.assertEqual(gadget_counts["catalog_count"], 17)
            self.assertEqual(gadget_counts["world_loot"]["canonical_catalog_candidates"], 9)
            self.assertTrue(any(
                "Jadwiga Servant - Lightning Barrage Reward" in warning
                for warning in document["content_availability"]["skipped_feature_groups"]
            ))
            self.assertEqual(gadget_counts["world_loot"]["injected_canonical_candidates"], 0)
            jail_counts = document["jails"]
            self.assertEqual(jail_counts["catalog_count"], 12)
            self.assertEqual(jail_counts["base_count"], 6)
            self.assertEqual(jail_counts["dlc_count"], 6)
            self.assertEqual(jail_counts["world_loot"]["already_present_in_movable_world_pool"], 10)
            self.assertEqual(
                jail_counts["world_loot"]["already_present_movable_candidates"],
                [
                    "DA_Jail_Bat", "DA_Jail_Hounds", "DA_Jail_Ivy", "DA_Jail_Reaper",
                    "DA_Jail_Bat_Pathos", "DA_Jail_Hounds_Pathos", "DA_Jail_Ivy_Pathos",
                    "DA_Jail_Ogre_Pathos", "DA_Jail_Reaper_Pathos", "DA_Jail_Stinger_Pathos",
                ],
            )
            self.assertEqual(
                jail_counts["world_loot"]["externally_injected_candidates"],
                [],
            )
            self.assertEqual(
                jail_counts["world_loot"]["protected_vanilla_sources"],
                [],
            )
            self.assertEqual(jail_counts["world_loot"]["shared_overlay_displaced_payloads"], 0)
            self.assertFalse(jail_counts["starting_jail_randomization"]["enabled"])
            self.assertFalse(jail_counts["game_assets_packaged"])
            self.assertEqual(document["counts"]["world_loot"]["canonical_reward_candidates"], 19)
            self.assertEqual(document["counts"]["world_loot"]["booster_candidates"], 0)
            self.assertEqual(document["counts"]["world_loot"]["booster_candidates_injected"], 0)
            self.assertEqual(document["counts"]["world_loot"]["booster_duplicate_sources_removed"], 0)
            self.assertEqual(document["counts"]["world_loot"]["canonical_overlay_displaced_payloads"], 0)
            self.assertIsNone(document["counts"]["world_loot"]["defensive_gadget_displaced_payloads"])
            self.assertIsNone(gadget_counts["world_loot"]["displaced_payloads"])
            self.assertEqual(
                gadget_counts["world_loot"]["shared_candidate_overlay_displaced_payloads"], 0,
            )
            jail_spoiler_rows = [
                row for row in spoiler_rows
                if row["feature"] == "WORLD_LOOT" and "category=jail" in row["notes"]
            ]
            # The spoiler contains one row per selected Jail source. Adding
            # destinations can rebalance which arity-compatible sources land
            # in this category.
            jail_source_assignments = [
                assignment for assignment in first.world_plan.assignments
                if assignment.source_candidate is not None
                and (
                    assignment.source_candidate.candidate_category == "jail"
                    or "jail" in (assignment.source_candidate.reward_categories or ())
                )
            ]
            self.assertEqual(len(jail_spoiler_rows), len(jail_source_assignments))

    # Check disabled enemy drops do not
    def test_disabled_enemy_drops_do_not_load_or_patch_enemy_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = self._engine(root / "data")
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_bequeathed_formae_quest_rewards=False,
                randomize_materials=False,
                randomize_shops=False,
                randomize_enemy_drops=False,
                install_after_generation=False,
            )
            result = engine.generate(GenerationRequest(Path("fixture-game"), None, "no-enemy-drops", options))
            document = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertIsNone(result.enemy_drop_plan)
            self.assertIsNone(result.enemy_drop_result)
            self.assertFalse(document["counts"]["enemy_drops"]["enabled"])
            self.assertEqual(result.output_files, ())

    # Check world ownership is planned before
    def test_world_ownership_is_planned_before_extractor_and_initial_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=_FakeRawArticleExtractor(root / "fake-extractor-cache"),
                world_quest_article_serializer=_FakeWorldQuestArticleSerializer(),
                bequeathed_formae_serializer=_FakeWorldQuestArticleSerializer(),
            )
            options = GenerationOptions(
                randomize_world_loot=True,
                randomize_materials=False,
                randomize_shops=False,
                randomize_extractor_recipes=True,
                randomize_initial_inventory=True,
                install_after_generation=False,
                generate_spoiler_log=True,
            )
            events: list[str] = []
            selected_plan: InitialInventoryPlan | None = None

            # Prepare extractor
            def prepare_extractor(*_args, **_kwargs):
                events.append("extractor_prepare")
                return SimpleNamespace(plan=None)

            # Build plan initial inventory
            def plan_initial_inventory(*args, **kwargs):
                nonlocal selected_plan
                events.append("initial_inventory_plan")
                selected_plan = build_initial_inventory_plan(*args, **kwargs)
                return selected_plan

            # Prepare initial
            def prepare_initial(plan, **kwargs):
                events.append("initial_inventory_prepare")
                return SimpleNamespace(
                    plan=plan,
                    source_fingerprint="fixture-source-fingerprint",
                    motorcycle_unlocked=kwargs["motorcycle_unlocked"],
                )

            # Read plan world
            def plan_world(locations, seed, **kwargs):
                events.append("world_loot_plan")
                return build_world_loot_randomization_plan(locations, seed, **kwargs)

            # Prepare gadget NPC
            def prepare_gadget_npc(_catalog, _seed, **kwargs):
                events.append("gadget_npc_prepare")
                self.assertIsNotNone(selected_plan)
                source_ids = {item.occurrence_id.casefold() for item in kwargs["source_occurrences"]}
                inventory_ids = {item.source_occurrence_id.casefold() for item in selected_plan.assignments}
                self.assertFalse(inventory_ids & source_ids)
                return SimpleNamespace(plan=build_defensive_gadget_npc_plan(
                    _catalog, _seed, enabled=False,
                ))

            ogre_shop_product = replace(
                _spoiler_product("Shop_Jadwiga_Weapon", 29, "DA_Jail_Ogre", price=0, stock=1),
                article_path="/Game/Blueprint/Article/Jail/DA_Jail_Ogre",
                item_object="DA_Jail_Ogre",
            )
            ogre_shop_stage = SimpleNamespace(
                tables=(SimpleNamespace(
                    family="NORMAL",
                    parsed=SimpleNamespace(products=(ogre_shop_product,)),
                ),),
                unavailable_optional_families=(),
            )

            with (
                patch("cv2randomizer.generation.pipeline.build_extractor_booster_pool", return_value=()),
                patch("cv2randomizer.generation.pipeline.prepare_extractor_plan", prepare_extractor),
                patch("cv2randomizer.generation.pipeline.build_initial_inventory_plan", plan_initial_inventory),
                patch("cv2randomizer.generation.pipeline.prepare_initial_inventory", prepare_initial),
                patch("cv2randomizer.generation.pipeline.build_world_loot_randomization_plan", plan_world),
                patch("cv2randomizer.generation.pipeline.prepare_defensive_gadget_npc_plan", prepare_gadget_npc),
                patch("cv2randomizer.generation.pipeline.prepare_shop_tables", return_value=ogre_shop_stage),
            ):
                result = engine.generate(GenerationRequest(
                    Path("fixture-game"), mappings, "extractor-initial-inventory-order", options, dry_run=True,
                ))

            self.assertEqual(events, [
                "world_loot_plan", "extractor_prepare", "initial_inventory_plan", "initial_inventory_prepare",
                "gadget_npc_prepare",
            ])
            self.assertEqual(len(result.initial_inventory_plan.assignments), 3)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            initial = manifest["counts"]["initial_inventory"]
            self.assertTrue(initial["enabled"])
            self.assertEqual(initial["status"], "plan_only")
            self.assertEqual(initial["source_fingerprint_sha256"], "fixture-source-fingerprint")
            self.assertEqual(len(initial["assignments"]), 3)
            self.assertEqual(manifest["counts"]["planning_priority"]["initial_inventory_rewards_reserved"], 3)
            with result.spoiler_path.open(encoding="utf-8-sig", newline="") as handle:
                spoiler_rows = [row for row in csv.DictReader(handle) if row["feature"] == "INITIAL_INVENTORY"]
            self.assertEqual(len(spoiler_rows), 3)
            self.assertEqual({row["destination"] for row in spoiler_rows}, {
                "Starting Weapon", "Starting Defensive Gadget", "Starting Jail",
            })

    # Check blood codes boosters flow through
    def test_blood_codes_and_boosters_flow_through_staging_manifest_and_spoiler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            blood_catalog = BloodCodeCatalogStore(_REPOSITORY_ROOT / "data" / "blood_codes" / "current").load("current")
            booster_catalog = BoosterCatalogStore(_REPOSITORY_ROOT / "data" / "boosters" / "current").load("current")
            seed = "bc-booster-pipeline-seed"
            blood_plan = build_blood_code_plan(blood_catalog, seed, mix_postgame_dlc_with_base=True)
            booster_plan = build_booster_plan(booster_catalog, seed, mix_postgame_dlc_with_base=True)
            packager = _FakePackager()
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=_FakeRawArticleExtractor(root / "fake-extractor-cache"),
                blood_code_catalog_store=BloodCodeCatalogStore(_REPOSITORY_ROOT / "data" / "blood_codes" / "current"),
                booster_catalog_store=BoosterCatalogStore(_REPOSITORY_ROOT / "data" / "boosters" / "current"),
                packager=packager,
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_materials=False,
                randomize_shops=False,
                randomize_enemy_drops=False,
                randomize_blood_codes=True,
                randomize_boosters=True,
                mix_postgame_dlc_with_base=True,
                install_after_generation=False,
                generate_spoiler_log=True,
            )

            # Prepare blood
            def prepare_blood(_catalog, _seed, **_kwargs):
                return SimpleNamespace(plan=blood_plan)

            # Prepare booster
            def prepare_booster(_catalog, _seed, **_kwargs):
                return SimpleNamespace(plan=booster_plan)

            # Apply blood
            def apply_blood(preparation, *, output_content_root, **_kwargs):
                header = output_content_root / "Quest" / "FixtureBloodGrant.uasset"
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_bytes(b"validated blood code package")
                header.with_suffix(".uexp").write_bytes(b"validated blood code exports")
                return BloodCodeGenerationResult(preparation.plan, (header, header.with_suffix(".uexp")), ("/Game/Quest/FixtureBloodGrant",), True)

            # Apply booster
            def apply_booster(preparation, *, output_content_root, **_kwargs):
                header = output_content_root / "Cinematics" / "CinematicScene" / "Scenario" / "SmallScenario" / "Blueprint" / "Tool" / "DT_GainBoosterSmallScenario.uasset"
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_bytes(b"validated booster table")
                header.with_suffix(".uexp").write_bytes(b"validated booster exports")
                return BoosterGenerationResult(preparation.plan, (header, header.with_suffix(".uexp")), preparation.plan.changed_slots, True)

            with (
                patch("cv2randomizer.generation.pipeline.prepare_blood_code_plan", prepare_blood),
                patch("cv2randomizer.generation.pipeline.prepare_booster_plan", prepare_booster),
                patch("cv2randomizer.generation.pipeline.apply_blood_code_plan", apply_blood),
                patch("cv2randomizer.generation.pipeline.apply_booster_plan", apply_booster),
                # This pipeline integration uses placeholder bytes, so the separate
                # real-asset staged serializer invariant is covered by its own tests.
                patch(
                    "cv2randomizer.generation.pipeline._validate_final_blood_code_artifact",
                    return_value={"logical_assignments": len(blood_plan.assignments), "packages": 1},
                ),
            ):
                result = engine.generate(GenerationRequest(Path("fixture-game"), mappings, seed, options))

            self.assertTrue(packager.called)
            self.assertEqual(len(result.output_files), 4)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["blood_codes"]["assignments"], 79)
            self.assertTrue(manifest["counts"]["blood_codes"]["reopened"])
            self.assertEqual(manifest["counts"]["boosters"]["assignments"], 102)
            self.assertTrue(manifest["counts"]["boosters"]["reopened"])
            self.assertNotIn("mix_postgame_dlc_with_base", manifest["counts"]["boosters"])
            with result.spoiler_path.open(encoding="utf-8-sig", newline="") as handle:
                features = {row["feature"] for row in csv.DictReader(handle)}
            self.assertIn("BLOOD_CODE", features)
            self.assertIn("BOOSTER", features)

    # Check defensive gadget NPC flow patches
    def test_defensive_gadget_npc_flow_patches_warp_without_pending_jadwiga(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            gadget_catalog_store = DefensiveGadgetCatalogStore(
                _REPOSITORY_ROOT / "data" / "reward_pools" / "current"
            )
            gadget_catalog = gadget_catalog_store.load("current")
            seed = "npc-gadget-seed"
            packager = _FakePackager()
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=_FakeRawArticleExtractor(root / "fake-extractor-cache"),
                defensive_gadget_catalog_store=gadget_catalog_store,
                raw_article_grant_serializer=_FakeRawArticleGrantSerializer(),
                world_quest_article_serializer=_FakeWorldQuestArticleSerializer(),
                bequeathed_formae_serializer=_FakeWorldQuestArticleSerializer(),
                packager=packager,
            )
            options = GenerationOptions(
                randomize_world_loot=True,
                randomize_materials=False,
                randomize_shops=False,
                install_after_generation=False,
                generate_spoiler_log=True,
            )

            # Prepare package data for patching
            def prepare(_catalog, _seed, **kwargs):
                return SimpleNamespace(plan=build_defensive_gadget_npc_plan(
                    _catalog,
                    _seed,
                    mix_postgame_dlc_with_base=kwargs["mix_postgame_dlc_with_base"],
                    reserved_reward_identities=kwargs["reserved_reward_identities"],
                    source_occurrences=kwargs["source_occurrences"],
                ))

            # Apply the prepared changes
            def apply(preparation, *, output_content_root, **_kwargs):
                header = output_content_root / "Quest" / "CharacterEpisode" / "FixtureWarpGrant.uasset"
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_bytes(b"validated gadget grant package")
                header.with_suffix(".uexp").write_bytes(b"validated gadget exports")
                return DefensiveGadgetGenerationResult(
                    preparation.plan,
                    (header, header.with_suffix(".uexp")),
                    ("/Game/Quest/CharacterEpisode/FixtureWarpGrant",),
                    True,
                )

            with (
                patch("cv2randomizer.generation.pipeline.prepare_defensive_gadget_npc_plan", prepare),
                patch("cv2randomizer.generation.pipeline.apply_defensive_gadget_npc_plan", apply),
                patch("cv2randomizer.generation.pipeline.apply_world_loot_plan", _fake_world_patch),
                patch("cv2randomizer.generation.pipeline.apply_world_loot_direct_grants", return_value=()),
            ):
                result = engine.generate(GenerationRequest(Path("fixture-game"), mappings, seed, options))
                world_on_dry_run = engine.generate(GenerationRequest(
                    Path("fixture-game"), mappings, seed,
                    replace(options, randomize_world_loot=True, install_after_generation=False),
                    dry_run=True,
                ))

            self.assertTrue(packager.called)
            self.assertEqual(len(result.defensive_gadget_result.output_files), 2)
            self.assertIsNotNone(result.defensive_gadget_plan)
            self.assertEqual(result.defensive_gadget_plan, world_on_dry_run.defensive_gadget_plan)
            self.assertTrue(result.defensive_gadget_result.reopened)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            gadget_counts = manifest["counts"]["defensive_gadgets"]
            self.assertEqual(gadget_counts["catalog_count"], 17)
            self.assertEqual(gadget_counts["npc_grants"]["assignments"], 1)
            self.assertEqual(gadget_counts["npc_grants"]["changed"], 1)
            self.assertEqual(gadget_counts["npc_grants"]["candidate_count"], 9)
            self.assertEqual(gadget_counts["npc_grants"]["validation_status"], "output_reopened_and_validated")
            self.assertEqual(gadget_counts["npc_grants"]["pending"], [])
            self.assertTrue(manifest["options"]["randomize_world_loot"])
            with result.spoiler_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            npc_rows = [row for row in rows if row["feature"].startswith("DEFENSIVE_GADGET_NPC")]
            self.assertEqual({row["destination"] for row in npc_rows}, {"warp_lise_grant"})

    # Check bequeathed axe direct grant is
    def test_bequeathed_axe_direct_grant_is_owned_by_world_loot(self) -> None:
        from cv2randomizer.game.bequeathed_formae_catalog import BequeathedFormaCatalogStore
        from cv2randomizer.game.catalogs import CatalogStore

        world = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        formae = BequeathedFormaCatalogStore(_REPOSITORY_ROOT / "data" / "bFormae" / "current").load()
        formae.validate_sources(world.world_loot, world.shops)
        self.assertEqual(
            tuple(item.object for item in formae.world_loot_direct_rewards),
            ("DA_ExecutionSword", "DA_LouAxe"),
        )
        self.assertEqual(tuple(item.object for item in formae.separate_direct_rewards), ())
        self.assertEqual(build_bequeathed_formae_plan(formae, "test-seed-17").assignments, ())
        self.assertIn("bequeathed_lou_axe_magmell_quest", {
            item.destination_id for item in world.world_loot
        })
        self.assertIn("bequeathed_executioner_lyle_quest", {
            item.destination_id for item in world.world_loot
        })

    # Check new direct quest destination does
    def test_new_direct_quest_destination_does_not_require_a_cached_map_descriptor(self) -> None:
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        destination = next(
            item for item in catalogs.world_loot
            if item.destination_id == "bequeathed_lou_axe_magmell_quest"
        )

        _validate_world_loot_prepared_descriptors(
            (destination,),
            {"world_loot_location_descriptors": {}},
            PackageInventory.create(()),
        )

    # Check all self mapped bequeathed formae
    def test_all_self_mapped_bequeathed_formae_complete_without_empty_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            formae_catalog_store = BequeathedFormaCatalogStore(
                _REPOSITORY_ROOT / "data" / "bFormae" / "current"
            )
            catalog = formae_catalog_store.load()
            plan = build_bequeathed_formae_plan(catalog, "formae-test")
            self.assertEqual(plan.changed_rewards, 0)
            packager = _FakePackager()
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=_FakeRawArticleExtractor(root / "fake-extractor-cache"),
                bequeathed_formae_catalog_store=formae_catalog_store,
                packager=packager,
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_materials=False,
                randomize_shops=False,
                randomize_bequeathed_formae_quest_rewards=True,
                install_after_generation=True,
            )

            # Prepare package data for patching
            def prepare(_catalog, _seed, **_kwargs):
                return SimpleNamespace(plan=plan)

            # Apply the prepared changes
            def apply(preparation, **_kwargs):
                return BequeathedFormaGenerationResult(preparation.plan, (), (), True)

            with (
                patch("cv2randomizer.generation.pipeline.prepare_bequeathed_formae_plan", prepare),
                patch("cv2randomizer.generation.pipeline.apply_bequeathed_formae_plan", apply),
            ):
                result = engine.generate(GenerationRequest(Path("fixture-game"), mappings, "formae-test", options))

            self.assertFalse(packager.called)
            self.assertIsNone(result.package_result)
            self.assertIsNone(result.installation_result)
            self.assertTrue(result.workspace.root.is_dir())
            self.assertTrue(result.workspace.logs.is_dir())
            self.assertIsNone(result.spoiler_text_path)
            self.assertIsNotNone(result.cleanup_report)
            self.assertFalse(result.workspace.packaged.exists())
            self.assertGreater(result.cleanup_report.temporary_bytes_after, 0)

    # Check shop spoiler preserve destination price
    def test_shop_spoiler_preserves_destination_price_and_reports_assigned_stock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = WorkspaceManager(root / "data").create_generation(normalize_seed("spoiler-seed"))
            products = (
                replace(
                    _spoiler_product("Shop_A", 0, "DA_Booster_Dex", price=17, stock=2),
                    article_path="/Game/Blueprint/Article/Booster/Basic/DA_Booster_Dex",
                ),
                replace(
                    _spoiler_product("Shop_A", 1, "DA_Item_MatBasicRecoveryCountUp_1", price=29, stock=5),
                    article_path="/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryCountUp_1",
                ),
            )
            shop_plan = build_shop_randomization_plan(products, "spoiler-seed", enabled_families=("NORMAL",))
            self.assertTrue(all(item.stock_override == 1 for item in shop_plan.assignments))
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_bequeathed_formae_quest_rewards=False,
                randomize_materials=False,
                randomize_shops=False,
                install_after_generation=False,
                generate_spoiler_log=True,
            )

            spoiler_path = _write_spoiler(
                workspace,
                options,
                WorldLootRandomizationPlan((), (), (), ()),
                shop_plan,
                MaterialRandomizationPlan((), (), ()),
            )

            with spoiler_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            expected = {
                f"{item.destination.family}:{item.destination.shop_row}:{item.destination.product_index}": (
                    item.destination.article_path,
                    item.source.article_path,
                    str(item.destination.price),
                    str(item.destination.uses_stock),
                    str(item.stock_override if item.stock_override is not None else item.destination.stock),
                )
                for item in shop_plan.assignments
            }
            actual = {
                row["destination"]: (
                    row["original"], row["assigned"], row["preserved_price"],
                    row["assigned_uses_stock"], row["assigned_stock"],
                )
                for row in rows if row["feature"] == "SHOP"
            }
            self.assertEqual(actual, expected)

    # Check shop world mix is reported
    def test_shop_world_mix_is_reported_in_manifest_and_spoiler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed = normalize_seed("cross-0")
            workspace = WorkspaceManager(root / "data").create_generation(seed)
            world_locations = (
                WorldLootLocation(
                    "/Game/Map/MainWorld/MainWorld/_Generated_/MixWorld1", "MixWorld1.umap", 1,
                    "BP_Pickup_WorldWeapon", "BP_PickupItem_C", "GROUND_PICKUP", "mix-world-1",
                    False, (), (RewardPayload("/Game/Blueprint/Article/Weapon/DA_WorldSword", 2, False),),
                ),
                WorldLootLocation(
                    "/Game/Map/MainWorld/MainWorld/_Generated_/MixWorld2", "MixWorld2.umap", 1,
                    "BP_Pickup_WorldMedicine", "BP_PickupItem_C", "GROUND_PICKUP", "mix-world-2",
                    False, (), (RewardPayload("/Game/Blueprint/Article/Item/DA_WorldMedicine", 5, True),),
                ),
            )
            world_plan = build_world_loot_randomization_plan(world_locations, seed)
            products = (
                _spoiler_product("Shop_A", 0, "DA_CookingRecipe", price=1000, stock=1),
                _spoiler_product("Shop_A", 1, "DA_ShopMedicine", price=25, stock=4),
            )
            world_plan, shop_plan = build_global_article_mix_plans(
                world_plan, products, seed, enabled_families=("NORMAL",),
            )
            options = GenerationOptions(
                randomize_materials=False,
                randomize_world_loot=True,
                randomize_shops=True,
                mix_shops_with_world=True,
                install_after_generation=False,
                generate_spoiler_log=True,
            )
            catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
            _write_manifest(
                workspace,
                seed=seed,
                options=options,
                catalogs=catalogs,
                game_version="fixture-build",
                catalog_revision="current",
                world_plan=world_plan,
                shop_plan=shop_plan,
                material_plan=MaterialRandomizationPlan((), (), ()),
                output_files=(),
                dry_run=True,
            )
            manifest = json.loads(workspace.manifest.read_text(encoding="utf-8"))
            self.assertTrue(manifest["options"]["mix_shops_with_world"])
            self.assertTrue(manifest["counts"]["shops"]["mix_shops_with_world"])
            pool = manifest["counts"]["global_article_pool"]
            self.assertEqual(pool["validation"], "validated")
            self.assertEqual(pool["candidate_inventory"], 4)
            self.assertGreater(pool["world_destinations_from_shop"], 0)
            self.assertGreater(pool["shop_destinations_from_world"], 0)

            spoiler_path = _write_spoiler(
                workspace,
                options,
                world_plan,
                shop_plan,
                MaterialRandomizationPlan((), (), ()),
            )
            with spoiler_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            shop_rows = {row["destination"]: row for row in rows if row["feature"] == "SHOP"}
            for assignment in shop_plan.assignments:
                key = (
                    f"{assignment.destination.family}:{assignment.destination.shop_row}:"
                    f"{assignment.destination.product_index}"
                )
                row = shop_rows[key]
                self.assertEqual(row["assigned"], assignment.source.canonical_path)
                self.assertEqual(row["preserved_price"], str(assignment.destination.price))
                self.assertEqual(row["assigned_uses_stock"], str(assignment.destination.uses_stock))
                self.assertEqual(row["assigned_stock"], str(assignment.destination.stock))
                self.assertIn("Article identity only", row["notes"])

    # Check unsupported game build fail before
    def test_unsupported_game_build_fails_before_workspace_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = self._engine(root / "data", supported=False)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            request = GenerationRequest(
                Path("fixture-game"), mappings, "seed",
                GenerationOptions(install_after_generation=False), dry_run=True,
            )

            with self.assertRaisesRegex(GenerationError, "unsupported fixture build"):
                engine.generate(request)
            self.assertFalse((root / "data" / "generated").exists())

    # Check local unverified generation require validated
    def test_local_unverified_generation_requires_a_validated_extraction_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = self._engine(root / "data", supported=False, local_unverified=True)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            request = GenerationRequest(
                Path("fixture-game"), mappings, "seed",
                GenerationOptions(install_after_generation=False), dry_run=True,
            )

            with self.assertRaisesRegex(GenerationError, "requires a validated extracted game-data cache"):
                engine.generate(request)
            self.assertFalse((root / "data" / "generated").exists())

    # Check local unverified extraction skips empty
    def test_local_unverified_extraction_skips_empty_required_package_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            pak_directory = game / "CodeVein2" / "Content" / "Paks"
            pak_directory.mkdir(parents=True)
            (pak_directory / "chunk0.utoc").write_bytes(b"fixture toc")
            (pak_directory / "chunk0.ucas").write_bytes(b"fixture ucas")
            (game / "CodeVein2.exe").write_bytes(b"local fixture executable")
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")

            class _InstalledInventory:
                # Inspect package inventory
                def inspect_package_inventory(self, _game_root):
                    return PackageInventory.create((_CORE_PACKAGE,))

            extraction_calls: list[tuple[str, ...]] = []

            class _StagedExtractor:
                # Initialize this object
                def __init__(self, cache: CacheManager) -> None:
                    self.cache = cache

                # Extract requested game assets
                def extract_assets(
                    self, game_root, virtual_paths, *, optional_virtual_paths=(),
                    game_version, data_revision, progress=None,
                ):
                    packages = tuple(virtual_paths)
                    if not packages:
                        raise AssertionError("extractor must not receive an empty package batch")
                    extraction_calls.append(packages)
                    detection = VersionDetection(
                        False, game_version, data_revision, game_root / "CodeVein2.exe",
                        "local validation only", (), True,
                    )
                    cache_entry = GameDataCacheStore(self.cache).cache_entry_path(game_root, detection)
                    assets = []
                    for package in packages:
                        normalized, relative = _normalize_asset_path(package)
                        package_root = cache_entry / "assets" / Path(*relative.parts)
                        package_root.mkdir(parents=True, exist_ok=True)
                        payload = b"fixture package"
                        filename = f"{relative.name}.uasset"
                        (package_root / filename).write_bytes(payload)
                        (package_root / ".cv2randomizer-asset.json").write_text(
                            json.dumps({
                                "virtual_path": normalized,
                                "files": [{
                                    "name": filename,
                                    "sha256": hashlib.sha256(payload).hexdigest(),
                                    "size": len(payload),
                                }],
                            }),
                            encoding="utf-8",
                        )
                        assets.append(ExtractedAsset(normalized, (package_root / filename,)))
                    return tuple(assets)

            cache_store = GameDataCacheStore(CacheManager(root / "persistent-cache"))
            engine = GenerationEngine(
                version_detector=_Detector(supported=False, local_unverified=True),
                catalog_store=CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "output"),
                extractor=_InstalledInventory(),
                game_data_cache_store=cache_store,
                staging_extractor_factory=lambda cache: _StagedExtractor(cache),
            )

            # This fixture models cache/extraction orchestration, not UE bytes.
            with patch("cv2randomizer.generation.pipeline.prepare_shop_tables", return_value=SimpleNamespace(
                tables=(SimpleNamespace(parsed=SimpleNamespace(products=())),),
            )):
                snapshot = engine.extract_game_data(game, mappings)

            self.assertEqual(snapshot.state, GameDataCacheState.READY)
            self.assertEqual(extraction_calls, [(_CORE_PACKAGE,)])
            self.assertEqual(snapshot.metadata["version_validation"], "local-unverified")

    # Check patch failure rolls back outputs
    def test_patch_failure_rolls_back_outputs_and_metadata_but_keeps_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = WorkspaceManager(root / "data")
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=manager,
                extractor=_FakeRawArticleExtractor(root / "fake-extractor-cache"),
                world_quest_article_serializer=_FakeWorldQuestArticleSerializer(),
                bequeathed_formae_serializer=_FakeWorldQuestArticleSerializer(),
            )
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            request = GenerationRequest(
                Path("fixture-game"), mappings, "rollback-seed",
                GenerationOptions(randomize_materials=False, randomize_shops=False,
                                  install_after_generation=False, generate_spoiler_log=True),
            )

            # Fail after partial write
            def fail_after_partial_write(plan: object, **kwargs: object) -> None:
                output = kwargs["output_content_root"]
                output.mkdir(parents=True)
                (output / "partial.umap").write_bytes(b"partial")
                raise RuntimeError("serializer validation failed")

            with patch("cv2randomizer.generation.pipeline.apply_world_loot_plan", fail_after_partial_write):
                with self.assertRaisesRegex(GenerationError, "serializer validation failed"):
                    engine.generate(request)

            generation = next((root / "data" / "generated").iterdir())
            self.assertFalse((generation / "staging" / "Content").exists())
            self.assertFalse((generation / "manifest.json").exists())
            self.assertFalse((generation / "spoiler_log.csv").exists())
            self.assertFalse((generation / "packaged" / "Spoiler.txt").exists())
            self.assertIn("serializer validation failed", (generation / "logs" / "generation_failure.txt").read_text())

    # Check normal generation packages validated staging
    def test_normal_generation_packages_validated_staging_and_commits_package_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            packager = _FakePackager()
            extractor = _FakeRawArticleExtractor(
                root / "fake-extractor-cache",
                container_names=("fixture-dlc.utoc",),
            )
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=extractor,
                raw_article_grant_serializer=_FakeRawArticleGrantSerializer(),
                world_quest_article_serializer=_FakeWorldQuestArticleSerializer(),
                bequeathed_formae_serializer=_FakeWorldQuestArticleSerializer(),
                packager=packager,
            )
            request = GenerationRequest(
                Path("fixture-game"), mappings, "package-seed",
                GenerationOptions(randomize_materials=False, randomize_shops=False,
                                  generate_spoiler_log=True, install_after_generation=False,
                                  ),
            )

            with (
                patch("cv2randomizer.generation.pipeline.apply_world_loot_plan", _fake_world_patch),
                patch("cv2randomizer.game.raw_article_grant_generation._validate_quest_source_fingerprint"),
            ):
                result = engine.generate(request)

            self.assertTrue(packager.called)
            self.assertEqual(extractor.inventory_calls, 1)
            self.assertTrue(result.spoiler_text_path is not None and result.spoiler_text_path.is_file())
            direct_assignments = [
                assignment for assignment in result.world_plan.assignments
                if assignment.destination.destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}
            ]
            self.assertEqual(len(result.world_direct_grant_results), len(direct_assignments))
            results_by_id = {item.destination_id: item for item in result.world_direct_grant_results}
            self.assertTrue(all(
                results_by_id[str(assignment.destination.destination_id)].changed
                == (tuple(item.article_path for item in assignment.destination.rewards)
                    != tuple(item.article_path for item in assignment.assigned_rewards))
                for assignment in direct_assignments
            ))
            self.assertTrue(all(
                grant.reopened_and_validated for grant in result.world_direct_grant_results if grant.changed
            ))
            staged_paths = {path.as_posix() for path in result.output_files}
            direct_package_names = (
                "QST_MainEpisode_DLC_e90360",
                "QST_CharacterEpisode_Iris_c00010",
                "QST_CharacterEpisode_Iris_c00070",
            )
            for package_name in direct_package_names:
                self.assertTrue(any(path.endswith(package_name + ".uasset") for path in staged_paths))
                self.assertTrue(any(path.endswith(package_name + ".uexp") for path in staged_paths))
            self.assertEqual(len(result.package_result.files), 3)
            document = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(any(
                path.endswith(package_name + ".uasset")
                for path in staged_paths
                for package_name in (
                    "BP_SmallScenario_JadwigaServant1",
                    "BP_SmallScenario_JadwigaServant2",
                    "BP_SmallScenario_JadwigaServant3",
                )
            ))
            self.assertTrue(any(
                "Jadwiga Servant - Lightning Barrage Reward" in warning
                for warning in document["content_availability"]["skipped_feature_groups"]
            ))
            self.assertEqual(document["status"], "package_validated")
            self.assertEqual(document["spoiler_txt_file"], "Spoiler.txt")
            spoiler_text = result.spoiler_text_path.read_text(encoding="utf-8")
            self.assertIn("=== World Loot ===", spoiler_text)
            self.assertNotIn("/Game/Map/", spoiler_text)
            self.assertNotIn("_Generated_", spoiler_text)
            self.assertIn("#", spoiler_text)
            self.assertIn("Total Randomized:", spoiler_text)
            self.assertIn("Unchanged:", spoiler_text)
            self.assertIn(
                "Skipped for this game version: 4 (3x World Loot, 1x Defensive Gadget Grants)",
                spoiler_text,
            )
            self.assertIn("App Version: v1.0.0", spoiler_text)
            self.assertIn("Game Version: fixture-build", spoiler_text)
            self.assertIn("Randomizer Settings:\n", spoiler_text)
            self.assertIn("Disabled Unlock all 6 Booster Slots", spoiler_text)
            self.assertIn("Disabled Unlock all NPC Trader Shop Tiers", spoiler_text)
            self.assertIn("Disabled Unlock Second Weapon Slot", spoiler_text)
            self.assertIn("Disabled Unrestricted Blood Code Mastery", spoiler_text)
            self.assertLess(spoiler_text.index("Created:"), spoiler_text.index("Total Randomized:"))
            self.assertLess(spoiler_text.index("Total Randomized:"), spoiler_text.index("App Version:"))
            self.assertLess(spoiler_text.index("Game Version:"), spoiler_text.index("Randomizer Settings:"))
            self.assertLess(spoiler_text.index("Randomizer Settings:"), spoiler_text.index("Seed:"))
            self.assertLess(spoiler_text.index("Seed:"), spoiler_text.index("Disabled Initial Inventory"))
            self.assertNotIn("Randomized assignments by category", spoiler_text)
            self.assertNotIn("=== Materials ===", spoiler_text)
            self.assertFalse(document["options"]["randomize_materials"])
            self.assertNotIn("dlc_enabled", document)
            self.assertIn("source_packages", document["content_availability"])
            direct_grants = document["counts"]["world_loot"]["direct_article_grants"]
            self.assertEqual(direct_grants["assignments"], len(direct_assignments))
            self.assertEqual(len(direct_grants["destinations"]), len(direct_assignments))
            self.assertTrue(all(
                not (row["destination_id"] or "").startswith(("one_time_enemy_", "harmony_moon_", "QST_FieldEnemy_"))
                for row in direct_grants["destinations"]
            ))
            self.assertTrue(all(
                row["reopened_and_validated"]
                for row in direct_grants["destinations"] if row["changed"]
            ))
            expected_field_ids = {
                row["id"] for row in json.loads(
                    (_REPOSITORY_ROOT / "data" / "field_enemy" / "current" / "field_enemy_direct_rewards.json").read_text(encoding="utf-8")
                )["locations"]
            }
            self.assertEqual(len(expected_field_ids), 24)
            field_grant_destinations = {
                row["destination_id"] for row in direct_grants["destinations"]
                if row["destination_id"] in expected_field_ids
            }
            self.assertEqual(field_grant_destinations, set())
            protected_field_ids = {
                row["id"] for row in json.loads(
                    (_REPOSITORY_ROOT / "data" / "field_enemy" / "current" / "field_enemy_key_exclusions.json").read_text(encoding="utf-8")
                )["exclusions"]
            }
            self.assertEqual(len(protected_field_ids), 10)
            self.assertTrue(protected_field_ids.isdisjoint(
                {row["destination_id"] for row in direct_grants["destinations"]}
            ))
            catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
            expected_non_field_ids = {
                location.destination_id for location in catalogs.world_loot
                if location.destination_id is not None
                and location.destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}
                and location.destination_kind != "RAW_ARTICLE_GRANT"
                and location.destination_id not in expected_field_ids
                and not location.destination_id.startswith(("one_time_enemy_", "harmony_moon_", "QST_FieldEnemy_"))
            }
            self.assertEqual(
                {row["destination_id"] for row in direct_grants["destinations"]} - expected_field_ids,
                expected_non_field_ids,
            )
            field_counts = document["counts"]["field_enemy"]
            self.assertFalse(field_counts["enabled_by_enemy_drops"])
            self.assertEqual(field_counts["catalog_eligible_locations"], 0)
            self.assertEqual(field_counts["eligible_where_destinations"], 0)
            self.assertEqual(field_counts["randomizable_destinations"], 0)
            self.assertEqual(field_counts["disabled_due_to_missing_sources"], 0)
            self.assertEqual(field_counts["protected_exclusions"], 0)
            self.assertEqual(field_counts["protected_exclusions_extracted"], 0)
            field_changed_ids = {
                item.destination_id for item in result.world_direct_grant_results
                if item.destination_id in expected_field_ids and item.changed
            }
            field_staged = {
                path.stem for path in result.output_files
                if path.as_posix().casefold().startswith((result.output_content_root / "quest" / "fieldenemy").as_posix().casefold())
                and path.suffix.casefold() == ".uasset"
            }
            self.assertEqual(field_changed_ids, set())
            self.assertEqual(field_staged, set())
            self.assertEqual(field_counts["patched_packages"], len(field_staged))
            self.assertTrue(protected_field_ids.isdisjoint(field_staged))
            with result.spoiler_path.open(encoding="utf-8-sig", newline="") as handle:
                spoiler_rows = list(csv.DictReader(handle))
            non_enemy_story_labels = {
                "Iris Character Episode - Initial Reward",
                "Iris Character Episode - Reward 1",
                "Iris Character Episode - Reward 2",
            }
            self.assertEqual(
                {row["destination"] for row in spoiler_rows if row["feature"] == "WORLD_LOOT" and (
                    "Jadwiga Servant" in row["destination"]
                    or "DLC e90360" in row["destination"]
                    or row["destination"].startswith("Harmony Moon -")
                    or row["destination"] in non_enemy_story_labels
                )},
                {
                    "DLC e90360 - Gift Heal Point Up Reward",
                    *non_enemy_story_labels,
                },
            )
            self.assertEqual(document["package"]["name"], "CV2Randomizer_P")
            self.assertEqual(len(document["package"]["files"]), 3)
            self.assertEqual(document["app_name"], "CodeVein2Randomizer")
            self.assertEqual(document["installation"]["status"], "not_requested")
            self.assertEqual(set(document["output_filenames"]), {path.name for path in result.package_result.files})

    # Check optional sources missing do not
    def test_optional_sources_missing_do_not_abort_base_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extractor = _FakeRawArticleExtractor(root / "cache", package_paths=())
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=extractor,
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_bequeathed_formae_quest_rewards=False,
                randomize_materials=False,
                randomize_shops=False,
                install_after_generation=False,
            )
            result = engine.generate(
                GenerationRequest(Path("fixture-game"), None, "optional-assets-absent", options, dry_run=True)
            )
            self.assertEqual(extractor.inventory_calls, 1)
            self.assertEqual(extractor.extraction_calls, 0)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertNotIn("dlc_enabled", manifest)
            self.assertEqual(manifest["options"], options.to_dict())
            self.assertTrue(manifest["content_availability"]["source_packages"]["missing_sources"])

    # Keep generating if the optional dialogue patch fails
    def test_dialogue_skip_failure_does_not_abort_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"fixture mapping")
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=_FakeRawArticleExtractor(root / "cache", package_paths=()),
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_bequeathed_formae_quest_rewards=False,
                randomize_materials=False,
                randomize_shops=False,
                tap_to_skip_dialogue=True,
                install_after_generation=False,
            )
            with patch(
                "cv2randomizer.generation.pipeline.apply_tap_to_skip_dialogue",
                side_effect=DialogueSkipError("unsupported subtitle layout"),
            ):
                result = engine.generate(
                    GenerationRequest(Path("fixture-game"), mappings, "dialogue-skip-failure", options)
                )

        self.assertIsNotNone(result)
        self.assertIsNotNone(result.dialogue_skip_result)
        self.assertFalse(result.dialogue_skip_result.applied)
        self.assertTrue(any("unsupported subtitle layout" in warning for warning in result.availability_warnings))

    # Check missing core installation asset still
    def test_missing_core_installation_asset_still_blocks_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extractor = _FakeRawArticleExtractor(
                root / "cache", package_paths=(), include_core=False,
            )
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=extractor,
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_bequeathed_formae_quest_rewards=False,
                randomize_materials=False,
                randomize_shops=False,
                install_after_generation=False,
            )
            with self.assertRaisesRegex(GenerationError, "missing required core source packages"):
                engine.generate(GenerationRequest(Path("fixture-game"), None, "missing-core", options))
            self.assertFalse((root / "data" / "generated").exists())

    # Check NPC traders keep base pool
    def test_npc_traders_only_keeps_base_pool_when_liv_table_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
            trade_dlc_package = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_Trade_DLC"
            packages = tuple(path for path in _fixture_source_packages() if path != trade_dlc_package)
            extractor = _FakeRawArticleExtractor(root / "cache", package_paths=packages)
            trader_catalog = tuple(
                product for product in catalogs.shops
                if product.family == "TRADE" and product.shop_row.startswith("Character.Type.Buddy.")
            )
            parsed_products = tuple(
                ShopTableProduct(
                    family=product.family,
                    shop_row=product.shop_row,
                    product_index=product.product_index,
                    article_path=product.original_product_path,
                    item_object=product.item_object,
                    article_path_offset=0,
                    article_path_bytes=struct.pack("<iiiii", 0, 0, 0, 0, 0),
                    price=product.price,
                    price_offset=20,
                    price_bytes=struct.pack("<i", product.price),
                    uses_stock=product.uses_stock,
                    uses_stock_offset=24,
                    uses_stock_bytes=bytes((int(product.uses_stock),)),
                    stock=product.stock,
                    stock_offset=25,
                    stock_bytes=struct.pack("<i", product.stock),
                    important=False,
                    evaluation_formula=f"preserve:{product.shop_row}:{product.product_index}",
                    evaluation_raw=f"raw:{product.shop_row}:{product.product_index}".encode(),
                    raw_bytes=b"fixture",
                    protected=product.protected,
                    protection_reason=product.protection_reason,
                )
                for product in trader_catalog
            )
            stage = SimpleNamespace(
                tables=(SimpleNamespace(family="TRADE", parsed=SimpleNamespace(products=parsed_products)),),
                unavailable_optional_families=(),
            )
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=extractor,
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_bequeathed_formae_quest_rewards=False,
                randomize_materials=False,
                randomize_shops=True,
                randomize_npc_traders=True,
                install_after_generation=False,
                generate_spoiler_log=True,
            )
            with patch(
                "cv2randomizer.generation.pipeline.prepare_shop_tables",
                return_value=stage,
            ) as prepare_shops:
                result = engine.generate(
                    GenerationRequest(Path("fixture-game"), mappings, "traders-without-liv", options, dry_run=True)
                )

            self.assertEqual(prepare_shops.call_args.kwargs["enabled_families"], ("NORMAL", "DLC", "LUXURY", "TRADE"))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            traders = manifest["counts"]["npc_traders"]
            self.assertTrue(traders["included_in_shops"])
            self.assertFalse(traders["mixed_with_world_pool"])
            self.assertNotIn("mode", traders)
            self.assertEqual(traders["available_families"], ["TRADE"])
            self.assertEqual(traders["unavailable_families"], ["TRADE_DLC"])
            self.assertEqual((traders["available_rows"], traders["available_slots"]), (12, 392))
            self.assertEqual(traders["assignments"], traders["eligible_slots"])
            self.assertGreater(traders["assignments"], 0)
            self.assertIn("Liv skipped because the optional TRADE_DLC table is absent", " ".join(manifest["content_availability"]["skipped_feature_groups"]))
            self.assertTrue(result.spoiler_path.is_file())

    # Check NPC trader mixed use global
    def test_npc_trader_mixed_uses_global_pool_with_ordinary_shop_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
            extractor = _FakeRawArticleExtractor(root / "cache")
            selected_catalog = (
                *tuple(item for item in catalogs.shops if item.family == "NORMAL")[:2],
                *(item for item in catalogs.shops
                  if item.family in {"TRADE", "TRADE_DLC"}
                  and item.shop_row.startswith("Character.Type.Buddy.")),
            )
            parsed_products = tuple(
                ShopTableProduct(
                    family=product.family,
                    shop_row=product.shop_row,
                    product_index=product.product_index,
                    article_path=product.original_product_path,
                    item_object=product.item_object,
                    article_path_offset=0,
                    article_path_bytes=struct.pack("<iiiii", 0, 0, 0, 0, 0),
                    price=product.price,
                    price_offset=20,
                    price_bytes=struct.pack("<i", product.price),
                    uses_stock=product.uses_stock,
                    uses_stock_offset=24,
                    uses_stock_bytes=bytes((int(product.uses_stock),)),
                    stock=product.stock,
                    stock_offset=25,
                    stock_bytes=struct.pack("<i", product.stock),
                    important=False,
                    evaluation_formula=f"preserve:{product.shop_row}:{product.product_index}",
                    evaluation_raw=f"raw:{product.shop_row}:{product.product_index}".encode(),
                    raw_bytes=b"fixture",
                    protected=product.protected,
                    protection_reason=product.protection_reason,
                )
                for product in selected_catalog
            )
            stage = SimpleNamespace(
                tables=tuple(
                    SimpleNamespace(family=family, parsed=SimpleNamespace(products=tuple(
                        item for item in parsed_products if item.family == family
                    )))
                    for family in ("NORMAL", "TRADE", "TRADE_DLC")
                ),
                unavailable_optional_families=(),
            )
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=extractor,
                world_quest_article_serializer=_FakeWorldQuestArticleSerializer(),
                bequeathed_formae_serializer=_FakeWorldQuestArticleSerializer(),
            )
            options = GenerationOptions(
                randomize_world_loot=True,
                randomize_materials=False,
                randomize_shops=True,
                mix_shops_with_world=True,
                install_after_generation=False,
                generate_spoiler_log=True,
            )
            with patch(
                "cv2randomizer.generation.pipeline.prepare_shop_tables",
                return_value=stage,
            ) as prepare_shops:
                result = engine.generate(
                    GenerationRequest(Path("fixture-game"), mappings, "mixed-traders", options, dry_run=True)
                )

            self.assertEqual(set(prepare_shops.call_args.kwargs["enabled_families"]), {"NORMAL", "DLC", "LUXURY", "TRADE", "TRADE_DLC"})
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            traders = manifest["counts"]["npc_traders"]
            pool = manifest["counts"]["global_article_pool"]
            self.assertTrue(traders["included_in_shops"])
            self.assertTrue(traders["mixed_with_world_pool"])
            self.assertNotIn("mode", traders)
            self.assertEqual((traders["available_rows"], traders["available_slots"]), (13, 423))
            self.assertTrue(pool["enabled"])
            self.assertEqual(pool["shop_locations"], 425)
            self.assertEqual(manifest["counts"]["shops"]["assignments"], 2)
            self.assertEqual(traders["assignments"], 423)
            self.assertEqual(traders["assignments"] + manifest["counts"]["shops"]["assignments"], 425)
            spoiler = result.spoiler_path.read_text(encoding="utf-8")
            self.assertIn("NPC_TRADER", spoiler)

    # Check missing optional shop tables are
    def test_missing_optional_shop_tables_are_skipped_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            optional_tables = {
                "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_DLC",
                "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_Trade_DLC",
            }
            packages = tuple(
                package for package in _fixture_source_packages()
                if package.casefold() not in {item.casefold() for item in optional_tables}
            )
            extractor = _FakeRawArticleExtractor(root / "cache", package_paths=packages)
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=extractor,
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_bequeathed_formae_quest_rewards=False,
                randomize_materials=False,
                randomize_shops=True,
                install_after_generation=False,
            )
            with patch(
                "cv2randomizer.generation.pipeline.prepare_shop_tables",
                return_value=SimpleNamespace(tables=(), unavailable_optional_families=()),
            ) as prepare_shops:
                result = engine.generate(
                    GenerationRequest(Path("fixture-game"), mappings, "shops-by-package", options, dry_run=True)
                )
            self.assertEqual(
                prepare_shops.call_args.kwargs["enabled_families"],
                ("NORMAL", "LUXURY", "TRADE"),
            )
            self.assertEqual(extractor.extraction_calls, 0)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            missing = {
                row["package"] for row in manifest["content_availability"]["source_packages"]["missing_sources"]
            }
            self.assertTrue(optional_tables.issubset(missing))
            self.assertNotIn("dlc_enabled", manifest)

    # Check inventory helpers keep unknown world
    def test_inventory_helpers_keep_unknown_world_and_material_rows_when_assets_exist(self) -> None:
        from cv2randomizer.generation.pipeline import _available_material_locations, _available_world_locations
        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        world = next(item for item in catalogs.world_loot if item.content_class == "UNKNOWN")
        material = next(item for item in catalogs.materials if item.where_content_class == "UNKNOWN")
        inventory = PackageInventory.create((
            world.package_path,
            material.package_path,
            material.resource_class_package,
            material.reward.article_path,
        ))
        self.assertEqual(_available_world_locations((world,), inventory), (world,))
        self.assertEqual(_available_material_locations((material,), inventory), (material,))
        self.assertEqual(_available_world_locations((world,), PackageInventory.create(())), ())
        cdo = replace(
            world,
            destination_kind="BLUEPRINT_CDO_ARTICLE_BULK",
            parent_blueprint_packages=("/Game/Blueprint/FieldGimmick/PickupItem/BP_PickupItem",),
        )
        self.assertEqual(_available_world_locations((cdo,), PackageInventory.create((cdo.package_path,))), ())
        self.assertEqual(_available_world_locations((cdo,), PackageInventory.create((
            cdo.package_path, *cdo.parent_blueprint_packages,
        ))), (cdo,))
        material_sources = (
            material.package_path,
            material.resource_class_package,
            material.reward.article_path,
        )
        for missing_source in material_sources:
            partial_inventory = PackageInventory.create(
                source for source in material_sources if source != missing_source
            )
            self.assertEqual(_available_material_locations((material,), partial_inventory), ())

    # Check Harmony quests follow DLC availability
    def test_harmony_quest_rewards_are_available_only_with_their_packages(self) -> None:
        from cv2randomizer.generation.pipeline import _available_world_locations

        catalogs = CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs").load("current")
        harmony_rewards = tuple(
            item for item in catalogs.world_loot
            if (item.destination_id or "").startswith("harmony_moon_")
        )
        self.assertEqual(len(harmony_rewards), 5)
        self.assertTrue(all(item.destination_kind == "QUEST_ARTICLE_GRANT" for item in harmony_rewards))
        self.assertEqual(_available_world_locations(harmony_rewards, PackageInventory.create(())), ())

        installed_dlc = PackageInventory.create(item.package_path for item in harmony_rewards)
        self.assertEqual(_available_world_locations(harmony_rewards, installed_dlc), harmony_rewards)

    # Check renewable ingredients are prepared separate
    def test_renewable_ingredients_are_prepared_as_a_separate_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            extractor = _FakeRawArticleExtractor(root / "cache")
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=CatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=extractor,
            )
            options = GenerationOptions(
                randomize_world_loot=False,
                randomize_bequeathed_formae_quest_rewards=False,
                randomize_materials=True,
                randomize_shops=True,
                install_after_generation=False,
            )

            with (
                patch(
                    "cv2randomizer.generation.pipeline.prepare_material_plan",
                    return_value=SimpleNamespace(plan=MaterialRandomizationPlan((), (), ())),
                ) as prepare_materials,
                patch("cv2randomizer.generation.pipeline.apply_material_plan") as apply_materials,
                patch("cv2randomizer.game.material_dependencies.MaterialDependencyResolver.stage_for_locations") as stage_materials,
                patch(
                    "cv2randomizer.generation.pipeline.prepare_shop_tables",
                    return_value=SimpleNamespace(tables=(), unavailable_optional_families=()),
                ) as prepare_shops,
            ):
                result = engine.generate(
                    GenerationRequest(Path("fixture-game"), mappings, "unknown-content", options, dry_run=True)
                )

            self.assertEqual(extractor.extraction_calls, 0)
            self.assertTrue(options.randomize_materials)
            prepare_materials.assert_called_once()
            self.assertEqual(len(prepare_materials.call_args.args[0]), 2253)
            self.assertTrue(all(
                item.mirror_of_package_path is None or item.mirror_of_export_index is not None
                for item in prepare_materials.call_args.args[0]
            ))
            apply_materials.assert_not_called()
            stage_materials.assert_not_called()
            self.assertEqual(prepare_shops.call_args.kwargs["enabled_families"], (
                "NORMAL", "DLC", "LUXURY", "TRADE", "TRADE_DLC",
            ))
            self.assertIsNotNone(result.material_preparation)
            self.assertIsNone(result.material_result)
            self.assertEqual(result.shop_plan.assignments, ())
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["counts"]["shops"]["assignments"], 0)
            self.assertEqual(manifest["options"]["randomize_materials"], True)

    # Check generate install copies owned containers
    def test_generate_and_install_copies_owned_containers_and_spoiler_to_fake_game(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = root / "game"
            paks = game_root / "CodeVein2" / "Content" / "Paks"
            paks.mkdir(parents=True)
            vanilla = paks / "pakchunk0-Windows.pak"
            vanilla.write_bytes(b"vanilla")
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            installer = InstallManager(root / "app-state")
            workspace_manager = WorkspaceManager(root / "data")
            persistent_cache_asset = workspace_manager.root / "extracted" / "READY" / "Content.uasset"
            persistent_cache_asset.parent.mkdir(parents=True)
            persistent_cache_asset.write_bytes(b"persistent vanilla cache")
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=workspace_manager,
                extractor=_FakeRawArticleExtractor(root / "fake-extractor-cache"),
                raw_article_grant_serializer=_FakeRawArticleGrantSerializer(),
                world_quest_article_serializer=_FakeWorldQuestArticleSerializer(),
                bequeathed_formae_serializer=_FakeWorldQuestArticleSerializer(),
                packager=_FakePackager(),
                installer=installer,
            )
            request = GenerationRequest(
                game_root, mappings, "install-seed",
                GenerationOptions(randomize_materials=False, randomize_shops=False,
                                  install_after_generation=True),
            )

            with (
                patch("cv2randomizer.generation.pipeline.apply_world_loot_plan", _fake_world_patch),
                patch("cv2randomizer.game.raw_article_grant_generation._validate_quest_source_fingerprint"),
            ):
                result = engine.generate(request)

            self.assertIsNotNone(result.installation_result)
            self.assertEqual(len(result.installation_result.files), 4)
            ownership = json.loads(installer.state_path.read_text(encoding="utf-8"))
            for installed in result.installation_result.files:
                expected_hash = (
                    ownership["files"].get(installed.name)
                    or ownership["spoiler_file"]["sha256"]
                )
                self.assertEqual(
                    hashlib.sha256(installed.read_bytes()).hexdigest(),
                    expected_hash,
                )
            self.assertEqual(
                ownership["spoiler_file"]["filename"],
                "Spoiler.txt",
            )
            self.assertEqual(vanilla.read_bytes(), b"vanilla")
            self.assertTrue(all(not path.exists() for path in result.package_result.files))
            self.assertFalse(result.manifest_path.exists())
            self.assertTrue(result.workspace.root.is_dir())
            self.assertTrue(result.workspace.logs.is_dir())
            self.assertTrue(result.spoiler_text_path and result.spoiler_text_path.is_file())
            spoiler_lines = result.spoiler_text_path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(spoiler_lines[0].startswith("Created: "))
            self.assertIn("Randomizer Settings:", spoiler_lines)
            self.assertIn("Seed: install-seed", spoiler_lines)
            self.assertGreater(spoiler_lines.index("Seed: install-seed"), spoiler_lines.index("Randomizer Settings:"))
            self.assertLess(
                spoiler_lines.index("Seed: install-seed"),
                spoiler_lines.index("Enabled World Loot (always on)"),
            )
            self.assertEqual(result.spoiler_text_path.name, "Spoiler.txt")
            self.assertEqual(result.spoiler_text_path.parent, result.installation_result.mods_directory)
            self.assertFalse((root / "data" / "Spoiler.txt").exists())
            self.assertGreater(result.cleanup_report.temporary_bytes_before, 0)
            self.assertGreater(result.cleanup_report.temporary_bytes_after, 0)
            self.assertFalse(result.workspace.packaged.exists())
            self.assertFalse(result.workspace.staging.exists())
            self.assertTrue(persistent_cache_asset.is_file())
            removal = installer.remove(game_root=game_root)
            self.assertEqual(len(removal.removed_files), 4)
            self.assertTrue(vanilla.is_file())

    # Check install failure rolls back generation
    def test_install_failure_rolls_back_generation_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_root = root / "game"
            paks = game_root / "CodeVein2" / "Content" / "Paks"
            paks.mkdir(parents=True)
            (paks / "pakchunk0-Windows.pak").write_bytes(b"vanilla")
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            packager = _FakePackager()
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=WorkspaceManager(root / "data"),
                extractor=_FakeRawArticleExtractor(root / "fake-extractor-cache"),
                raw_article_grant_serializer=_FakeRawArticleGrantSerializer(),
                world_quest_article_serializer=_FakeWorldQuestArticleSerializer(),
                bequeathed_formae_serializer=_FakeWorldQuestArticleSerializer(),
                packager=packager,
                installer=_FailingInstaller(),  # type: ignore[arg-type]
            )
            request = GenerationRequest(
                game_root, mappings, "install-failure-seed",
                GenerationOptions(randomize_materials=False, randomize_shops=False,
                                  install_after_generation=True),
            )

            with (
                patch("cv2randomizer.generation.pipeline.apply_world_loot_plan", _fake_world_patch),
                patch("cv2randomizer.game.raw_article_grant_generation._validate_quest_source_fingerprint"),
            ):
                with self.assertRaisesRegex(GenerationError, "simulated install failure") as raised:
                    engine.generate(request)

            self.assertEqual(str(raised.exception), "Generation failed: simulated install failure")

            generation = next((root / "data" / "generated").iterdir())
            self.assertFalse((generation / "staging" / "Content").exists())
            self.assertFalse((generation / "manifest.json").exists())
            self.assertFalse(any((generation / "packaged").glob("CV2Randomizer_P.*")))
            self.assertIn(
                "simulated install failure",
                (generation / "logs" / "generation_failure.txt").read_text(encoding="utf-8"),
            )

    # Check package verification failure rolls back
    def test_package_verification_failure_rolls_back_staging_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = root / "CV2.usmap"
            mappings.write_bytes(b"test mapping")
            packager = _FakePackager(fail=True)
            manager = WorkspaceManager(root / "data")
            engine = GenerationEngine(
                version_detector=_Detector(),
                catalog_store=_ClassifiedFixtureCatalogStore(_REPOSITORY_ROOT / "data" / "catalogs"),
                workspace_manager=manager,
                extractor=_FakeRawArticleExtractor(root / "fake-extractor-cache"),
                raw_article_grant_serializer=_FakeRawArticleGrantSerializer(),
                world_quest_article_serializer=_FakeWorldQuestArticleSerializer(),
                bequeathed_formae_serializer=_FakeWorldQuestArticleSerializer(),
                packager=packager,
            )
            request = GenerationRequest(
                Path("fixture-game"), mappings, "package-failure-seed",
                GenerationOptions(randomize_materials=False, randomize_shops=False,
                                  install_after_generation=False),
            )

            with (
                patch("cv2randomizer.generation.pipeline.apply_world_loot_plan", _fake_world_patch),
                patch("cv2randomizer.game.raw_article_grant_generation._validate_quest_source_fingerprint"),
            ):
                with self.assertRaisesRegex(GenerationError, "container verification failure"):
                    engine.generate(request)

            generation = next((root / "data" / "generated").iterdir())
            self.assertFalse((generation / "staging" / "Content").exists())
            self.assertFalse((generation / "manifest.json").exists())
            self.assertTrue(packager.called)
            self.assertIn("container verification failure", (generation / "logs" / "generation_failure.txt").read_text())

    # Check options reject non boolean values
    def test_options_reject_non_boolean_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "randomize_shops.*boolean"):
            GenerationOptions(randomize_shops=1)  # type: ignore[arg-type]
        migrated = GenerationOptions.from_dict({"shops_normal": False, "randomize_npc_traders": False})
        self.assertTrue(migrated.shops_normal)
        self.assertTrue(migrated.randomize_npc_traders)
        self.assertNotIn("shops_normal", migrated.to_dict())

    # Check legacy shop DLC checkbox setting
    def test_legacy_shop_dlc_checkbox_setting_is_ignored(self) -> None:
        options = GenerationOptions.from_dict({"shops_dlc": True})
        self.assertTrue(options.shops_normal)
        self.assertTrue(options.shops_trade)
        self.assertNotIn("shops_dlc", options.to_dict())

    # Check world overrides are filtered material
    def test_world_overrides_are_filtered_to_material_maps_with_class_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source" / "Content"
            intermediate_root = root / "extracted" / "world-loot" / "Content"
            world_packages = []
            output_headers = {}
            for index, name in enumerate(("Changed", "Unchanged", "Unrelated")):
                relative = Path("Map") / f"{name}.umap"
                source_header = source_root / relative
                output_header = intermediate_root / relative
                source_header.parent.mkdir(parents=True, exist_ok=True)
                output_header.parent.mkdir(parents=True, exist_ok=True)
                source_header.write_bytes(f"source-{name}".encode())
                output_header.write_bytes(f"world-{name}".encode())
                world_packages.append(WorldLootPackagePatchResult(source_header, output_header, ()))
                output_headers[name] = output_header

            changed_destination = MaterialLocation(
                "/Game/Map/Changed", "Changed.umap", 1, "Material_Changed",
                "BP_Material_C", "/Game/Material/ClassA", "ClassA_C",
                RewardPayload("/Game/Item/DA_ClassA", 1, True), "save-changed", False, None,
            )
            changed_source = MaterialLocation(
                "/Game/Map/Source", "Source.umap", 2, "Material_Source",
                "BP_Material_C", "/Game/Material/ClassB", "ClassB_C",
                RewardPayload("/Game/Item/DA_ClassB", 1, True), "save-source", False, None,
            )
            unchanged_destination = MaterialLocation(
                "/Game/Map/Unchanged", "Unchanged.umap", 3, "Material_Unchanged",
                "BP_Material_C", "/Game/Material/ClassC", "ClassC_C",
                RewardPayload("/Game/Item/DA_ClassC", 1, True), "save-unchanged", False, None,
            )
            unchanged_source = MaterialLocation(
                "/Game/Map/Source", "Source.umap", 4, "Material_SourceSameClass",
                "BP_Material_C", "/Game/Material/ClassC", "ClassC_C",
                RewardPayload("/Game/Item/DA_ClassC", 1, True), "save-source-same", False, None,
            )
            material_plan = MaterialRandomizationPlan(
                (
                    MaterialAssignment(changed_destination, changed_source),
                    MaterialAssignment(unchanged_destination, unchanged_source),
                ),
                (),
                (),
            )
            world_result = WorldLootGenerationResult(
                3,
                3,
                tuple(world_packages),
                tuple(output_headers.values()),
            )

            overrides = _world_package_overrides(
                world_result,
                source_content_root=source_root,
                intermediate_content_root=intermediate_root,
                work_root=root,
                material_plan=material_plan,
            )

            self.assertEqual(overrides, {"/game/map/changed": output_headers["Changed"]})

    # Check world outputs already final root
    def test_world_outputs_already_in_final_root_skip_copy_and_hash_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source" / "Content"
            output_root = root / "staging" / "Content"
            relative_header = Path("Map") / "Generated" / "Map001.umap"
            source_header = source_root / relative_header
            final_header = output_root / relative_header
            for header, payload in (
                (source_header, b"source"),
                (final_header, b"world-patched"),
                (final_header.with_suffix(".uexp"), b"world-export"),
            ):
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_bytes(payload)
            result = WorldLootGenerationResult(
                1, 1,
                (WorldLootPackagePatchResult(source_header, final_header, ()),),
                (final_header, final_header.with_suffix(".uexp")),
            )

            with patch("cv2randomizer.generation.pipeline._copy_validated_file") as copy_file:
                _copy_world_outputs(
                    result,
                    intermediate_content_root=output_root,
                    output_content_root=output_root,
                    material_result=None,
                )
            copy_file.assert_not_called()
            combined = _remap_world_result(
                result,
                source_content_root=source_root,
                intermediate_content_root=output_root,
                output_content_root=output_root,
            )
            self.assertEqual(combined.output_packages, (final_header, final_header.with_suffix(".uexp")))
            self.assertEqual(final_header.read_bytes(), b"world-patched")

    # Check prepared cache world output composes
    def test_prepared_cache_world_output_composes_without_workspace_source_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source" / "Content"
            cache_root = root / "cache" / "prepared-assets"
            intermediate_root = root / "extracted" / "world-loot" / "Content"
            output_root = root / "staging" / "Content"
            relative_header = Path("Map") / "Generated" / "Map001.umap"
            prepared_header = cache_root / relative_header
            world_header = intermediate_root / relative_header
            final_header = output_root / relative_header
            for header, payload in (
                (prepared_header, b"prepared-template"),
                (world_header, b"world-patched"),
            ):
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_bytes(payload)
                header.with_suffix(".uexp").write_bytes(payload + b"-export")
            world_result = WorldLootGenerationResult(
                1, 1,
                (WorldLootPackagePatchResult(prepared_header, world_header, ()),),
                (world_header, world_header.with_suffix(".uexp")),
            )

            overrides = _world_package_overrides(
                world_result,
                source_content_root=source_root,
                intermediate_content_root=intermediate_root,
                work_root=root,
            )
            _copy_world_outputs(
                world_result,
                intermediate_content_root=intermediate_root,
                output_content_root=output_root,
                material_result=None,
            )
            combined = _remap_world_result(
                world_result,
                source_content_root=source_root,
                intermediate_content_root=intermediate_root,
                output_content_root=output_root,
            )

            self.assertEqual(overrides, {"/game/map/generated/map001": world_header})
            self.assertEqual(combined.package_results[0].output_path, final_header)
            self.assertEqual(combined.output_packages, (final_header, final_header.with_suffix(".uexp")))
            self.assertEqual(final_header.read_bytes(), b"world-patched")

    # Check world material outputs compose one
    def test_world_and_material_outputs_compose_to_one_map_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source" / "Content"
            intermediate_root = root / "extracted" / "world-loot" / "Content"
            output_root = root / "staging" / "Content"
            relative_header = Path("Map") / "Generated" / "Map001.umap"
            source_header = source_root / relative_header
            world_header = intermediate_root / relative_header
            final_header = output_root / relative_header
            for header, payload in (
                (source_header, b"source"),
                (world_header, b"world-patched"),
                (final_header, b"combined-material-patched"),
            ):
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_bytes(payload)
                header.with_suffix(".uexp").write_bytes(payload + b"-export")
            world_result = WorldLootGenerationResult(
                1, 1,
                (WorldLootPackagePatchResult(source_header, world_header, ()),),
                (world_header, world_header.with_suffix(".uexp")),
            )
            material_result = MaterialGenerationResult(
                1, 1, 0, (), (final_header, final_header.with_suffix(".uexp")),
            )

            overrides = _world_package_overrides(
                world_result,
                source_content_root=source_root,
                intermediate_content_root=intermediate_root,
                work_root=root,
            )
            _copy_world_outputs(
                world_result,
                intermediate_content_root=intermediate_root,
                output_content_root=output_root,
                material_result=material_result,
            )
            combined = _remap_world_result(
                world_result,
                source_content_root=source_root,
                intermediate_content_root=intermediate_root,
                output_content_root=output_root,
            )

            self.assertEqual(overrides, {"/game/map/generated/map001": world_header})
            self.assertEqual(final_header.read_bytes(), b"combined-material-patched")
            self.assertEqual(combined.output_packages, (final_header, final_header.with_suffix(".uexp")))
            self.assertEqual(combined.package_results[0].output_path, final_header)


if __name__ == "__main__":
    unittest.main()
