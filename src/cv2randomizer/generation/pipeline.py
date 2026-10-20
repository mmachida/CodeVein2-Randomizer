"""All-or-nothing orchestration through validated staging outputs."""

from __future__ import annotations

import csv
import hashlib
import inspect
import json
import logging
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from cv2randomizer import APP_NAME, __version__
from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.core.determinism import RANDOMIZER_ALGORITHM_VERSION
from cv2randomizer.core.extract_diagnostics import ExtractMemoryDiagnostics
from cv2randomizer.core.memory_budget import MemoryBudgetScheduler
from cv2randomizer.core.seed import NormalizedSeed, normalize_seed
from cv2randomizer.core.workspace import (
    CacheManager,
    GenerationCleanupReport,
    GenerationWorkspace,
    WorkspaceManager,
)
from cv2randomizer.generation.spoiler import (
    SpoilerEntry,
    item_label,
    spoiler_assignment_summary,
    write_spoiler,
)
from cv2randomizer.generation.reproducibility import (
    ReproducibilityMetadata,
    build_reproducibility_metadata,
    canonical_seed_text,
    write_assignment_manifest,
)
from cv2randomizer.game.item_display_names import spoiler_item_category
from cv2randomizer.game.asset_extraction import AssetExtractionError, IoStoreAssetExtractor
from cv2randomizer.game.game_data_cache import (
    CachedAssetExtractor,
    GameDataCacheSnapshot,
    GameDataCacheState,
    GameDataCacheStore,
    _link_raw_cache_assets,
)
from cv2randomizer.game.dependency_requests import (
    DependencyRequestRegistry,
    find_missing_required_packages,
)
from cv2randomizer.game.content_availability import (
    ContentAvailability,
    PackageInventory,
    package_path_from_object_path,
    resolve_content_availability,
)
from cv2randomizer.game.article_reward_catalog import (
    CanonicalArticleRewardCatalog,
    CanonicalArticleRewardCatalogError,
    CanonicalArticleRewardCatalogStore,
)
from cv2randomizer.game.catalogs import (
    CatalogError, CatalogStore, RewardPayload, RuntimeCatalogs, WorldLootLocation,
    is_world_map_package,
)
from cv2randomizer.game.renewable_ingredients import renewable_material_locations
from cv2randomizer.game.blood_code_catalog import (
    BloodCodeCatalog, BloodCodeCatalogError, BloodCodeCatalogStore,
    SCENARIO_SCHEMA_PACKAGES,
)
from cv2randomizer.game.blood_code_audit import blood_code_coverage, installed_blood_code_articles
from cv2randomizer.game.blood_code_generation import (
    BloodCodeGenerationError,
    BloodCodeGenerationResult,
    BloodCodePreparation,
    BloodCodeSerializer,
    apply_blood_code_plan,
    prepare_blood_code_plan,
    required_blood_code_package_paths,
)
from cv2randomizer.game.blood_code_mastery_generation import (
    LOW_BLOOD_CODE_MASTERY_APPROPRIATE_LEVEL,
    BloodCodeMasteryError,
    BloodCodeMasteryResult,
    BloodCodeMasterySerializer,
    apply_blood_code_mastery,
    blood_code_mastery_packages,
    inventory_for_blood_code_mastery,
)
from cv2randomizer.game.bequeathed_formae_catalog import (
    BequeathedFormaCatalog,
    BequeathedFormaCatalogError,
    BequeathedFormaCatalogStore,
)
from cv2randomizer.game.bequeathed_formae_generation import (
    BequeathedFormaGenerationError,
    BequeathedFormaGenerationResult,
    BequeathedFormaPreparation,
    apply_bequeathed_formae_plan,
    prepare_bequeathed_formae_plan,
)
from cv2randomizer.game.booster_catalog import BoosterCatalog, BoosterCatalogError, BoosterCatalogStore
from cv2randomizer.game.booster_catalog import BOOSTER_ROW_STRUCT_PACKAGE, BOOSTER_TABLE_PACKAGE
from cv2randomizer.game.booster_slot_unlock_generation import (
    BOOSTER_SLOT_FLAGS_PACKAGE,
    BoosterSlotUnlockError,
    BoosterSlotUnlockResult,
    BoosterSlotUnlockSerializer,
    apply_booster_slot_unlocks,
    inventory_for_booster_slot_flags,
)
from cv2randomizer.game.tutorial_disable_generation import (
    BASE_TUTORIAL_FLAG_TABLE,
    TUTORIAL_FLAG_TABLES,
    TutorialDisableError,
    TutorialDisableResult,
    TutorialDisableSerializer,
    apply_tutorial_disable,
    inventory_for_tutorial_flags,
)
from cv2randomizer.game.dialogue_skip_generation import (
    DialogueSkipResult,
    DialogueSkipSerializer,
    apply_tap_to_skip_dialogue,
)
from cv2randomizer.game.motorcycle_unlock_generation import (
    MOTORCYCLE_CACHE_PACKAGES,
    MOTORCYCLE_FORMA_ARTICLE_PACKAGE,
    MOTORCYCLE_UNLOCK_PACKAGES,
    MotorcycleUnlockError,
    MotorcycleUnlockResult,
    MotorcycleUnlockSerializer,
    apply_motorcycle_unlock,
    inventory_for_motorcycle_unlock_packages,
)
from cv2randomizer.game.version_compatibility import is_game_data_layout_mismatch
from cv2randomizer.game.booster_reward_pool import build_extractor_booster_pool
from cv2randomizer.game.booster_generation import (
    BoosterGenerationError,
    BoosterGenerationResult,
    BoosterPreparation,
    BoosterSerializer,
    apply_booster_plan,
    prepare_booster_plan,
)
from cv2randomizer.game.defensive_gadget_catalog import (
    DefensiveGadgetCatalog,
    DefensiveGadgetCatalogError,
    DefensiveGadgetCatalogStore,
)
from cv2randomizer.game.defensive_gadget_generation import (
    DefensiveGadgetGenerationError,
    DefensiveGadgetGenerationResult,
    DefensiveGadgetPreparation,
    DefensiveGadgetSerializer,
    apply_defensive_gadget_npc_plan,
    prepare_defensive_gadget_npc_plan,
)
from cv2randomizer.game.field_enemy_catalog import (
    FieldEnemyCatalog,
    FieldEnemyCatalogError,
    FieldEnemyCatalogStore,
)
from cv2randomizer.game.extractor_catalog import ExtractorCatalog, ExtractorCatalogError, ExtractorCatalogStore
from cv2randomizer.game.extractor_generation import (
    ExtractorGenerationError,
    ExtractorGenerationResult,
    ExtractorPreparation,
    ExtractorSerializer,
    apply_extractor_plan,
    prepare_extractor_plan,
)
from cv2randomizer.game.initial_inventory_generation import (
    InitialInventoryGenerationError,
    InitialInventoryGenerationResult,
    InitialInventoryPreparation,
    InitialInventorySerializer,
    apply_initial_inventory_plan,
    prepare_initial_inventory,
)
from cv2randomizer.game.jail_catalog import JailCatalog, JailCatalogError, JailCatalogStore
from cv2randomizer.game.material_dependencies import MaterialDependencyResolver
from cv2randomizer.game.material_generation import (
    MaterialGenerationError,
    MaterialGenerationResult,
    MaterialPlanPreparation,
    apply_material_plan,
    prepare_material_plan,
)
from cv2randomizer.game.material_serialization import MaterialSerializer
from cv2randomizer.game.enemy_drop_catalog import EnemyDropCatalog, EnemyDropCatalogError, EnemyDropCatalogStore
from cv2randomizer.game.enemy_drop_generation import ENEMY_DROP_TABLES
from cv2randomizer.game.enemy_drop_generation import (
    EnemyDropGenerationError,
    EnemyDropGenerationResult,
    EnemyDropSerializer,
    apply_enemy_drop_plan,
    prepare_enemy_drop_plan,
)
from cv2randomizer.game.shop_generation import (
    ShopGenerationError,
    ShopGenerationResult,
    ShopTablesStage,
    apply_shop_plan,
    prepare_shop_tables,
)
from cv2randomizer.game.versioning import GameVersionDetector, VersionDetection
from cv2randomizer.game.world_loot_dependencies import WorldLootDependencyResolver
from cv2randomizer.game.world_loot_compatibility import apply_world_loot_export_rebindings
from cv2randomizer.game.world_loot_generation import (
    WorldLootGenerationResult,
    apply_world_loot_plan,
)
from cv2randomizer.game.world_loot_serialization import WorldLootSerializer
from cv2randomizer.game.raw_article_grant_generation import (
    RawArticleGrantGenerationError,
    RawArticleGrantSerializer,
    WorldLootDirectGrantResult,
    apply_world_loot_direct_grants,
    filter_compatible_raw_article_grants,
    filter_compatible_world_loot_quest_grants,
)
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES
from cv2randomizer.game.fname_identity import FNameIdentity
from cv2randomizer.game.shop_tables import ShopTableProduct
from cv2randomizer.packaging.installer import InstallManager, InstallationResult
from cv2randomizer.packaging.retoc import PackageResult, RetocPackager
from cv2randomizer.randomizer.materials import MaterialAssignment, MaterialRandomizationPlan
from cv2randomizer.randomizer.enemy_drops import EnemyDropPlan
from cv2randomizer.randomizer.blood_codes import BloodCodePlan
from cv2randomizer.randomizer.boosters import BoosterPlan
from cv2randomizer.randomizer.booster_rewards import exchange_extractor_world_sources
from cv2randomizer.randomizer.extractor import ExtractorPlan
from cv2randomizer.randomizer.initial_inventory import (
    INITIAL_INVENTORY_PACKAGE,
    InitialInventoryPlan,
    build_initial_inventory_plan,
)
from cv2randomizer.game.initial_inventory_catalog import build_available_weapon_pool
from cv2randomizer.randomizer.reward_reservations import RewardReservationSet
from cv2randomizer.randomizer.source_occurrences import (
    exchange_planned_reward_occurrence,
    planned_reward_source_occurrences,
)
from cv2randomizer.randomizer.bequeathed_formae import BequeathedFormaPlan
from cv2randomizer.randomizer.defensive_gadgets import (
    DefensiveGadgetNpcPlan,
    defensive_gadget_world_candidates,
)
from cv2randomizer.randomizer.jails import jail_world_candidates
from cv2randomizer.randomizer.linked_jail_rewards import (
    OGRE_ARTICLE,
    OGRE_SHOP_FAMILY,
    STINGER_ARTICLE,
    STINGER_SHOP_FAMILY,
    STINGER_WORLD_EXPORT_NAME,
    STINGER_WORLD_PACKAGE,
    stinger_route_unavailable_reason,
    build_ogre_shop_mirror_assignment,
    build_stinger_shop_mirror_assignment,
    exclude_linked_jail_shop_routes,
    find_ogre_shop_product,
    find_stinger_shop_product,
    validate_jail_shop_routes,
)
from cv2randomizer.randomizer.article_rewards import canonical_article_world_candidates
from cv2randomizer.randomizer.shops import (
    ArticleRewardIdentity,
    NpcTraderPlanSummary,
    ShopAssignment,
    ShopRandomizationPlan,
    build_npc_trader_only_plan,
    build_global_article_mix_plans,
    build_shop_randomization_plan,
    combine_shop_randomization_plans,
    is_npc_trader_row,
    shop_slot_lock_reason,
)
from cv2randomizer.randomizer.world_loot import (
    WorldLootRewardCandidate,
    WorldLootRandomizationPlan,
    build_world_loot_randomization_plan,
    filter_enemy_reward_world_locations,
    world_loot_slot,
)


Progress = Callable[[str, int, int], None]
LOGGER = logging.getLogger(__name__)
_WORLD_SPOILER_CATEGORY_BY_CANDIDATE = {
    "booster": "Boosters",
    "defensive_gadget": "Defensive Gadgets",
    "jail": "Jails",
}
class GenerationError(RuntimeError):
    """Raised when generation cannot safely reach validated staging."""


def _generation_error_cause(error: BaseException) -> GenerationError | None:
    """Find cancellation or generation failures wrapped by an optional patch."""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, GenerationError):
            return current
        current = current.__cause__
    return None


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """User-selected inputs needed for one local generation run."""

    game_root: Path
    mappings_path: Path | None
    seed: str | int | None
    options: GenerationOptions
    dry_run: bool = False
    game_data_cache_entry: Path | None = None
    replace_existing_randomizer: bool = False


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Reproducible assignment summary and validated staging tree."""

    workspace: GenerationWorkspace
    seed: NormalizedSeed
    game_version: str
    catalog_revision: str
    dry_run: bool
    world_plan: WorldLootRandomizationPlan
    shop_plan: ShopRandomizationPlan
    material_preparation: MaterialPlanPreparation | None
    world_result: WorldLootGenerationResult | None
    shop_result: ShopGenerationResult | None
    material_result: MaterialGenerationResult | None
    output_content_root: Path
    output_files: tuple[Path, ...]
    manifest_path: Path
    spoiler_path: Path | None
    package_result: PackageResult | None = None
    installation_result: InstallationResult | None = None
    enemy_drop_plan: EnemyDropPlan | None = None
    enemy_drop_result: EnemyDropGenerationResult | None = None
    blood_code_plan: BloodCodePlan | None = None
    blood_code_result: BloodCodeGenerationResult | None = None
    booster_plan: BoosterPlan | None = None
    booster_result: BoosterGenerationResult | None = None
    defensive_gadget_plan: DefensiveGadgetNpcPlan | None = None
    defensive_gadget_result: DefensiveGadgetGenerationResult | None = None
    bequeathed_formae_plan: BequeathedFormaPlan | None = None
    bequeathed_formae_result: BequeathedFormaGenerationResult | None = None
    extractor_plan: ExtractorPlan | None = None
    extractor_result: ExtractorGenerationResult | None = None
    initial_inventory_plan: InitialInventoryPlan | None = None
    initial_inventory_result: InitialInventoryGenerationResult | None = None
    motorcycle_unlock_result: MotorcycleUnlockResult | None = None
    tutorial_disable_result: TutorialDisableResult | None = None
    dialogue_skip_result: DialogueSkipResult | None = None
    blood_code_mastery_result: BloodCodeMasteryResult | None = None
    world_direct_grant_results: tuple[WorldLootDirectGrantResult, ...] = ()
    content_availability: ContentAvailability | None = None
    availability_warnings: tuple[str, ...] = ()
    spoiler_text_path: Path | None = None
    cleanup_report: GenerationCleanupReport | None = None
    assignment_manifest_path: Path | None = None
    config_fingerprint: str | None = None
    content_fingerprint: str | None = None
    assignment_manifest_sha256: str | None = None


class GenerationEngine:
    """Validate, plan, stage, patch, package, and optionally install a generation."""

    # Initialize this object
    def __init__(
        self,
        *,
        version_detector: GameVersionDetector | None = None,
        allow_local_unverified: bool = False,
        catalog_store: CatalogStore | None = None,
        workspace_manager: WorkspaceManager | None = None,
        extractor: IoStoreAssetExtractor | None = None,
        world_loot_resolver: WorldLootDependencyResolver | None = None,
        world_loot_serializer: WorldLootSerializer | None = None,
        material_resolver: MaterialDependencyResolver | None = None,
        material_serializer: MaterialSerializer | None = None,
        enemy_drop_catalog_store: EnemyDropCatalogStore | None = None,
        enemy_drop_serializer: EnemyDropSerializer | None = None,
        blood_code_catalog_store: BloodCodeCatalogStore | None = None,
        blood_code_serializer: BloodCodeSerializer | None = None,
        booster_catalog_store: BoosterCatalogStore | None = None,
        booster_serializer: BoosterSerializer | None = None,
        booster_slot_unlock_serializer: BoosterSlotUnlockSerializer | None = None,
        tutorial_disable_serializer: TutorialDisableSerializer | None = None,
        dialogue_skip_serializer: DialogueSkipSerializer | None = None,
        blood_code_mastery_serializer: BloodCodeMasterySerializer | None = None,
        motorcycle_unlock_serializer: MotorcycleUnlockSerializer | None = None,
        extractor_catalog_store: ExtractorCatalogStore | None = None,
        extractor_serializer: ExtractorSerializer | None = None,
        initial_inventory_serializer: InitialInventorySerializer | None = None,
        defensive_gadget_catalog_store: DefensiveGadgetCatalogStore | None = None,
        defensive_gadget_serializer: DefensiveGadgetSerializer | None = None,
        jail_catalog_store: JailCatalogStore | None = None,
        field_enemy_catalog_store: FieldEnemyCatalogStore | None = None,
        bequeathed_formae_catalog_store: BequeathedFormaCatalogStore | None = None,
        bequeathed_formae_serializer: BloodCodeSerializer | None = None,
        raw_article_grant_serializer: RawArticleGrantSerializer | None = None,
        world_quest_article_serializer: BloodCodeSerializer | None = None,
        canonical_article_reward_catalog_store: CanonicalArticleRewardCatalogStore | None = None,
        game_data_cache_store: GameDataCacheStore | None = None,
        staging_extractor_factory: Callable[[CacheManager], IoStoreAssetExtractor] | None = None,
        packager: RetocPackager | None = None,
        installer: InstallManager | None = None,
    ) -> None:
        self.version_detector = version_detector or GameVersionDetector(
            allow_local_unverified=allow_local_unverified,
        )
        self.catalog_store = catalog_store or CatalogStore()
        self.workspace_manager = workspace_manager or WorkspaceManager()
        self.extractor = extractor or IoStoreAssetExtractor()
        self.game_data_cache_store = game_data_cache_store or GameDataCacheStore()
        self.staging_extractor_factory = staging_extractor_factory
        self.world_loot_resolver = world_loot_resolver or WorldLootDependencyResolver(extractor=self.extractor)
        self.world_loot_serializer = world_loot_serializer or WorldLootSerializer()
        self.material_resolver = material_resolver or MaterialDependencyResolver(extractor=self.extractor)
        self.material_serializer = material_serializer or MaterialSerializer()
        self.enemy_drop_catalog_store = enemy_drop_catalog_store or EnemyDropCatalogStore()
        self.enemy_drop_serializer = enemy_drop_serializer or EnemyDropSerializer()
        self.blood_code_catalog_store = blood_code_catalog_store or BloodCodeCatalogStore()
        self.blood_code_serializer = blood_code_serializer or BloodCodeSerializer()
        self.world_quest_article_serializer = world_quest_article_serializer or self.blood_code_serializer
        self.booster_catalog_store = booster_catalog_store or BoosterCatalogStore()
        self.booster_serializer = booster_serializer or BoosterSerializer()
        self.booster_slot_unlock_serializer = booster_slot_unlock_serializer or BoosterSlotUnlockSerializer()
        self.tutorial_disable_serializer = tutorial_disable_serializer or TutorialDisableSerializer()
        self.dialogue_skip_serializer = dialogue_skip_serializer or DialogueSkipSerializer()
        self.blood_code_mastery_serializer = blood_code_mastery_serializer or BloodCodeMasterySerializer()
        self.motorcycle_unlock_serializer = motorcycle_unlock_serializer or MotorcycleUnlockSerializer()
        self.extractor_catalog_store = extractor_catalog_store or ExtractorCatalogStore()
        self.extractor_serializer = extractor_serializer or ExtractorSerializer()
        self.initial_inventory_serializer = initial_inventory_serializer or InitialInventorySerializer()
        self.defensive_gadget_catalog_store = defensive_gadget_catalog_store or DefensiveGadgetCatalogStore()
        self.defensive_gadget_serializer = defensive_gadget_serializer or DefensiveGadgetSerializer()
        self.jail_catalog_store = jail_catalog_store or JailCatalogStore()
        self.field_enemy_catalog_store = field_enemy_catalog_store or FieldEnemyCatalogStore()
        self.canonical_article_reward_catalog_store = canonical_article_reward_catalog_store or CanonicalArticleRewardCatalogStore()
        self.bequeathed_formae_catalog_store = bequeathed_formae_catalog_store or BequeathedFormaCatalogStore()
        self.bequeathed_formae_serializer = bequeathed_formae_serializer or self.blood_code_serializer
        self.raw_article_grant_serializer = raw_article_grant_serializer or RawArticleGrantSerializer()
        self.packager = packager or RetocPackager()
        self.installer = installer or InstallManager()

    # Extract game data
    def extract_game_data(
        self,
        game_root: Path,
        mappings_path: Path,
        *,
        progress: Progress | None = None,
        reuse_cached_raw: bool = False,
    ) -> GameDataCacheSnapshot:
        """Run transactional extraction with process-level RAM diagnostics."""
        scheduler = MemoryBudgetScheduler()
        diagnostics = ExtractMemoryDiagnostics(
            self.game_data_cache_store.root / "diagnostics",
            scheduler,
        )
        diagnostics.start()

        # Read tracked progress
        def tracked_progress(stage: str, current: int, total: int) -> None:
            diagnostics.set_phase(stage, current, total)
            if progress is not None:
                progress(stage, current, total)

        self._attach_extraction_telemetry(self.extractor, scheduler, diagnostics)
        try:
            snapshot = self._extract_game_data_impl(
                game_root,
                mappings_path,
                progress=tracked_progress,
                memory_scheduler=scheduler,
                memory_diagnostics=diagnostics,
                reuse_cached_raw=reuse_cached_raw,
            )
        except Exception as error:
            diagnostics.finish(success=False, error=f"{type(error).__name__}: {error}")
            raise
        else:
            if not snapshot.ready:
                diagnostics.finish(success=False, error=snapshot.reason)
            else:
                diagnostics.finish(success=True)
            return snapshot
        finally:
            self._attach_extraction_telemetry(self.extractor, None, None)

    # Extract game data impl
    def _extract_game_data_impl(
        self,
        game_root: Path,
        mappings_path: Path,
        *,
        progress: Progress | None,
        memory_scheduler: MemoryBudgetScheduler,
        memory_diagnostics: ExtractMemoryDiagnostics,
        reuse_cached_raw: bool = False,
    ) -> GameDataCacheSnapshot:
        """Extract and validate every catalogued source into a replaceable vanilla cache."""
        if not isinstance(game_root, Path) or not isinstance(mappings_path, Path):
            raise GenerationError("Game path and CV2.usmap must be filesystem paths.")
        try:
            selected_game = game_root.expanduser().resolve(strict=True)
            mappings = mappings_path.expanduser().resolve(strict=True)
        except OSError as error:
            raise GenerationError(f"Could not access the game or CV2.usmap file: {error}") from error
        if not mappings.is_file():
            raise GenerationError("A matching CV2.usmap file is required to validate extracted game data.")
        detection = self.version_detector.detect(selected_game)
        if not detection.usable_for_local_validation or not detection.version_id or not detection.catalog_revision:
            raise GenerationError(f"Extraction is blocked by game-version validation: {detection.reason}")
        reusable_raw_snapshot: GameDataCacheSnapshot | None = None
        if reuse_cached_raw:
            reusable_raw_snapshot = self.game_data_cache_store.inspect(selected_game, detection)
            if (
                reusable_raw_snapshot.state is not GameDataCacheState.PREPARED_OUTDATED
                or reusable_raw_snapshot.entry_path is None
                or reusable_raw_snapshot.metadata is None
            ):
                raise GenerationError(
                    "Prepared-only cache rebuild requires valid raw assets from the same installed game: "
                    + reusable_raw_snapshot.reason
                )
            cached_package_inventory = reusable_raw_snapshot.metadata.get("extracted_package_inventory")
            if not isinstance(cached_package_inventory, list) or not cached_package_inventory:
                raise GenerationError("The existing raw cache has no package inventory to reuse safely.")
        if progress:
            progress("reading-game-archives", 0, 1)
        inspect_inventory = getattr(self.extractor, "inspect_package_inventory", None)
        if not callable(inspect_inventory):
            raise GenerationError("The asset extractor cannot inspect source packages before extraction.")
        package_index_started = time.perf_counter()
        try:
            installed_inventory: PackageInventory = inspect_inventory(selected_game)
        except (OSError, RuntimeError, ValueError) as error:
            raise GenerationError(f"Could not inspect installed source-package inventory: {error}") from error
        memory_diagnostics.record_stage(
            "installed_package_indexing",
            time.perf_counter() - package_index_started,
            containers=len(installed_inventory.container_names),
            unique_packages=len(installed_inventory.package_paths),
        )
        missing_core = tuple(
            package for package in detection.required_asset_paths
            if not installed_inventory.contains(package)
        )
        if missing_core:
            raise GenerationError(
                "The supported installation is missing required core source packages: "
                + ", ".join(missing_core)
            )
        if progress:
            progress("reading-game-archives", 1, 1)
            progress("preparing-source-registry", 0, 1)
        catalog_started = time.perf_counter()
        try:
            catalogs = self.catalog_store.load(detection.catalog_revision)
            enemy_drops = self.enemy_drop_catalog_store.load(detection.catalog_revision)
            blood_codes = self.blood_code_catalog_store.load(detection.catalog_revision)
            boosters = self.booster_catalog_store.load(detection.catalog_revision)
            extractor_catalog = self.extractor_catalog_store.load(detection.catalog_revision)
            gadgets = self.defensive_gadget_catalog_store.load(detection.catalog_revision)
            jails = self.jail_catalog_store.load()
            jails.validate_world_sources(catalogs.world_loot)
            field_enemy = self.field_enemy_catalog_store.load(
                detection.catalog_revision,
                important_item_exclusions=catalogs.important_item_exclusions,
            )
            canonical_articles = self.canonical_article_reward_catalog_store.load(detection.catalog_revision)
            formae = self.bequeathed_formae_catalog_store.load()
            formae.validate_sources(catalogs.world_loot, catalogs.shops)
        except (CatalogError, OSError, ValueError) as error:
            raise GenerationError(f"Could not build the supported source registry: {error}") from error
        source_registry = _known_source_packages(
            catalogs,
            enemy_drop_catalog=enemy_drops,
            blood_code_catalog=blood_codes,
            booster_catalog=boosters,
            extractor_catalog=extractor_catalog,
            defensive_gadget_catalog=gadgets,
            jail_catalog=jails,
            field_enemy_catalog=field_enemy,
            canonical_article_catalog=canonical_articles,
            bequeathed_formae_catalog=formae,
            initial_inventory_enabled=True,
            include_booster_slot_flags=True,
            include_motorcycle_unlock_packages=True,
            include_tutorial_flag_tables=True,
        )
        source_registry["blood_code_installed_articles"] = tuple(
            package for package in installed_blood_code_articles(installed_inventory) if "/Debug/" not in package
        )
        required_sources = tuple(sorted(
            set(detection.required_asset_paths),
            key=str.casefold,
        ))
        world_cdo_sources = tuple(sorted({
            package
            for location in catalogs.world_loot
            if location.destination_kind == "BLUEPRINT_CDO_ARTICLE_BULK"
            for package in (location.package_path, *location.parent_blueprint_packages)
        } | {
            package
            for location in catalogs.world_loot
            if location.instance_override_source is not None
            for package in (
                location.instance_override_source.cdo_package,
                *location.instance_override_source.parent_blueprint_packages,
            )
        }, key=str.casefold))
        request_collection_started = time.perf_counter()
        dependency_registry = DependencyRequestRegistry()
        for system, packages in source_registry.items():
            if system == "shops":
                continue
            dependency_registry.add_many(
                packages,
                system=system,
                reason=f"Runtime source catalog: {system}",
            )
        for product in catalogs.shops:
            npc_trader = product.shop_row.casefold().startswith("character.type.buddy.")
            dependency_registry.add(
                product.original_product_path,
                system="npc_traders" if npc_trader else "shops",
                reason=f"Finite source product: {product.family} / {product.shop_row}",
            )
        dependency_registry.add_many(
            (f"/Game/Blueprint/GameSystem/Shop/{table}" for table in SHOP_TABLE_NAMES.values()),
            system="shops",
            reason="Shop DataTable source",
        )
        dependency_registry.add_many(
            required_sources,
            system="required_core",
            reason="Game and base Blood Code core sources",
        )
        request_collection_elapsed = time.perf_counter() - request_collection_started
        required_keys = {
            dependency.canonical_package_path
            for dependency in dependency_registry.requests
            if "required_core" in dependency.requested_by_systems
        }
        # Resolve each global request against the Retoc inventory exactly once.
        # Subsequent extraction receives the already case-preserved package
        # spelling returned here, rather than asking every catalog group to
        # resolve the same logical source again.
        resolved_sources: dict[str, str] = {}
        missing_required: list[str] = []
        path_resolution_started = time.perf_counter()
        for dependency in dependency_registry.requests:
            resolved, variants = installed_inventory.resolve_package_path_spelling(
                dependency.package_path,
            )
            if resolved is None:
                if dependency.canonical_package_path in required_keys:
                    detail = f" ({', '.join(variants)})" if variants else ""
                    missing_required.append(dependency.package_path + detail)
                continue
            resolved_sources[dependency.canonical_package_path] = resolved
        path_resolution_elapsed = time.perf_counter() - path_resolution_started
        if missing_required:
            raise GenerationError(
                "A required core source package disappeared during extraction preparation: "
                + ", ".join(sorted(missing_required, key=str.casefold))
            )
        all_sources = dependency_registry.package_paths
        available_to_extract = tuple(sorted(resolved_sources.values(), key=str.casefold))
        resolved_dependency_paths = dict(resolved_sources)
        optional_to_extract = tuple(
            dependency.package_path for dependency in dependency_registry.requests
            if dependency.canonical_package_path in resolved_sources
            and dependency.canonical_package_path not in required_keys
        )
        required_available = tuple(
            dependency.package_path for dependency in dependency_registry.requests
            if dependency.canonical_package_path in resolved_sources
            and dependency.canonical_package_path in required_keys
        )
        request_summary = dependency_registry.summary()
        memory_diagnostics.record_operation(
            "dependency_request_registry",
            phase="catalog_sources",
            collection_elapsed_seconds=request_collection_elapsed,
            **request_summary,
        )
        memory_diagnostics.record_operation(
            "dependency_path_resolution",
            phase="catalog_sources",
            lookup_calls=len(dependency_registry.requests),
            unique_resolved_packages=len(resolved_sources),
            unresolved_optional_packages=(
                len(dependency_registry.requests) - len(resolved_sources) - len(missing_required)
            ),
            elapsed_seconds=path_resolution_elapsed,
        )
        memory_diagnostics.record_stage(
            "catalog_discovery_and_source_plan",
            time.perf_counter() - catalog_started,
            catalog_sources=len(all_sources),
            total_dependency_requests=dependency_registry.request_count,
            duplicate_source_requests_removed=dependency_registry.duplicate_request_count,
            required_packages=len(required_sources),
            optional_packages=len(optional_to_extract),
        )

        final_store = self.game_data_cache_store
        final_store.root.mkdir(parents=True, exist_ok=True)
        staging_root = final_store.root / f".rebuild-{uuid.uuid4().hex}"
        staging_cache = CacheManager(root=staging_root, single_entry=True)
        staging_store = GameDataCacheStore(staging_cache)
        try:
            cache_identity = staging_store.identity(selected_game, detection)
            cache_source_hash = staging_store.cache_source_hash(
                selected_game, detection, identity=cache_identity,
            )
            if progress:
                progress("extracting-game-data", 0, len(available_to_extract))
            if self.staging_extractor_factory is not None:
                staged_extractor = self.staging_extractor_factory(staging_cache)
            else:
                with_cache = getattr(self.extractor, "with_cache", None)
                if not callable(with_cache):
                    raise GenerationError("The configured extractor cannot build an isolated cache replacement.")
                staged_extractor = with_cache(staging_cache)
            self._attach_extraction_telemetry(staged_extractor, memory_scheduler, memory_diagnostics)
            bind_cache_identity = getattr(staged_extractor, "set_cache_source_hash", None)
            if callable(bind_cache_identity):
                bind_cache_identity(cache_source_hash)
            elif self.staging_extractor_factory is None:
                raise GenerationError("The configured extractor cannot bind files to the validated cache identity.")
            if reusable_raw_snapshot is not None:
                raw_reuse_started = time.perf_counter()
                staging_entry = staging_store.cache_entry_path(selected_game, detection, create=True)
                linked_files, linked_bytes = _link_raw_cache_assets(
                    reusable_raw_snapshot.entry_path,
                    staging_entry,
                )
                prime_inventory = getattr(staged_extractor, "prime_cached_asset_inventory", None)
                primed_packages = (
                    prime_inventory(staging_entry, reusable_raw_snapshot.metadata["extracted_package_inventory"])
                    if callable(prime_inventory) else 0
                )
                memory_diagnostics.record_stage(
                    "raw_cache_reuse_and_prepared_only_rebuild",
                    time.perf_counter() - raw_reuse_started,
                    linked_files=linked_files,
                    reused_bytes=linked_bytes,
                    primed_packages=primed_packages,
                )
            try:
                extraction_parameters = inspect.signature(staged_extractor.extract_assets).parameters
            except (TypeError, ValueError):
                extraction_parameters = {}
            supports_pre_resolved_paths = (
                "_resolved_package_paths" in extraction_parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in extraction_parameters.values()
                )
            )
            resolved_path_kwargs = (
                {"_resolved_package_paths": resolved_dependency_paths}
                if supports_pre_resolved_paths else {}
            )
            extracted: dict[str, Any] = {}
            extraction_started = time.perf_counter()
            if required_available:
                required_assets = staged_extractor.extract_assets(
                    selected_game,
                    required_available,
                    **resolved_path_kwargs,
                    game_version=detection.version_id,
                    data_revision=detection.catalog_revision,
                    progress=progress,
                )
                extracted.update({asset.virtual_path.casefold(): asset for asset in required_assets})
            # Validate the core source set immediately. If Retoc ever returns a
            # different package identity than requested, stop before spending
            # several more minutes extracting optional catalogs and shop
            # dependencies; the staged cache will be discarded either way.
            missing_required = find_missing_required_packages(
                required_sources,
                resolved_sources,
                lambda package: installed_inventory.resolve_package_path(package)[0],
                extracted,
            )
            memory_diagnostics.record_operation(
                "required_source_validation",
                required_sources=len(required_sources),
                extracted_packages=len(extracted),
                missing_packages=len(missing_required),
                missing_samples=list(missing_required[:20]),
            )
            if missing_required:
                raise GenerationError(
                    "Required core source packages could not be extracted: " + ", ".join(missing_required)
                )
            if optional_to_extract:
                optional_assets = staged_extractor.extract_assets(
                    selected_game,
                    optional_to_extract,
                    optional_virtual_paths=optional_to_extract,
                    **resolved_path_kwargs,
                    game_version=detection.version_id,
                    data_revision=detection.catalog_revision,
                    progress=progress,
                )
                extracted.update({asset.virtual_path.casefold(): asset for asset in optional_assets})
            # Catalogs may contain only the literal NameMap string. Read the
            # table's FName Number before deciding which Article packages exist.
            installed_shop_families = tuple(
                family for family, table in SHOP_TABLE_NAMES.items()
                if installed_inventory.contains(f"/Game/Blueprint/GameSystem/Shop/{table}")
                and any(product.family.upper() == family for product in catalogs.shops)
            )
            if installed_shop_families:
                shop_references = prepare_shop_tables(
                    catalogs, game_root=selected_game,
                    work_root=staging_root / "shop-reference-validation",
                    mappings_path=mappings, game_version=detection.version_id,
                    data_revision=detection.catalog_revision,
                    enabled_families=installed_shop_families,
                    extractor=staged_extractor, progress=progress,
                )
                article_packages = tuple(sorted({
                    product.article_package_path
                    for table in shop_references.tables for product in table.parsed.products
                }, key=str.casefold))
                dependency_registry.add_many(
                    article_packages,
                    system="shops_discovered_articles",
                    reason="Article packages referenced by installed finite shop tables",
                )
                source_registry["shops"] = tuple(sorted({
                    *source_registry.get("shops", ()),
                    *article_packages,
                    *(f"/Game/Blueprint/GameSystem/Shop/{table}" for table in SHOP_TABLE_NAMES.values()),
                }, key=str.casefold))
                missing_articles: list[str] = []
                resolved_article_paths: dict[str, str] = {}
                shop_path_resolution_started = time.perf_counter()
                shop_path_resolution_calls = 0
                for package in article_packages:
                    resolved = resolved_sources.get(package.casefold())
                    if resolved is None:
                        shop_path_resolution_calls += 1
                        resolved, _variants = installed_inventory.resolve_package_path_spelling(package)
                        if resolved is not None:
                            resolved_sources[package.casefold()] = resolved
                    if resolved is not None and resolved.casefold() not in extracted:
                        missing_articles.append(resolved)
                    if resolved is not None:
                        resolved_article_paths[package.casefold()] = resolved
                        resolved_article_paths[resolved.casefold()] = resolved
                memory_diagnostics.record_operation(
                    "dependency_path_resolution",
                    phase="shop_articles",
                    lookup_calls=shop_path_resolution_calls,
                    requested_packages=len(article_packages),
                    elapsed_seconds=time.perf_counter() - shop_path_resolution_started,
                )
                if missing_articles:
                    articles = staged_extractor.extract_assets(
                        selected_game, tuple(sorted(set(missing_articles), key=str.casefold)),
                        **(
                            {"_resolved_package_paths": resolved_article_paths}
                            if supports_pre_resolved_paths else {}
                        ),
                        game_version=detection.version_id, data_revision=detection.catalog_revision,
                        progress=progress,
                    )
                    extracted.update({asset.virtual_path.casefold(): asset for asset in articles})
            memory_diagnostics.record_stage(
                "required_optional_and_shop_article_extraction",
                time.perf_counter() - extraction_started,
                available_sources=len(available_to_extract),
                required_sources=len(required_available),
                optional_sources=len(optional_to_extract),
                final_extracted_packages=len(extracted),
            )
            extracted_keys = set(extracted)
            missing_world_cdo = tuple(
                package for package in world_cdo_sources
                if (resolved := (
                    resolved_sources.get(package.casefold())
                    or installed_inventory.resolve_package_path_spelling(package)[0]
                )) is not None
                if resolved.casefold() not in extracted_keys
            )
            if missing_world_cdo:
                raise GenerationError(
                    "Extract/cache omitted catalogued World Loot Blueprint/CDO dependencies: "
                    + ", ".join(missing_world_cdo)
                )
            entry = staging_store.cache_entry_path(selected_game, detection, create=False)
            if progress:
                progress("validating-game-data", 0, 2)
            dependency_started = time.perf_counter()
            world_loot_dependency_index = self._prefetch_cached_dependencies(
                catalogs=catalogs,
                source_registry=source_registry,
                field_enemy_catalog=field_enemy,
                canonical_article_catalog=canonical_articles,
                extractor_catalog=extractor_catalog,
                defensive_gadget_catalog=gadgets,
                jail_catalog=jails,
                bequeathed_formae_catalog=formae,
                installed_inventory=installed_inventory,
                game_root=selected_game,
                mappings_path=mappings,
                work_root=staging_root / "dependency-validation",
                prepared_cache_root=entry / "prepared-assets",
                game_version=detection.version_id,
                data_revision=detection.catalog_revision,
                display_version=detection.display_version,
                compatibility_profile=detection.world_loot_compatibility_profile,
                extractor=staged_extractor,
                material_locations=renewable_material_locations(catalogs.world_loot),
                progress=progress,
                memory_scheduler=memory_scheduler,
                memory_diagnostics=memory_diagnostics,
            )
            blueprint_closure = (
                world_loot_dependency_index.get("blueprint_packages", [])
                if isinstance(world_loot_dependency_index, dict) else []
            )
            if isinstance(blueprint_closure, list):
                dependency_registry.add_many(
                    (package for package in blueprint_closure if isinstance(package, str)),
                    system="world_loot_blueprint_closure",
                    reason="Transitive generated Blueprint parent dependency",
                )
                source_registry["world_loot_blueprint_closure"] = tuple(sorted(
                    {package for package in blueprint_closure if isinstance(package, str)},
                    key=str.casefold,
                ))
            asset_inventory_reader = getattr(staged_extractor, "cached_asset_inventory", None)
            cache_entry = staging_store.cache_entry_path(selected_game, detection, create=False)
            if callable(asset_inventory_reader):
                cache_assets = asset_inventory_reader(cache_entry)
                for asset in cache_assets:
                    dependency_registry.set_components(
                        asset.virtual_path,
                        (path.suffix.casefold() for path in asset.files),
                    )
            else:
                cache_assets = None
            final_request_summary = dependency_registry.summary()
            memory_diagnostics.record_operation(
                "dependency_request_registry",
                phase="complete_graph",
                **final_request_summary,
                resolved_unique_packages=len(resolved_sources),
                extracted_unique_packages=(len(cache_assets) if cache_assets is not None else None),
            )
            memory_diagnostics.record_operation(
                "dependency_duplicate_requests",
                phase="complete_graph",
                packages=dependency_registry.duplicate_rows(),
            )
            memory_diagnostics.record_stage(
                "static_dependency_and_prepared_cache_generation",
                time.perf_counter() - dependency_started,
                maps_with_world_locations=len(catalogs.world_loot),
                prepared_map_count=(
                    len(world_loot_dependency_index.get("maps", {}))
                    if isinstance(world_loot_dependency_index, dict) else 0
                ),
            )
            validation_started = time.perf_counter()
            metadata = staging_store.build_metadata(
                game_root=selected_game,
                detection=detection,
                source_registry=source_registry,
                required_sources=required_sources,
                world_loot_dependency_index=world_loot_dependency_index,
                extract_memory_summary=memory_diagnostics.summary(),
                extracted_assets=cache_assets,
                identity=cache_identity,
            )
            metadata["blood_code_reward_descriptors"] = blood_code_coverage(blood_codes, installed_inventory)
            staging_store.write_metadata(entry, metadata)
            staging_store.validate_staged_entry(
                entry,
                required_sources,
                trusted_write_hashes=cache_assets is not None,
            )
            memory_diagnostics.record_stage(
                "staged_cache_integrity_validation",
                time.perf_counter() - validation_started,
                required_packages=len(required_sources),
            )
            if progress:
                progress("validating-game-data", 1, 2)

            destination = final_store.cache_entry_path(selected_game, detection, create=False)
            backup = final_store.root / f".previous-{destination.name}-{uuid.uuid4().hex}"
            moved_old = False
            if progress:
                progress("saving-game-data-cache", 0, 1)
            promotion_started = time.perf_counter()
            try:
                if destination.exists():
                    destination.replace(backup)
                    moved_old = True
                entry.replace(destination)
            except Exception:
                if destination.exists():
                    shutil.rmtree(destination, ignore_errors=True)
                if moved_old and backup.exists():
                    backup.replace(destination)
                raise
            snapshot = final_store.inspect(selected_game, detection)
            if not snapshot.ready:
                shutil.rmtree(destination, ignore_errors=True)
                if moved_old and backup.exists():
                    backup.replace(destination)
                raise GenerationError(f"New game-data cache did not validate after promotion: {snapshot.reason}")
            memory_diagnostics.record_stage(
                "atomic_cache_promotion_and_ready_inspection",
                time.perf_counter() - promotion_started,
                promoted_path=str(destination),
            )
            if moved_old:
                shutil.rmtree(backup, ignore_errors=True)
            return snapshot
        except GenerationError:
            raise
        except Exception as error:
            raise GenerationError(f"Could not extract and validate game data: {error}") from error
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)

    # Read attach extraction telemetry
    @staticmethod
    def _attach_extraction_telemetry(
        component: object,
        scheduler: MemoryBudgetScheduler | None,
        diagnostics: ExtractMemoryDiagnostics | None,
    ) -> None:
        configure = getattr(component, "set_extraction_telemetry", None)
        if callable(configure):
            parameters = inspect.signature(configure).parameters
            arguments = [scheduler, diagnostics.set_job if diagnostics is not None else None]
            keywords = {}
            if "profiling_callback" in parameters and diagnostics is not None:
                keywords["profiling_callback"] = diagnostics.record_operation
            configure(*arguments, **keywords)

    # Read prefetch cached dependencies
    def _prefetch_cached_dependencies(
        self,
        *,
        catalogs: RuntimeCatalogs,
        source_registry: dict[str, tuple[str, ...]] | None = None,
        field_enemy_catalog: FieldEnemyCatalog | None = None,
        canonical_article_catalog: CanonicalArticleRewardCatalog | None = None,
        extractor_catalog: ExtractorCatalog | None = None,
        defensive_gadget_catalog: DefensiveGadgetCatalog | None = None,
        jail_catalog: JailCatalog | None = None,
        bequeathed_formae_catalog: BequeathedFormaCatalog | None = None,
        installed_inventory: PackageInventory,
        game_root: Path,
        mappings_path: Path,
        work_root: Path,
        prepared_cache_root: Path | None = None,
        game_version: str,
        data_revision: str,
        compatibility_profile: str | None,
        display_version: str | None = None,
        extractor: IoStoreAssetExtractor,
        material_locations,
        progress: Progress | None,
        memory_scheduler: MemoryBudgetScheduler,
        memory_diagnostics: ExtractMemoryDiagnostics,
    ) -> dict[str, Any] | None:
        """Resolve generated-map Blueprint ancestry now, so Generate remains cache-only."""
        eligible = tuple(
            location for location in catalogs.world_loot
            if installed_inventory.contains(location.package_path)
            and (
                location.destination_kind != "BLUEPRINT_CDO_ARTICLE_BULK"
                or all(installed_inventory.contains(package) for package in location.parent_blueprint_packages)
            )
            and (
                location.instance_override_source is None
                or (
                    installed_inventory.contains(location.instance_override_source.cdo_package)
                    and all(installed_inventory.contains(package)
                            for package in location.instance_override_source.parent_blueprint_packages)
                )
            )
            and (
                location.location_type not in {
                    "RENEWABLE_INGREDIENT", "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
                }
                or (location.structural_class_package is not None
                    and installed_inventory.contains(location.structural_class_package))
            )
        )
        cdo_locations = tuple(
            location for location in eligible
            if location.destination_kind == "BLUEPRINT_CDO_ARTICLE_BULK"
        )
        map_locations = tuple(
            location for location in eligible
            if location.destination_kind in {
                "ARTICLE_BULK", "RENEWABLE_INGREDIENT", "INSTANCE_ARTICLE_BULK_OVERRIDE",
            }
            and is_world_map_package(location.package_path)
            and location.source_file.casefold() not in catalogs.unsafe_packages
        )
        dependency_index = None
        if map_locations:
            candidate_name_values = _world_loot_candidate_fname_values(
                catalogs,
                installed_inventory=installed_inventory,
                field_enemy_catalog=field_enemy_catalog,
                canonical_article_catalog=canonical_article_catalog,
                extractor_catalog=extractor_catalog,
                defensive_gadget_catalog=defensive_gadget_catalog,
                jail_catalog=jail_catalog,
                bequeathed_formae_catalog=bequeathed_formae_catalog,
            )
            world_stage_kwargs = dict(
                game_root=game_root,
                locations=map_locations,
                work_root=work_root / "world-loot",
                mappings_path=mappings_path,
                game_version=game_version,
                data_revision=data_revision,
                display_version=display_version,
                extractor=extractor,
                prepared_cache_root=prepared_cache_root,
                progress=progress,
            )
            stage_signature = inspect.signature(self.world_loot_resolver.stage_for_locations)
            stage_parameters = stage_signature.parameters
            if "prepared_cache_root" not in stage_parameters and not any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in stage_parameters.values()
            ):
                world_stage_kwargs.pop("prepared_cache_root", None)
            if "candidate_name_values" in stage_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in stage_parameters.values()
            ):
                world_stage_kwargs["candidate_name_values"] = candidate_name_values
            if "compatibility_profile" in stage_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in stage_parameters.values()
            ):
                world_stage_kwargs["compatibility_profile"] = compatibility_profile
            if "display_version" in stage_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in stage_parameters.values()
            ):
                world_stage_kwargs["display_version"] = display_version
            if "memory_scheduler" in stage_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in stage_parameters.values()
            ):
                world_stage_kwargs["memory_scheduler"] = memory_scheduler
            if "diagnostics_callback" in stage_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in stage_parameters.values()
            ):
                world_stage_kwargs["diagnostics_callback"] = memory_diagnostics.set_job
            if "profile_callback" in stage_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in stage_parameters.values()
            ):
                world_stage_kwargs["profile_callback"] = memory_diagnostics.record_operation
            if "helper_metrics_callback" in stage_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in stage_parameters.values()
            ):
                world_stage_kwargs["helper_metrics_callback"] = memory_diagnostics.observe_helper
            staged_world = self.world_loot_resolver.stage_for_locations(**world_stage_kwargs)
            dependency_index = staged_world.dependency_index
            if dependency_index is not None:
                catalogs = replace(
                    catalogs,
                    world_loot=apply_world_loot_export_rebindings(catalogs.world_loot, dependency_index),
                )
                eligible = apply_world_loot_export_rebindings(eligible, dependency_index)
                material_locations = renewable_material_locations(catalogs.world_loot)
        if cdo_locations:
            if dependency_index is None:
                dependency_index = {
                    "schema_version": 1,
                    "game_version": game_version,
                    "catalog_revision": data_revision,
                    "maps": {},
                    "blueprint_packages": [],
                }
            dependency_index["blueprint_cdo_defaults"] = {
                location.package_path.casefold(): {
                    "package_path": location.package_path,
                    "export_index": location.export_index,
                    "export_name": location.export_name,
                    "actor_class": location.actor_class,
                    "parent_blueprint_packages": list(location.parent_blueprint_packages),
                    "physical_instance_count": location.physical_instance_count,
                    "rewards": [
                        {
                            "article_path": reward.article_path,
                            "count": reward.count,
                            "notification": reward.notification,
                            "soft_object_path_identity": (
                                {
                                    "package_name": {
                                        "value": reward.soft_object_path_identity.package_name.value,
                                        "number": reward.soft_object_path_identity.package_name.number,
                                    },
                                    "asset_name": {
                                        "value": reward.soft_object_path_identity.asset_name.value,
                                        "number": reward.soft_object_path_identity.asset_name.number,
                                    },
                                }
                                if reward.soft_object_path_identity is not None else None
                            ),
                        }
                        for reward in location.rewards
                    ],
                }
                for location in cdo_locations
            }
        instance_override_locations = tuple(
            location for location in eligible if location.instance_override_source is not None
        )
        if instance_override_locations:
            if dependency_index is None:
                dependency_index = {
                    "schema_version": 1, "game_version": game_version,
                    "catalog_revision": data_revision, "maps": {},
                    "blueprint_packages": [], "blueprint_cdo_defaults": {},
                }
            cdo_defaults = dependency_index.setdefault("blueprint_cdo_defaults", {})
            grouped_overrides: dict[str, list[WorldLootLocation]] = {}
            for location in instance_override_locations:
                source = location.instance_override_source
                assert source is not None
                grouped_overrides.setdefault(source.cdo_package.casefold(), []).append(location)
            for group in grouped_overrides.values():
                first = group[0]
                source = first.instance_override_source
                assert source is not None
                if any(
                    item.instance_override_source is None
                    or item.instance_override_source.cdo_export_index != source.cdo_export_index
                    or item.instance_override_source.cdo_export_name != source.cdo_export_name
                    or item.rewards != first.rewards
                    for item in group
                ):
                    raise GenerationError("Per-instance override actors disagree about their shared CDO source.")
                cdo_defaults[source.cdo_package.casefold()] = {
                    "package_path": source.cdo_package,
                    "export_index": source.cdo_export_index,
                    "export_name": source.cdo_export_name,
                    "actor_class": first.actor_class,
                    "parent_blueprint_packages": list(source.parent_blueprint_packages),
                    "physical_instance_count": len(group),
                    "rewards": [
                        {
                            "article_path": reward.article_path,
                            "count": reward.count,
                            "notification": reward.notification,
                            "soft_object_path_identity": (
                                {
                                    "package_name": {
                                        "value": reward.soft_object_path_identity.package_name.value,
                                        "number": reward.soft_object_path_identity.package_name.number,
                                    },
                                    "asset_name": {
                                        "value": reward.soft_object_path_identity.asset_name.value,
                                        "number": reward.soft_object_path_identity.asset_name.number,
                                    },
                                } if reward.soft_object_path_identity is not None else None
                            ),
                        } for reward in first.rewards
                    ],
                }
        if dependency_index is None:
            dependency_index = {
                "schema_version": 1,
                "game_version": game_version,
                "catalog_revision": data_revision,
                "maps": {},
                "blueprint_packages": [],
                "blueprint_cdo_defaults": {},
            }
        dependency_index["compatibility_profile"] = compatibility_profile
        dependency_index.setdefault("world_loot_export_rebindings", [])
        map_exports = {
            (str(package).casefold(), str(export_index)): class_info
            for package, exports in dependency_index.get("maps", {}).items()
            if isinstance(exports, dict)
            for export_index, class_info in exports.items()
        }
        incompatible_map_packages = _incompatible_world_loot_map_packages(dependency_index)
        incompatible_locations = _incompatible_world_loot_location_keys(dependency_index)
        cdo_descriptors = dependency_index.get("blueprint_cdo_defaults", {})
        location_descriptors: dict[str, dict[str, Any]] = {}
        for location in catalogs.world_loot:
            if (
                location.package_path.casefold() in incompatible_map_packages
                or _world_loot_location_key(location.package_path, location.export_index) in incompatible_locations
            ):
                continue
            descriptor_key = _world_loot_location_key(location.package_path, location.export_index)
            if descriptor_key in location_descriptors:
                raise GenerationError(f"Duplicate prepared World Loot location identity: {descriptor_key}.")
            map_class = map_exports.get((location.package_path.casefold(), str(location.export_index)))
            owner_available = installed_inventory.contains(location.package_path)
            reward_descriptors = []
            for reward_index, reward in enumerate(location.rewards):
                article_package = package_path_from_object_path(reward.article_path)
                reward_descriptors.append({
                    "reward_index": reward_index,
                    "article_path": reward.article_path,
                    "count": reward.count,
                    "notification": reward.notification,
                    "soft_object_path_identity": _world_reward_fname_identity(location, reward_index),
                    "article_package_available": installed_inventory.contains(article_package),
                })
            if location.destination_kind == "ARTICLE_BULK":
                patch_descriptor = {
                    "serializer": "ARTICLE_BULK",
                    "expected_rewards": reward_descriptors,
                }
            elif location.destination_kind == "BLUEPRINT_CDO_ARTICLE_BULK":
                patch_descriptor = {
                    "serializer": "BLUEPRINT_CDO_ARTICLE_BULK",
                    "owner_package": location.package_path,
                    "parent_blueprint_packages": list(location.parent_blueprint_packages),
                    "expected_rewards": reward_descriptors,
                }
            elif location.destination_kind == "INSTANCE_ARTICLE_BULK_OVERRIDE":
                source = location.instance_override_source
                patch_descriptor = {
                    "serializer": location.destination_kind,
                    "instance_override_source": ({
                        "cdo_package": source.cdo_package,
                        "cdo_export_index": source.cdo_export_index,
                        "cdo_export_name": source.cdo_export_name,
                        "class_package": source.class_package,
                        "parent_blueprint_packages": list(source.parent_blueprint_packages),
                    } if source is not None else None),
                    "expected_rewards": reward_descriptors,
                }
            elif location.destination_kind == "RAW_ARTICLE_GRANT":
                patch_descriptor = {
                    "serializer": "RAW_ARTICLE_GRANT",
                    "layout": asdict(location.raw_article_layout) if location.raw_article_layout else None,
                    "expected_rewards": reward_descriptors,
                }
            else:
                patch_descriptor = {
                    "serializer": location.destination_kind,
                    "layout": asdict(location.quest_article_layout) if location.quest_article_layout else None,
                    "layouts": [asdict(layout) for layout in location.quest_grant_layouts]
                    if location.quest_article_layout is None and location.quest_grant_layouts else None,
                    "expected_rewards": reward_descriptors,
                }
            cdo_prepared = (
                location.destination_kind == "BLUEPRINT_CDO_ARTICLE_BULK"
                and location.package_path.casefold() in cdo_descriptors
            )
            map_prepared = (
                location.destination_kind in {
                    "ARTICLE_BULK", "RENEWABLE_INGREDIENT", "INSTANCE_ARTICLE_BULK_OVERRIDE",
                }
                and isinstance(map_class, dict)
                and map_class.get("export_name") == location.export_name
            )
            override_source = location.instance_override_source
            override_cdo_prepared = (
                override_source is not None
                and isinstance(cdo_descriptors, dict)
                and override_source.cdo_package.casefold() in cdo_descriptors
            )
            direct_prepared = (
                location.destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}
                and owner_available
            )
            prepared = bool(
                cdo_prepared or direct_prepared
                or (map_prepared and location.destination_kind == "ARTICLE_BULK")
                or (map_prepared and location.destination_kind == "RENEWABLE_INGREDIENT")
                or (map_prepared and location.destination_kind == "INSTANCE_ARTICLE_BULK_OVERRIDE" and override_cdo_prepared)
            )
            if not owner_available:
                preparation_reason = "destination package is absent from the installed IoStore"
            elif location.source_file.casefold() in catalogs.unsafe_packages:
                preparation_reason = "destination package is marked unsafe/unparsed"
            elif not prepared:
                preparation_reason = "no matching seed-independent parser/patch descriptor was prepared"
            else:
                preparation_reason = "prepared and bound to the current catalog/cache identity"
            location_descriptors[descriptor_key] = {
                "package_path": location.package_path,
                "export_index": location.export_index,
                "mirror_of_package_path": location.mirror_of_package_path,
                "mirror_of_export_index": location.mirror_of_export_index,
                "export_name": location.export_name,
                "actor_class": location.actor_class,
                "actor_category": location.actor_category,
                "destination_kind": location.destination_kind,
                "location_type": location.location_type,
                "structural_class_package": location.structural_class_package,
                "catalogued_effective_reset_on_yadorigi_rest": (
                    True if location.location_type == "RENEWABLE_INGREDIENT" else None
                ),
                "reset_validation_source": (
                    "catalogued_physical_audit" if location.location_type == "RENEWABLE_INGREDIENT" else None
                ),
                "instance_override_source": patch_descriptor.get("instance_override_source"),
                "destination_id": location.destination_id,
                "protected": location.protected,
                "protection_reasons": list(location.protection_reasons),
                "physical_instance_count": location.physical_instance_count,
                "parent_blueprint_packages": list(location.parent_blueprint_packages),
                "package_available": owner_available,
                "blueprint_class_name": map_class.get("class_name") if isinstance(map_class, dict) else None,
                "blueprint_class_package": map_class.get("class_package") if isinstance(map_class, dict) else None,
                "prepared": prepared,
                "preparation_reason": preparation_reason,
                "rewards": reward_descriptors,
                "patch_descriptor": patch_descriptor,
            }
        dependency_index["world_loot_location_descriptors"] = location_descriptors
        available_materials = tuple(
            location for location in material_locations
            if installed_inventory.contains(location.resource_class_package)
        )
        validated_material_classes = {
            location.resource_class_package.casefold() for location in available_materials
        }
        validated_profiles = {}
        if available_materials:
            validate_kwargs = dict(
                game_root=game_root,
                locations=available_materials,
                work_root=work_root / "materials",
                mappings_path=mappings_path,
                game_version=game_version,
                data_revision=data_revision,
                extractor=extractor,
                progress=progress,
            )
            validate_signature = inspect.signature(self.material_resolver.validate_classes)
            validate_parameters = validate_signature.parameters
            if "memory_scheduler" in validate_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in validate_parameters.values()
            ):
                validate_kwargs["memory_scheduler"] = memory_scheduler
            if "diagnostics_callback" in validate_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in validate_parameters.values()
            ):
                validate_kwargs["diagnostics_callback"] = memory_diagnostics.set_job
            validation = self.material_resolver.validate_classes(**validate_kwargs)
            validated_profiles = {
                profile.package_path.casefold(): asdict(profile)
                for profile in validation.profiles
            }
        audited_reset_by_class: dict[str, bool | None] = {}
        incompatible_material_location_rows = dependency_index.setdefault(
            "incompatible_world_loot_locations", [],
        )
        if not isinstance(incompatible_material_location_rows, list):
            raise GenerationError("Extracted World Loot compatibility records are invalid.")
        for location in catalogs.world_loot:
            if location.location_type not in {
                "RENEWABLE_INGREDIENT", "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
            } or location.structural_class_package is None:
                continue
            audited_reset = True if location.location_type == "RENEWABLE_INGREDIENT" else None
            key = location.structural_class_package.casefold()
            if key in audited_reset_by_class and audited_reset_by_class[key] != audited_reset:
                raise GenerationError(
                    f"Structural pickup class has conflicting audited reset behavior: {location.structural_class_package}."
                )
            audited_reset_by_class[key] = audited_reset
        for location in catalogs.world_loot:
            if location.location_type not in {
                "RENEWABLE_INGREDIENT", "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
            }:
                continue
            class_package = location.structural_class_package
            # Structural profiles are prepared only for classes present in the
            # selected install. Missing optional content stays unavailable and
            # is filtered from the runtime pool; it must not invalidate Extract.
            if not class_package or not installed_inventory.contains(class_package):
                continue
            if class_package.casefold() not in validated_material_classes:
                continue
            profile = validated_profiles.get(class_package.casefold()) if class_package else None
            expected_class_name = f"{class_package.rsplit('/', 1)[-1]}_C" if class_package else None
            if (
                not isinstance(profile, dict)
                or profile.get("material_hierarchy") is not True
                or profile.get("class_name") != expected_class_name
                or (location.location_type.startswith("HEALING_UPGRADE_") and profile.get("effective_reset_on_yadorigi_rest") is True)
                or (location.location_type == "RENEWABLE_INGREDIENT" and profile.get("effective_reset_on_yadorigi_rest") is False)
            ):
                reason = (
                    profile.get("reason") if isinstance(profile, dict) else None
                ) or "structural pickup class is incompatible with the audited catalog layout"
                record = next((
                    row for row in incompatible_material_location_rows
                    if isinstance(row, dict)
                    and str(row.get("package_path", "")).casefold() == location.package_path.casefold()
                    and row.get("export_index") == location.export_index
                ), None)
                if record is None:
                    record = {
                        "package_path": location.package_path,
                        "export_index": location.export_index,
                        "reasons": [],
                    }
                    incompatible_material_location_rows.append(record)
                reasons = record.get("reasons")
                if not isinstance(reasons, list):
                    raise GenerationError("Extracted World Loot compatibility reasons are invalid.")
                reasons.append(f"structural pickup class is incompatible: {reason}")
                continue
            profile["catalogued_effective_reset_on_yadorigi_rest"] = audited_reset_by_class.get(class_package.casefold())
            profile["reset_validation_source"] = (
                "runtime_serialized_profile"
                if profile.get("effective_reset_on_yadorigi_rest") is not None
                else "catalogued_physical_audit"
            )
        dependency_index["renewable_class_profiles"] = validated_profiles
        return dependency_index
    # Generate randomized package output
    def generate(
        self,
        request: GenerationRequest,
        *,
        progress: Progress | None = None,
    ) -> GenerationResult:
        """Generate validated files, package them, and optionally install owned containers."""
        generation_started = time.perf_counter()
        performance: dict[str, float] = {}
        if not isinstance(request.options, GenerationOptions):
            raise GenerationError("Generation request must use validated GenerationOptions.")
        if type(request.dry_run) is not bool:
            raise GenerationError("dry_run must be a boolean.")
        if not isinstance(request.game_root, Path):
            raise GenerationError("game_root must be a filesystem path.")
        try:
            game_root = request.game_root.expanduser().resolve()
            detection = self.version_detector.detect(game_root)
        except (OSError, TypeError, ValueError) as error:
            raise GenerationError(f"Could not validate the selected game installation: {error}") from error
        if not detection.usable_for_local_validation or not detection.version_id or not detection.catalog_revision:
            raise GenerationError(f"Generation is blocked by game-version validation: {detection.reason}")
        if detection.local_unverified and request.game_data_cache_entry is None:
            raise GenerationError(
                "Local-unverified generation requires a validated extracted game-data cache."
            )
        cache_snapshot: GameDataCacheSnapshot | None = None
        asset_extractor = self.extractor
        if request.game_data_cache_entry is not None:
            if not isinstance(request.game_data_cache_entry, Path):
                raise GenerationError("game_data_cache_entry must be a filesystem path or None.")
            cache_snapshot = self.game_data_cache_store.inspect(game_root, detection)
            requested_entry = request.game_data_cache_entry.expanduser().resolve()
            if not cache_snapshot.ready or cache_snapshot.entry_path is None:
                raise GenerationError(f"Generation requires valid extracted game data: {cache_snapshot.reason}")
            if requested_entry != cache_snapshot.entry_path.resolve():
                raise GenerationError("The selected vanilla cache does not match the current game installation.")
            try:
                asset_extractor = CachedAssetExtractor(cache_snapshot)
            except (OSError, ValueError) as error:
                raise GenerationError(f"Could not open the persistent vanilla cache: {error}") from error
        inspect_inventory = getattr(asset_extractor, "inspect_package_inventory", None)
        if not callable(inspect_inventory):
            raise GenerationError("The asset extractor cannot inspect source packages before planning.")
        try:
            inventory: PackageInventory = inspect_inventory(game_root)
        except (OSError, RuntimeError, ValueError) as error:
            raise GenerationError(f"Could not inspect installed source-package inventory: {error}") from error
        missing_core = tuple(
            package for package in detection.required_asset_paths
            if not inventory.contains(package)
        )
        if missing_core:
            raise GenerationError(
                "The supported installation is missing required core source packages: "
                + ", ".join(missing_core)
            )
        incompatible_world_map_packages: frozenset[str] = frozenset()
        incompatible_world_location_count = 0
        incompatible_raw_grant_count = 0
        incompatible_quest_grant_count = 0
        version_skip_counts: dict[str, int] = {}
        catalog_world_loot: tuple[WorldLootLocation, ...] = ()
        dependency_index = None
        try:
            catalogs = self.catalog_store.load(detection.catalog_revision)
            if cache_snapshot is not None and cache_snapshot.metadata is not None:
                dependency_index = cache_snapshot.metadata.get("world_loot_dependency_index")
            compatibility_profile = detection.world_loot_compatibility_profile
            cached_profile = (
                dependency_index.get("compatibility_profile")
                if isinstance(dependency_index, dict) else None
            )
            if compatibility_profile is not None and cached_profile != compatibility_profile:
                raise GenerationError(
                    f"The {compatibility_profile} World Loot profile is missing from extracted game data. "
                    "Run Extract Game Data again before generating."
                )
            if compatibility_profile is None and cached_profile is not None:
                raise GenerationError(
                    "Extracted World Loot data belongs to a different game-version profile. "
                    "Run Extract Game Data again before generating."
                )
            if dependency_index is not None:
                catalogs = replace(
                    catalogs,
                    world_loot=apply_world_loot_export_rebindings(catalogs.world_loot, dependency_index),
                )
            catalog_world_loot = catalogs.world_loot
            incompatible_world_map_packages = _incompatible_world_loot_map_packages(dependency_index)
            incompatible_world_location_keys = _incompatible_world_loot_location_keys(dependency_index)
            if incompatible_world_map_packages or incompatible_world_location_keys:
                for location in catalogs.world_loot:
                    if (
                        location.package_path.casefold() not in incompatible_world_map_packages
                        and _world_loot_location_key(location.package_path, location.export_index)
                        not in incompatible_world_location_keys
                    ):
                        continue
                    category = _version_skip_category(location, request.options)
                    if category is not None:
                        _add_version_skip_count(version_skip_counts, category, 1)
                runtime_world_loot = _compatible_world_loot_locations(catalogs.world_loot, dependency_index)
                incompatible_world_location_count = len(catalogs.world_loot) - len(runtime_world_loot)
                catalogs = replace(catalogs, world_loot=runtime_world_loot)
            renewable_locations = renewable_material_locations(catalogs.world_loot)
            catalogs = replace(
                catalogs,
                materials=renewable_locations,
                materials_available=bool(renewable_locations),
                features={**catalogs.features, "materials": bool(renewable_locations)},
            )
            catalogs_for_manifest = catalogs
            catalogs.require_features(
                world_loot=request.options.randomize_world_loot and bool(catalogs.world_loot),
                materials=request.options.randomize_materials and bool(renewable_locations),
                shops=(
                    bool(catalogs.shops)
                    and (
                        request.options.randomize_shops
                        or request.options.randomize_npc_traders
                        or request.options.unlock_npc_trader_shops
                    )
                ),
            )
            prepared_material_profiles = None
            if dependency_index is not None:
                _validate_world_loot_prepared_descriptors(
                    catalogs.world_loot, dependency_index, inventory,
                )
                profiles = dependency_index.get("renewable_class_profiles")
                if isinstance(profiles, dict):
                    prepared_material_profiles = profiles
        except (CatalogError, OSError, ValueError) as error:
            raise GenerationError(f"Catalog validation failed: {error}") from error
        enemy_drop_catalog: EnemyDropCatalog | None = None
        if request.options.randomize_enemy_drops:
            try:
                enemy_drop_catalog = self.enemy_drop_catalog_store.load(detection.catalog_revision)
            except (EnemyDropCatalogError, OSError, ValueError) as error:
                raise GenerationError(f"Enemy Drop catalog validation failed: {error}") from error
        blood_code_catalog: BloodCodeCatalog | None = None
        if request.options.randomize_blood_codes:
            try:
                blood_code_catalog = self.blood_code_catalog_store.load(detection.catalog_revision)
            except (BloodCodeCatalogError, OSError, ValueError) as error:
                raise GenerationError(f"Blood Code catalog validation failed: {error}") from error
        booster_catalog: BoosterCatalog | None = None
        if request.options.randomize_boosters or request.options.randomize_world_loot or request.options.randomize_extractor_recipes:
            try:
                booster_catalog = self.booster_catalog_store.load(detection.catalog_revision)
            except (BoosterCatalogError, OSError, ValueError) as error:
                raise GenerationError(f"Booster catalog validation failed: {error}") from error
        extractor_catalog: ExtractorCatalog | None = None
        if (request.options.randomize_world_loot or request.options.randomize_extractor_recipes
                or request.options.cheaper_booster_extractor):
            try:
                extractor_catalog = self.extractor_catalog_store.load(detection.catalog_revision)
            except (ExtractorCatalogError, OSError, ValueError) as error:
                raise GenerationError(f"Extractor recipe catalog validation failed: {error}") from error
        defensive_gadget_catalog: DefensiveGadgetCatalog | None = None
        if (
            request.options.randomize_world_loot
            or request.options.randomize_defensive_gadget_npc_rewards
            or request.options.randomize_initial_inventory
        ):
            try:
                defensive_gadget_catalog = self.defensive_gadget_catalog_store.load(detection.catalog_revision)
            except (DefensiveGadgetCatalogError, OSError, ValueError) as error:
                raise GenerationError(f"Defensive Gadget catalog validation failed: {error}") from error
        jail_catalog: JailCatalog | None = None
        if request.options.randomize_world_loot or request.options.randomize_initial_inventory:
            try:
                jail_catalog = self.jail_catalog_store.load()
                jail_catalog.validate_world_sources(catalog_world_loot)
            except (JailCatalogError, OSError, ValueError) as error:
                raise GenerationError(f"Jail catalog validation failed: {error}") from error
        field_enemy_catalog: FieldEnemyCatalog | None = None
        if request.options.randomize_world_loot and request.options.randomize_enemy_drops:
            try:
                field_enemy_catalog = self.field_enemy_catalog_store.load(
                    detection.catalog_revision,
                    important_item_exclusions=catalogs.important_item_exclusions,
                )
            except (FieldEnemyCatalogError, OSError, ValueError) as error:
                raise GenerationError(f"FieldEnemy reward catalog validation failed: {error}") from error
        canonical_article_reward_catalog: CanonicalArticleRewardCatalog | None = None
        if request.options.randomize_world_loot or request.options.randomize_extractor_recipes:
            try:
                canonical_article_reward_catalog = self.canonical_article_reward_catalog_store.load(
                    detection.catalog_revision,
                )
            except (CanonicalArticleRewardCatalogError, OSError, ValueError) as error:
                raise GenerationError(f"Canonical Article reward catalog validation failed: {error}") from error
        bequeathed_formae_catalog: BequeathedFormaCatalog | None = None
        if (
            request.options.randomize_world_loot
            or request.options.randomize_shops
            or request.options.randomize_npc_traders
            or request.options.randomize_bequeathed_formae_quest_rewards
        ):
            try:
                bequeathed_formae_catalog = self.bequeathed_formae_catalog_store.load()
                bequeathed_formae_catalog.validate_sources(catalog_world_loot, catalogs.shops)
            except (BequeathedFormaCatalogError, OSError, ValueError) as error:
                raise GenerationError(f"Bequeathed Forma catalog validation failed: {error}") from error

        source_availability = resolve_content_availability(
            _known_source_packages(
                catalogs,
                enemy_drop_catalog=enemy_drop_catalog,
                blood_code_catalog=blood_code_catalog,
                booster_catalog=booster_catalog,
                extractor_catalog=extractor_catalog,
                defensive_gadget_catalog=defensive_gadget_catalog,
                jail_catalog=jail_catalog,
                field_enemy_catalog=field_enemy_catalog,
                canonical_article_catalog=canonical_article_reward_catalog,
                bequeathed_formae_catalog=bequeathed_formae_catalog,
                initial_inventory_enabled=(
                    request.options.randomize_initial_inventory or request.options.motorcycle_unlocked
                ),
                include_motorcycle_unlock_packages=request.options.motorcycle_unlocked,
            ),
            inventory,
        )
        availability_warnings: list[str] = []
        if incompatible_world_map_packages or incompatible_world_location_keys:
            availability_warnings.append(
                f"World Loot left {incompatible_world_location_count} locations unchanged because their exports "
                "could not be matched safely to this game version."
            )
            if request.options.randomize_materials and not catalogs.materials:
                availability_warnings.append(
                    "Ingredients skipped because no compatible renewable material locations remain."
                )

        # Runtime eligibility follows actual source availability. BASE/DLC/
        # AFTER_ENDING/UNKNOWN labels remain descriptive catalog metadata only.
        world_locations_before_inventory = catalogs.world_loot
        world_locations_with_packages = _available_world_locations(world_locations_before_inventory, inventory)
        available_world_keys = {
            _world_loot_location_key(location.package_path, location.export_index)
            for location in world_locations_with_packages
        }
        if request.options.randomize_world_loot:
            for location in world_locations_before_inventory:
                if _world_loot_location_key(location.package_path, location.export_index) in available_world_keys:
                    continue
                category = _version_skip_category(location, request.options)
                if category is not None and category != "Ingredients":
                    _add_version_skip_count(version_skip_counts, category, 1)
        available_world = filter_enemy_reward_world_locations(
            world_locations_with_packages,
            enabled=request.options.randomize_enemy_drops,
        )
        if request.options.randomize_world_loot:
            available_world, incompatible_raw_grants = filter_compatible_raw_article_grants(
                available_world,
                game_root=game_root,
                game_version=detection.version_id,
                data_revision=detection.catalog_revision,
                extractor=asset_extractor,
                compatibility_profile=detection.world_loot_compatibility_profile,
                mappings_path=request.mappings_path,
                serializer=self.raw_article_grant_serializer,
                progress=progress,
            )
            if incompatible_raw_grants:
                incompatible_raw_grant_count = len(incompatible_raw_grants)
                _add_version_skip_count(
                    version_skip_counts, "World Loot", incompatible_raw_grant_count,
                )
                skipped_names = ", ".join(
                    item.display_name or str(item.destination_id)
                    for item in incompatible_raw_grants
                )
                availability_warnings.append(
                    "Direct World Loot rewards left vanilla because their source data could not be "
                    f"verified against the validated layout: {skipped_names}."
                )
        stinger_world_active = request.options.randomize_world_loot and any(
            location.package_path.casefold() == STINGER_WORLD_PACKAGE.casefold()
            and location.export_name.casefold() == STINGER_WORLD_EXPORT_NAME.casefold()
            for location in available_world
        )
        available_materials = (
            _available_material_locations(catalogs.materials, inventory)
            if request.options.randomize_materials
            else ()
        )
        if request.options.randomize_materials:
            available_material_keys = {
                _world_loot_location_key(item.package_path, item.export_index)
                for item in available_materials
            }
            missing_materials = sum(
                _world_loot_location_key(item.package_path, item.export_index) not in available_material_keys
                for item in catalogs.materials
            )
            _add_version_skip_count(version_skip_counts, "Ingredients", missing_materials)
        shop_package_by_family = {
            family: f"/Game/Blueprint/GameSystem/Shop/{table}"
            for family, table in SHOP_TABLE_NAMES.items()
        }
        requested_shop_families = _enabled_shop_families(request.options, catalogs.shops)
        requested_trader_families = _enabled_npc_trader_families(request.options, catalogs.shops)
        unlock_trader_families = tuple(
            family for family in ("TRADE", "TRADE_DLC")
            if request.options.unlock_npc_trader_shops
            and any(
                product.family.upper() == family
                and is_npc_trader_row(product.family, product.shop_row)
                for product in catalogs.shops
            )
        )
        try:
            validate_jail_shop_routes(catalogs.shops)
            stinger_shop_catalog_slot = find_stinger_shop_product(catalogs.shops)
        except ValueError as error:
            raise GenerationError(f"Unique Jail Shop route validation failed: {error}") from error
        stinger_missing_route_reason = (
            stinger_route_unavailable_reason(stinger_shop_catalog_slot, inventory)
            if stinger_world_active else None
        )
        if stinger_missing_route_reason is not None:
            # This one-time pickup shares a unique Jail with a finite Shop
            # alternate. If either route is unavailable in this build, leave
            # the pickup vanilla and continue randomizing the other sources.
            available_world = tuple(
                location for location in available_world
                if not (
                    location.package_path.casefold() == STINGER_WORLD_PACKAGE.casefold()
                    and location.export_name.casefold() == STINGER_WORLD_EXPORT_NAME.casefold()
                )
            )
            availability_warnings.append(
                "Josee's unique Stinger pickup left vanilla because "
                f"{stinger_missing_route_reason}; other World Loot sources remain active."
            )
            stinger_world_active = False
        linked_stinger_shop_required = (
            stinger_world_active
            and stinger_shop_catalog_slot is not None
        )
        requested_table_families = tuple(
            family for family in SHOP_TABLE_NAMES
            if family in (
                set(requested_shop_families)
                | set(requested_trader_families)
                | set(unlock_trader_families)
                | ({STINGER_SHOP_FAMILY} if linked_stinger_shop_required else set())
                | ({OGRE_SHOP_FAMILY} if request.options.randomize_initial_inventory else set())
            )
        )
        available_shop_families = _available_shop_families(
            requested_table_families, catalogs.shops, inventory,
            include_protected_families=unlock_trader_families,
        )
        missing_shop_families = tuple(
            family for family in requested_shop_families
            if family not in available_shop_families and not inventory.contains(shop_package_by_family[family])
        )
        if missing_shop_families:
            availability_warnings.append(
                "Shop tables skipped because their source packages are absent: "
                + ", ".join(missing_shop_families)
                + "."
            )
        missing_trader_families = tuple(
            family for family in requested_trader_families
            if family not in available_shop_families and not inventory.contains(shop_package_by_family[family])
        )
        missing_version_shop_families = set(missing_shop_families) | set(missing_trader_families)
        if missing_version_shop_families:
            skipped_missing_shop_offers = sum(
                _is_finite_shop_catalog_offer(product)
                for product in catalogs.shops
                if product.family.upper() in missing_version_shop_families
            )
            _add_version_skip_count(version_skip_counts, "Shops", skipped_missing_shop_offers)
        if missing_trader_families:
            if "TRADE_DLC" in missing_trader_families:
                availability_warnings.append("NPC Trader Liv skipped because the optional TRADE_DLC table is absent.")
            if "TRADE" in missing_trader_families:
                availability_warnings.append("NPC Traders skipped because the base TRADE table is absent.")
        missing_unlock_families = tuple(
            family for family in unlock_trader_families if family not in available_shop_families
        )
        if missing_unlock_families:
            availability_warnings.append(
                "NPC Trader tiers could not be unlocked because their source tables are unavailable: "
                + ", ".join(missing_unlock_families)
                + "."
            )
        if request.options.randomize_materials and not available_materials:
            availability_warnings.append(
                "Materials skipped: no catalogued instance has its map, renewable class, and reward package available."
            )
        if request.options.randomize_world_loot and not available_world:
            availability_warnings.append("World Loot skipped: no catalogued source package is available.")
        catalogs = replace(
            catalogs,
            world_loot=available_world,
            materials=available_materials,
            shops=tuple(item for item in catalogs.shops if item.family.upper() in set(available_shop_families)),
        )

        if request.options.randomize_world_loot and field_enemy_catalog is not None:
            available_field_rows = field_enemy_catalog.eligible_locations(inventory)
            if len(available_field_rows) != len(field_enemy_catalog.locations):
                _add_version_skip_count(
                    version_skip_counts,
                    "Enemy Drops",
                    len(field_enemy_catalog.locations) - len(available_field_rows),
                )
                availability_warnings.append(
                    f"{len(field_enemy_catalog.locations) - len(available_field_rows)} FieldEnemy locations skipped: "
                    "their source packages are absent; available locations remain randomized."
                )
                field_enemy_catalog = replace(field_enemy_catalog, locations=available_field_rows)
            elif not available_field_rows:
                availability_warnings.append("FieldEnemy group skipped: no source locations are available.")

        if blood_code_catalog is not None:
            blood_slots = tuple(
                slot for slot in blood_code_catalog.slots
                if all(inventory.contains(package) for package in slot.physical_source_packages)
                and inventory.contains(slot.original_blood_code_package)
            )
            if request.options.randomize_blood_codes:
                _add_version_skip_count(
                    version_skip_counts,
                    "Blood Codes",
                    len(blood_code_catalog.slots) - len(blood_slots),
                )
            active_packages = {
                package
                for slot in blood_slots
                for package in slot.physical_source_packages
            }
            if any(slot.location_kind == "scenario_reward" for slot in blood_slots):
                active_packages.update(SCENARIO_SCHEMA_PACKAGES)
            blood_code_catalog = replace(
                blood_code_catalog,
                slots=blood_slots,
                source_package_sha256={
                    package: hashes for package, hashes in blood_code_catalog.source_package_sha256.items()
                    if package.casefold() in {item.casefold() for item in active_packages}
                },
            )
            if not blood_slots and request.options.randomize_blood_codes:
                availability_warnings.append("Blood Codes skipped: no grant Quest package and Blood Code Article pair is available.")

        if defensive_gadget_catalog is not None:
            original_gadget_catalog = defensive_gadget_catalog
            available_gadgets = tuple(
                item for item in original_gadget_catalog.items if inventory.contains(item.asset_path)
            )
            available_gadget_grants = tuple(
                slot for slot in original_gadget_catalog.grants
                if inventory.contains(slot.package_path)
                and inventory.contains(slot.original_asset_path)
            )
            skipped_gadgets = len(original_gadget_catalog.items) - len(available_gadgets)
            skipped_grants = len(original_gadget_catalog.grants) - len(available_gadget_grants)
            if skipped_gadgets:
                availability_warnings.append(f"{skipped_gadgets} Defensive Gadget identities skipped: Article package absent.")
            if skipped_grants:
                availability_warnings.append(f"{skipped_grants} Defensive Gadget grant slots skipped: source package or Article absent.")
            defensive_gadget_catalog = replace(
                original_gadget_catalog,
                items=available_gadgets,
                grants=available_gadget_grants,
            )

        if bequeathed_formae_catalog is not None:
            original_formae_catalog = bequeathed_formae_catalog
            available_formae = tuple(
                item for item in original_formae_catalog.items
                if inventory.contains(item.asset_path)
                and all(inventory.contains(grant.quest_asset) for grant in item.direct_grants)
            )
            skipped_formae = len(original_formae_catalog.items) - len(available_formae)
            if skipped_formae:
                availability_warnings.append(
                    f"{skipped_formae} Bequeathed Forma identities skipped: Article or direct Quest package absent."
                )
            bequeathed_formae_catalog = replace(original_formae_catalog, items=available_formae)

        if extractor_catalog is not None:
            present_tables = tuple(
                table for table in extractor_catalog.tables if inventory.contains(table.virtual_package)
            )
            missing_tables = tuple(
                table.table_name for table in extractor_catalog.tables
                if not inventory.contains(table.virtual_package)
            )
            if missing_tables:
                if request.options.randomize_extractor_recipes or request.options.cheaper_booster_extractor:
                    _add_version_skip_count(
                        version_skip_counts,
                        "Booster Extractor",
                        sum(
                            len(table.rows)
                            for table in extractor_catalog.tables
                            if table.table_name in missing_tables
                        ),
                    )
                availability_warnings.append(
                    "Extractor tables skipped because their source packages are absent: "
                    + ", ".join(missing_tables)
                    + "."
                )
            extractor_catalog = replace(extractor_catalog, tables=present_tables)

        if enemy_drop_catalog is not None:
            present_enemy_tables = tuple(
                name for name, package in ENEMY_DROP_TABLES if inventory.contains(package)
            )
            missing_enemy_tables = tuple(
                name for name, package in ENEMY_DROP_TABLES if not inventory.contains(package)
            )
            if missing_enemy_tables:
                if request.options.randomize_enemy_drops and enemy_drop_catalog is not None:
                    _add_version_skip_count(
                        version_skip_counts,
                        "Enemy Drops",
                        sum(
                            row.expected_slots
                            for row in enemy_drop_catalog.rows
                            if row.table in missing_enemy_tables
                            and row.classification != "UNRESOLVED"
                        ),
                    )
                availability_warnings.append(
                    "Enemy Drop tables skipped because their source packages are absent: "
                    + ", ".join(missing_enemy_tables)
                    + "."
                )

        if booster_catalog is not None:
            if request.options.randomize_boosters and not inventory.contains(BOOSTER_TABLE_PACKAGE):
                _add_version_skip_count(version_skip_counts, "Boosters", len(booster_catalog.slots))
                availability_warnings.append("Boosters skipped: the mastery DataTable source package is missing.")
            elif request.options.randomize_boosters:
                missing_rows = sum(
                    not inventory.contains(slot.booster_article_path)
                    for slot in booster_catalog.slots
                )
                if missing_rows:
                    _add_version_skip_count(version_skip_counts, "Boosters", missing_rows)
                    availability_warnings.append(
                        f"{missing_rows} Booster mastery rows skipped: their Article source package is absent."
                    )

        if extractor_catalog is not None:
            extractor_tables = tuple(
                table for table in extractor_catalog.tables if inventory.contains(table.virtual_package)
            )
            extractor_catalog = replace(extractor_catalog, tables=extractor_tables)
            if not extractor_catalog.tables:
                if request.options.randomize_extractor_recipes:
                    availability_warnings.append("Booster Extractor options skipped: no recipe DataTable source package is available.")
                if request.options.cheaper_booster_extractor:
                    availability_warnings.append("Cheaper Booster Extractor skipped: no recipe tables are present in this game version.")
            elif request.options.randomize_extractor_recipes and len(extractor_catalog.tables) < 2:
                availability_warnings.append("Extractor recipes will use only the source tables present in this installation.")

        if defensive_gadget_catalog is not None:
            defensive_gadget_catalog = replace(
                defensive_gadget_catalog,
                items=tuple(item for item in defensive_gadget_catalog.items if inventory.contains(item.asset_path)),
                grants=tuple(slot for slot in defensive_gadget_catalog.grants if inventory.contains(slot.package_path)),
            )
        if jail_catalog is not None:
            available_jails = jail_catalog.available_items(inventory)
            skipped_jails = len(jail_catalog.items) - len(available_jails)
            if skipped_jails:
                availability_warnings.append(
                    f"{skipped_jails} Jail identities skipped: their Article or World Loot source package is absent."
                )
            jail_catalog = replace(
                jail_catalog,
                items=available_jails,
            )
        if canonical_article_reward_catalog is not None:
            canonical_article_reward_catalog = replace(
                canonical_article_reward_catalog,
                items=tuple(
                    item for item in canonical_article_reward_catalog.items
                    if inventory.contains(item.asset_path)
                ),
            )
        if bequeathed_formae_catalog is not None:
            original_formae = bequeathed_formae_catalog.items
            formae_items = bequeathed_formae_catalog.available_items(inventory)
            available_by_id = {item.object.casefold(): item for item in formae_items}
            missing_formae = len(original_formae) - len(formae_items)
            missing_grant_routes = sum(
                len(item.direct_grants) - len(available_by_id[item.object.casefold()].direct_grants)
                for item in original_formae
                if item.source_class == "DIRECT_QUEST" and item.object.casefold() in available_by_id
            )
            if missing_formae or missing_grant_routes:
                availability_warnings.append(
                    f"{missing_formae} Bequeathed Forma identities and {missing_grant_routes} Quest routes skipped: "
                    "their Article or Quest packages are absent."
                )
            bequeathed_formae_catalog = replace(bequeathed_formae_catalog, items=formae_items)
        try:
            seed = normalize_seed(request.seed)
        except (TypeError, ValueError) as error:
            raise GenerationError(f"Seed validation failed: {error}") from error
        any_feature = (
            request.options.randomize_world_loot
            or request.options.randomize_materials
            or request.options.randomize_shops
            or request.options.randomize_npc_traders
            or request.options.randomize_enemy_drops
            or request.options.randomize_blood_codes
            or request.options.randomize_boosters
            or request.options.randomize_extractor_recipes
            or request.options.cheaper_booster_extractor
            or request.options.low_blood_code_mastery_exp
            or request.options.randomize_initial_inventory
            or request.options.randomize_defensive_gadget_npc_rewards
            or request.options.randomize_bequeathed_formae_quest_rewards
            or request.options.unlock_all_booster_slots
            or request.options.motorcycle_unlocked
            or request.options.second_weapon_unlocked
            or request.options.disable_tutorials
            or request.options.tap_to_skip_dialogue
        )
        if request.mappings_path is not None and not isinstance(request.mappings_path, Path):
            raise GenerationError("mappings_path must be a filesystem path or None.")
        try:
            mappings = request.mappings_path.expanduser().resolve(strict=True) if request.mappings_path else None
        except OSError as error:
            raise GenerationError(f"Could not access the CV2.usmap mapping: {error}") from error
        if any_feature and (mappings is None or not mappings.is_file()):
            raise GenerationError("A matching CV2.usmap file is required for the enabled randomizer features.")
        if progress:
            progress("creating-workspace", 0, 1)
        try:
            workspace = self.workspace_manager.create_generation(seed)
        except OSError as error:
            raise GenerationError(f"Could not create the generation workspace: {error}") from error

        output_root = workspace.staging / "Content"
        world_intermediate_root = workspace.extracted / "world-loot" / "Content"
        empty_materials = MaterialRandomizationPlan((), (), ())
        shop_stage: ShopTablesStage | None = None
        material_preparation: MaterialPlanPreparation | None = None
        world_result: WorldLootGenerationResult | None = None
        world_direct_grant_results: tuple[WorldLootDirectGrantResult, ...] = ()
        shop_result: ShopGenerationResult | None = None
        unavailable_shop_stage_families: tuple[str, ...] = ()
        unavailable_installed_shop_families: tuple[str, ...] = ()
        incompatible_shop_product_count = 0
        linked_stinger_shop_product: ShopTableProduct | None = None
        linked_ogre_shop_product: ShopTableProduct | None = None
        material_result: MaterialGenerationResult | None = None
        enemy_drop_preparation = None
        enemy_drop_result: EnemyDropGenerationResult | None = None
        blood_code_preparation: BloodCodePreparation | None = None
        blood_code_plan: BloodCodePlan | None = None
        blood_code_result: BloodCodeGenerationResult | None = None
        booster_preparation: BoosterPreparation | None = None
        booster_plan: BoosterPlan | None = None
        booster_result: BoosterGenerationResult | None = None
        extractor_preparation: ExtractorPreparation | None = None
        extractor_plan: ExtractorPlan | None = None
        extractor_result: ExtractorGenerationResult | None = None
        initial_inventory_preparation: InitialInventoryPreparation | None = None
        initial_inventory_plan: InitialInventoryPlan | None = None
        initial_inventory_result: InitialInventoryGenerationResult | None = None
        booster_slot_unlock_result: BoosterSlotUnlockResult | None = None
        motorcycle_unlock_result: MotorcycleUnlockResult | None = None
        tutorial_disable_result: TutorialDisableResult | None = None
        dialogue_skip_result: DialogueSkipResult | None = None
        blood_code_mastery_result: BloodCodeMasteryResult | None = None
        defensive_gadget_preparation: DefensiveGadgetPreparation | None = None
        defensive_gadget_plan: DefensiveGadgetNpcPlan | None = None
        defensive_gadget_result: DefensiveGadgetGenerationResult | None = None
        bequeathed_formae_preparation: BequeathedFormaPreparation | None = None
        bequeathed_formae_plan: BequeathedFormaPlan | None = None
        bequeathed_formae_result: BequeathedFormaGenerationResult | None = None
        package_result: PackageResult | None = None
        installation_result: InstallationResult | None = None
        success = False
        failure_message = "Generation did not reach staging validation."
        reward_reservations = RewardReservationSet()
        try:
            # World owns every physical direct-reward occurrence first. The
            # Extractor then borrows selected Booster occurrences from this
            # plan and returns one unused vanilla recipe output to each source.
            # This keeps the combined inventory a strict one-for-one exchange.
            if request.options.randomize_world_loot:
                quest_sources = (
                    *catalogs.world_loot,
                    *(field_enemy_catalog.locations if field_enemy_catalog is not None else ()),
                )
                compatible_quest_sources, incompatible_quest_grants = filter_compatible_world_loot_quest_grants(
                    quest_sources,
                    game_root=game_root,
                    work_root=workspace.root,
                    mappings_path=mappings,
                    game_version=detection.version_id,
                    data_revision=detection.catalog_revision,
                    extractor=asset_extractor,
                    serializer=self.world_quest_article_serializer,
                    compatibility_profile=detection.world_loot_compatibility_profile,
                    progress=progress,
                )
                compatible_quest_by_id = {
                    (item.package_path.casefold(), str(item.destination_id).casefold()): item
                    for item in compatible_quest_sources
                    if item.destination_kind == "QUEST_ARTICLE_GRANT"
                }
                catalogs = replace(
                    catalogs,
                    world_loot=tuple(
                        compatible_quest_by_id.get(
                            (item.package_path.casefold(), str(item.destination_id).casefold()), item,
                        )
                        for item in catalogs.world_loot
                        if item.destination_kind != "QUEST_ARTICLE_GRANT"
                        or (item.package_path.casefold(), str(item.destination_id).casefold()) in compatible_quest_by_id
                    ),
                )
                if field_enemy_catalog is not None:
                    field_enemy_catalog = replace(
                        field_enemy_catalog,
                        locations=tuple(
                            compatible_quest_by_id.get(
                                (item.package_path.casefold(), str(item.destination_id).casefold()), item,
                            )
                            for item in field_enemy_catalog.locations
                            if item.destination_kind != "QUEST_ARTICLE_GRANT"
                            or (item.package_path.casefold(), str(item.destination_id).casefold()) in compatible_quest_by_id
                        ),
                    )
                if incompatible_quest_grants:
                    incompatible_quest_grant_count = len(incompatible_quest_grants)
                    _add_version_skip_count(
                        version_skip_counts, "Quest Rewards", incompatible_quest_grant_count,
                    )
                    skipped_names = ", ".join(
                        item.display_name or str(item.destination_id)
                        for item in incompatible_quest_grants
                    )
                    availability_warnings.append(
                        "Structured World Loot quest rewards left vanilla because their installed task data "
                        f"does not match the validated layout: {skipped_names}."
                    )
            world_plan_locations = (
                *catalogs.world_loot,
                *(field_enemy_catalog.locations if field_enemy_catalog else ()),
            )
            world_plan_candidates = (
                (
                    *(
                        defensive_gadget_world_candidates(
                            _without_reserved_rewards(defensive_gadget_catalog, reward_reservations, "defensive_gadget"),
                        )
                        if defensive_gadget_catalog is not None else ()
                    ),
                    *(jail_world_candidates(
                        _without_reserved_rewards(jail_catalog, reward_reservations, "jail"),
                        world_loot=catalogs.world_loot,
                    )
                      if jail_catalog is not None else ()),
                    *(canonical_article_world_candidates(canonical_article_reward_catalog, include_boosters=False)
                      if canonical_article_reward_catalog is not None else ()),
                )
                if request.options.randomize_world_loot else ()
            )
            world_plan_options = {
                "enabled": request.options.randomize_world_loot,
                "include_healing": request.options.randomize_healing,
                "unsafe_packages": catalogs.unsafe_packages,
                "available_reward_packages": source_availability.available_packages,
                "supported_quest_grant_destinations": (
                    field_enemy_catalog.destination_ids if field_enemy_catalog is not None else ()
                ),
                "reserved_reward_identities": reward_reservations.asset_paths,
                "catalog_candidates": world_plan_candidates,
            }
            world_plan = build_world_loot_randomization_plan(
                world_plan_locations,
                seed,
                **world_plan_options,
            )
            if (request.options.randomize_extractor_recipes or request.options.cheaper_booster_extractor) \
                    and extractor_catalog is not None and extractor_catalog.tables:
                if extractor_catalog is None or mappings is None:
                    raise GenerationError("Booster Extractor catalog or CV2.usmap mapping was not loaded.")
                try:
                    extractor_pool = (
                        build_extractor_booster_pool(
                            extractor=extractor_catalog,
                            world_plan=world_plan,
                            available_reward_packages=source_availability.available_packages,
                        )
                        if request.options.randomize_extractor_recipes else ()
                    )
                    extractor_preparation = prepare_extractor_plan(
                        extractor_catalog,
                        extractor_pool,
                        seed,
                        game_root=game_root,
                        work_root=workspace.root,
                        mappings_path=mappings,
                        game_version=detection.version_id,
                        data_revision=detection.catalog_revision,
                        extractor=asset_extractor,
                        serializer=self.extractor_serializer,
                        mix_postgame_dlc_with_base=request.options.mix_postgame_dlc_with_base,
                        randomize_recipes=request.options.randomize_extractor_recipes,
                        available_reward_packages=source_availability.available_packages,
                        progress=progress,
                    )
                    incompatible_tables = tuple(
                        getattr(extractor_preparation, "incompatible_tables", ())
                    )
                    if incompatible_tables:
                        skipped_rows = sum(
                            len(table.rows)
                            for table in extractor_catalog.tables
                            if table.table_name in incompatible_tables
                        )
                        _add_version_skip_count(
                            version_skip_counts, "Booster Extractor", skipped_rows,
                        )
                        availability_warnings.append(
                            "Booster Extractor files left unchanged because their layout is incompatible with "
                            "this game version: " + ", ".join(incompatible_tables) + "."
                        )
                except ExtractorGenerationError as error:
                    _add_version_skip_count(
                        version_skip_counts,
                        "Booster Extractor",
                        sum(len(table.rows) for table in extractor_catalog.tables),
                    )
                    source_availability = _mark_invalid_sources(
                        source_availability,
                        "extractor",
                        (table.virtual_package for table in extractor_catalog.tables),
                        str(error),
                    )
                    availability_warnings.append(f"Booster Extractor options skipped: source validation failed: {error}")
                if extractor_preparation is not None:
                    extractor_plan = extractor_preparation.plan
                    if extractor_plan is not None:
                        world_plan = exchange_extractor_world_sources(world_plan, extractor_plan)
            available_table_families = tuple(
                family for family in requested_table_families
                if family in available_shop_families
                and (
                    family in unlock_trader_families
                    or any(product.family.upper() == family and not product.protected for product in catalogs.shops)
                )
            )
            should_prepare_shops = bool(available_table_families)
            parsed_shop_products = ()
            shop_products = ()
            trader_products = ()
            trader_products_in_tables = ()
            unavailable_shop_products = ()
            if should_prepare_shops:
                if progress:
                    progress("preparing-shop-tables", 0, len(available_table_families))
                try:
                    shop_stage = prepare_shop_tables(
                        catalogs,
                        game_root=game_root,
                        work_root=workspace.root,
                        mappings_path=mappings,
                        game_version=detection.version_id,
                        data_revision=detection.catalog_revision,
                        enabled_families=available_table_families,
                        extractor=asset_extractor,
                        progress=progress,
                    )
                    unavailable_shop_stage_families = tuple(getattr(
                        shop_stage,
                        "unavailable_families",
                        getattr(shop_stage, "unavailable_optional_families", ()),
                    ))
                    unexpected = tuple(
                        family for family in unavailable_shop_stage_families
                        if inventory.contains(shop_package_by_family[family])
                    )
                    if unexpected:
                        unavailable_installed_shop_families = unexpected
                        availability_warnings.append(
                            "Shop tables marked unavailable after extraction or validation failed for installed packages: "
                            + ", ".join(unexpected)
                            + "."
                        )
                    incompatible_shop_families = tuple(getattr(shop_stage, "incompatible_families", ()))
                    if incompatible_shop_families:
                        availability_warnings.append(
                            "Shop tables skipped because their layout does not match this game's version: "
                            + ", ".join(incompatible_shop_families)
                            + "."
                        )
                        skipped_incompatible_shop_offers = sum(
                            _is_finite_shop_catalog_offer(product)
                            for product in catalogs.shops
                            if product.family.upper() in set(incompatible_shop_families)
                        )
                        _add_version_skip_count(
                            version_skip_counts, "Shops", skipped_incompatible_shop_offers,
                        )
                    skipped_catalog_product_count = int(getattr(
                        shop_stage, "skipped_catalog_product_count", 0,
                    ))
                    incompatible_shop_product_count = skipped_catalog_product_count
                    _add_version_skip_count(
                        version_skip_counts, "Shops", skipped_catalog_product_count,
                    )
                    if skipped_catalog_product_count:
                        availability_warnings.append(
                            f"Skipped {skipped_catalog_product_count} shop offers absent from this game's version."
                        )
                    parsed_shop_products = tuple(
                        product for table in shop_stage.tables for product in table.parsed.products
                    )
                    if linked_stinger_shop_required:
                        linked_product = find_stinger_shop_product(parsed_shop_products)
                        if not isinstance(linked_product, ShopTableProduct):
                            raise ShopGenerationError(
                                "The parsed base Shop table omitted the audited Josee Stinger alternate slot."
                            )
                        linked_stinger_shop_product = linked_product
                    ogre_product = find_ogre_shop_product(parsed_shop_products)
                    if (
                        isinstance(ogre_product, ShopTableProduct)
                        and inventory.contains(ogre_product.article_package_path)
                    ):
                        linked_ogre_shop_product = ogre_product
                    trader_products_in_tables = tuple(
                        product for product in parsed_shop_products
                        if is_npc_trader_row(product.family, product.shop_row)
                    )
                    available_products = tuple(
                        product for product in parsed_shop_products
                        if inventory.contains(product.article_package_path)
                    )
                    unavailable_shop_products = tuple(
                        product for product in parsed_shop_products
                        if not inventory.contains(product.article_package_path)
                    )
                    unavailable_finite_shop_products = sum(
                        shop_slot_lock_reason(product) is None
                        for product in unavailable_shop_products
                    )
                    _add_version_skip_count(
                        version_skip_counts, "Shops", unavailable_finite_shop_products,
                    )
                    # Use the same decoded identity in availability diagnostics
                    # as in eligibility; do not report ambiguous literal aliases.
                    shop_sources = resolve_content_availability({"shops": (
                        *(f"/Game/Blueprint/GameSystem/Shop/{name}" for name in SHOP_TABLE_NAMES.values()),
                        *(product.article_package_path for product in parsed_shop_products),
                    )}, inventory)
                    source_availability = replace(source_availability, sources=(
                        *(source for source in source_availability.sources if source.subsystem != "shops"),
                        *shop_sources.sources,
                    ))
                    if unavailable_installed_shop_families:
                        source_availability = _mark_invalid_sources(
                            source_availability,
                            "shops",
                            (
                                shop_package_by_family[family]
                                for family in unavailable_installed_shop_families
                            ),
                            "installed package inventory listed this table, but its installed-version layout could not be validated",
                        )
                    shop_products = exclude_linked_jail_shop_routes(
                        product for product in available_products
                        if product.family.upper() in requested_shop_families
                        and not is_npc_trader_row(product.family, product.shop_row)
                        # Josee's finite Stinger shop slot is an alternate route
                        # for the same one-time reward as the Sunken City pickup.
                        # Never let it become a second source/destination, even
                        # when World Loot is disabled and Shops are randomized.
                    )
                    trader_products = tuple(
                        product for product in available_products
                        if request.options.randomize_npc_traders
                        and product.family.upper() in requested_trader_families
                        and is_npc_trader_row(product.family, product.shop_row)
                    )
                except (ShopGenerationError, OSError, ValueError) as error:
                    if linked_stinger_shop_required:
                        if isinstance(error, OSError):
                            raise GenerationError(
                                f"Could not read Josee's linked Stinger shop route safely: {error}"
                            ) from error
                        stinger_sources = tuple(
                            location for location in world_plan_locations
                            if location.package_path.casefold() == STINGER_WORLD_PACKAGE.casefold()
                            and location.export_name.casefold() == STINGER_WORLD_EXPORT_NAME.casefold()
                        )
                        if len(stinger_sources) != 1:
                            raise GenerationError(
                                "Could not identify one canonical Josee Stinger World Loot source to leave vanilla."
                            ) from error
                        try:
                            world_plan = build_world_loot_randomization_plan(
                                world_plan_locations,
                                seed,
                                excluded_slots=(world_loot_slot(stinger_sources[0]),),
                                **world_plan_options,
                            )
                            if extractor_plan is not None:
                                world_plan = exchange_extractor_world_sources(world_plan, extractor_plan)
                        except (ValueError, RuntimeError) as replan_error:
                            raise GenerationError(
                                f"Could not safely keep Josee's Stinger source vanilla: {replan_error}"
                            ) from replan_error
                        _add_version_skip_count(version_skip_counts, "World Loot", 1)
                        availability_warnings.append(
                            "Josee's Stinger pickup left vanilla because its linked Shop row could not be "
                            f"validated for this game version: {error}."
                        )
                        stinger_world_active = False
                        linked_stinger_shop_required = False
                    source_availability = _mark_invalid_sources(
                        source_availability,
                        "shops",
                        (shop_package_by_family[family] for family in available_table_families),
                        str(error),
                    )
                    skipped_shop_offers = sum(
                        _is_finite_shop_catalog_offer(product)
                        for product in catalogs.shops
                        if product.family.upper() in set(available_table_families)
                    )
                    _add_version_skip_count(version_skip_counts, "Shops", skipped_shop_offers)
                    availability_warnings.append(f"Shops skipped: source table validation failed: {error}")
                    shop_stage = None
                    unavailable_shop_stage_families = ()
                    unavailable_installed_shop_families = ()
                    parsed_shop_products = ()
                    shop_products = ()
                    trader_products = ()
                    trader_products_in_tables = ()

            ordinary_families = tuple(
                family for family in requested_shop_families if family in available_table_families
            )
            standard_global_mix = (
                request.options.mix_shops_with_world
                and request.options.randomize_world_loot
                and request.options.randomize_shops
                and bool(ordinary_families)
            )
            trader_global_mix = (
                request.options.randomize_npc_traders
                and request.options.npc_traders_mode == "mixed"
                and bool(trader_products)
            )
            plans: list[ShopRandomizationPlan] = []
            if standard_global_mix or trader_global_mix:
                global_products = tuple(shop_products)
                if trader_global_mix:
                    global_products = (*global_products, *trader_products)
                global_families = tuple(
                    family for family in SHOP_TABLE_NAMES
                    if any(product.family.upper() == family for product in global_products)
                )
                world_plan, global_shop_plan = build_global_article_mix_plans(
                    world_plan,
                    global_products,
                    seed,
                    enabled_families=global_families,
                    mix_postgame_dlc_with_base=request.options.mix_postgame_dlc_with_base,
                    unavailable_families=(
                        unavailable_shop_stage_families
                    ),
                    reserved_reward_identities=reward_reservations.asset_paths,
                )
                plans.append(global_shop_plan)
            else:
                plans.append(build_shop_randomization_plan(
                    shop_products,
                    seed,
                    enabled_families=ordinary_families,
                    mix_postgame_dlc_with_base=request.options.mix_postgame_dlc_with_base,
                    unavailable_families=(
                        unavailable_shop_stage_families
                    ),
                    reserved_reward_identities=reward_reservations.asset_paths,
                ))
            if request.options.randomize_npc_traders and request.options.npc_traders_mode == "only":
                plans.append(build_npc_trader_only_plan(
                    trader_products,
                    seed,
                    unavailable_families=tuple(
                        family for family in ("TRADE", "TRADE_DLC")
                        if family in requested_trader_families
                        and (
                            family not in available_table_families
                            or family in unavailable_shop_stage_families
                        )
                    ),
                    reserved_reward_identities=reward_reservations.asset_paths,
                ))
            shop_plan = combine_shop_randomization_plans(*plans)
            shop_plan = replace(shop_plan, disabled_slots=(
                *shop_plan.disabled_slots, *unavailable_shop_products,
            ))
            if request.options.randomize_npc_traders:
                shop_plan = replace(
                    shop_plan,
                    npc_traders=_npc_trader_plan_summary(
                        mode=request.options.npc_traders_mode,
                        parsed_products=trader_products_in_tables,
                        plan=shop_plan,
                        world_plan=world_plan,
                        requested_families=requested_trader_families,
                        unavailable_families=tuple(dict.fromkeys((
                            *missing_trader_families,
                            *unavailable_shop_stage_families,
                        ))),
                    ),
                )
            if request.options.randomize_initial_inventory:
                if not inventory.contains(INITIAL_INVENTORY_PACKAGE):
                    availability_warnings.append(
                        "Initial Inventory skipped: DA_InitialInventory_Player is absent from the installed package inventory."
                    )
                elif defensive_gadget_catalog is None or jail_catalog is None or mappings is None:
                    availability_warnings.append(
                        "Initial Inventory skipped: its source catalogs or CV2.usmap mapping are unavailable."
                    )
                else:
                    original_world_plan, original_shop_plan = world_plan, shop_plan
                    try:
                        planned_inventory = build_initial_inventory_plan(
                            seed,
                            detection.catalog_revision,
                            weapons=build_available_weapon_pool(
                                catalogs,
                                inventory,
                                unsafe_packages=catalogs.unsafe_packages,
                            ),
                            defensive_gadgets=defensive_gadget_catalog,
                            jails=jail_catalog,
                            reservations=reward_reservations,
                            source_occurrences=planned_reward_source_occurrences(world_plan, shop_plan),
                        )
                        for assignment in planned_inventory.assignments:
                            if not assignment.source_occurrence_id:
                                raise InitialInventoryGenerationError(
                                    f"Starting {assignment.display_name} has no owned source occurrence to transfer."
                                )
                            world_plan, shop_plan = exchange_planned_reward_occurrence(
                                world_plan,
                                shop_plan,
                                source_occurrence_id=assignment.source_occurrence_id,
                                replacement_article_path=assignment.vanilla_article_path,
                                replacement_occurrence_id=f"initial-inventory:{assignment.slot_id}",
                                replacement_content_class="BASE",
                            )
                        initial_inventory_preparation = prepare_initial_inventory(
                            planned_inventory,
                            game_root=game_root,
                            work_root=workspace.root,
                            mappings_path=mappings,
                            game_version=detection.version_id,
                            data_revision=detection.catalog_revision,
                            extractor=asset_extractor,
                            serializer=self.initial_inventory_serializer,
                            motorcycle_unlocked=request.options.motorcycle_unlocked,
                            progress=progress,
                        )
                    except (InitialInventoryGenerationError, ValueError, OSError) as error:
                        world_plan, shop_plan = original_world_plan, original_shop_plan
                        initial_inventory_preparation = None
                        _handle_initial_inventory_preparation_failure(
                            error,
                            availability_warnings=availability_warnings,
                        )
                    else:
                        initial_inventory_plan = planned_inventory
            elif request.options.motorcycle_unlocked:
                motorcycle_forma = MOTORCYCLE_FORMA_ARTICLE_PACKAGE
                if not inventory.contains(INITIAL_INVENTORY_PACKAGE):
                    availability_warnings.append(
                        "Motorcycle at Start skipped: DA_InitialInventory_Player is absent from this game version."
                    )
                else:
                    motorcycle_inventory = inventory
                    if not motorcycle_inventory.contains(motorcycle_forma):
                        try:
                            motorcycle_inventory = asset_extractor.inspect_package_inventory(game_root)
                        except (AssetExtractionError, OSError, RuntimeError, ValueError) as error:
                            raise GenerationError(
                                f"Could not verify the installed Motorcycle Forma Article: {error}"
                            ) from error
                    if not motorcycle_inventory.contains(motorcycle_forma):
                        availability_warnings.append(
                            "Motorcycle at Start skipped: its Forma Article is absent from this game version."
                        )
                    elif mappings is None:
                        availability_warnings.append(
                            "Motorcycle at Start skipped: CV2.usmap is unavailable."
                        )
                    else:
                        try:
                            initial_inventory_preparation = prepare_initial_inventory(
                                None,
                                game_root=game_root,
                                work_root=workspace.root,
                                mappings_path=mappings,
                                game_version=detection.version_id,
                                data_revision=detection.catalog_revision,
                                extractor=asset_extractor,
                                serializer=self.initial_inventory_serializer,
                                motorcycle_unlocked=True,
                                progress=progress,
                            )
                        except (InitialInventoryGenerationError, ValueError, OSError) as error:
                            _handle_initial_inventory_preparation_failure(
                                error,
                                availability_warnings=availability_warnings,
                            )
            if request.options.motorcycle_unlocked:
                phase_started = time.perf_counter()
                try:
                    motorcycle_package_inventory = inventory_for_motorcycle_unlock_packages(
                        game_root=game_root,
                        cached_inventory=inventory,
                        inventory_extractor=asset_extractor,
                    )
                    missing_motorcycle_packages = [
                        package for package in MOTORCYCLE_UNLOCK_PACKAGES
                        if not motorcycle_package_inventory.contains(package)
                    ]
                    if missing_motorcycle_packages:
                        availability_warnings.append(
                            "Motorcycle at Start skipped: required StoryFlag/player package(s) are absent: "
                            + ", ".join(missing_motorcycle_packages)
                            + "."
                        )
                    elif mappings is None:
                        availability_warnings.append(
                            "Motorcycle at Start skipped: CV2.usmap is unavailable."
                        )
                    else:
                        motorcycle_unlock_result = apply_motorcycle_unlock(
                            game_root=game_root,
                            work_root=workspace.root,
                            output_content_root=output_root,
                            mappings_path=mappings,
                            game_version=detection.version_id,
                            data_revision=detection.catalog_revision,
                            extractor=asset_extractor,
                            package_inventory=motorcycle_package_inventory,
                            serializer=self.motorcycle_unlock_serializer,
                            progress=progress,
                        )
                except MotorcycleUnlockError as error:
                    if is_game_data_layout_mismatch(error):
                        availability_warnings.append(
                            "Motorcycle at Start skipped: its StoryFlag/player data layout is incompatible "
                            f"with this game version ({error})."
                        )
                    else:
                        raise GenerationError(f"Could not unlock the motorcycle for new games: {error}") from error
                except GenerationError:
                    raise
                except (AssetExtractionError, OSError, RuntimeError, ValueError) as error:
                    raise GenerationError(f"Could not inspect installed motorcycle unlock flags: {error}") from error
                performance["motorcycle_unlock_patch_seconds"] = time.perf_counter() - phase_started
            if request.options.randomize_materials:
                material_locations = catalogs.materials
                if material_locations:
                    if progress:
                        progress("validating-material-pool", 0, len(material_locations))
                    try:
                        material_preparation = prepare_material_plan(
                            material_locations,
                            seed,
                            game_root=game_root,
                            work_root=workspace.root,
                            mappings_path=mappings,
                            game_version=detection.version_id,
                            data_revision=detection.catalog_revision,
                            enabled=True,
                            unsafe_packages=catalogs.unsafe_packages,
                            resolver=self.material_resolver,
                            extractor=asset_extractor,
                            prepared_class_profiles=prepared_material_profiles,
                            progress=progress,
                        )
                    except (MaterialGenerationError, OSError, ValueError) as error:
                        source_availability = _mark_invalid_sources(
                            source_availability,
                            "materials",
                            (
                                package
                                for item in material_locations
                                for package in (item.package_path, item.resource_class_package)
                            ),
                            str(error),
                        )
                        _add_version_skip_count(
                            version_skip_counts,
                            "Ingredients",
                            len(material_locations),
                        )
                        availability_warnings.append(f"Materials skipped: source validation failed: {error}")
                if material_preparation is not None:
                    skipped_materials = sum(
                        not item.location.protected and item.reason != "unsafe package"
                        for item in material_preparation.plan.locked_locations
                    )
                    _add_version_skip_count(version_skip_counts, "Ingredients", skipped_materials)
            material_plan = material_preparation.plan if material_preparation else empty_materials
            if request.options.randomize_materials and material_preparation is None:
                # A missing source package leaves that optional location vanilla.
                # The filtered rows remain visible as disabled diagnostics, but
                # never enter Blueprint/map extraction.
                material_plan = MaterialRandomizationPlan((), (), catalogs_for_manifest.materials)
            if request.options.randomize_enemy_drops:
                if enemy_drop_catalog is None or mappings is None:
                    availability_warnings.append("Enemy Drops skipped: catalog or CV2.usmap mapping is unavailable.")
                elif not present_enemy_tables:
                    availability_warnings.append("Enemy Drops skipped: none of the known source tables exists.")
                else:
                    if progress:
                        progress("preparing-enemy-drops", 0, len(present_enemy_tables))
                    try:
                        enemy_drop_preparation = prepare_enemy_drop_plan(
                            enemy_drop_catalog,
                            seed,
                            game_root=game_root,
                            work_root=workspace.root,
                            mappings_path=mappings,
                            game_version=detection.version_id,
                            data_revision=detection.catalog_revision,
                            extractor=asset_extractor,
                            serializer=self.enemy_drop_serializer,
                            mix_postgame_dlc_with_base=request.options.mix_postgame_dlc_with_base,
                            active_tables=present_enemy_tables,
                            progress=progress,
                        )
                        incompatible_tables = tuple(
                            getattr(enemy_drop_preparation, "incompatible_tables", ())
                        )
                        if incompatible_tables:
                            skipped_drop_slots = sum(
                                row.expected_slots
                                for row in enemy_drop_catalog.rows
                                if row.table in incompatible_tables
                                and row.classification != "UNRESOLVED"
                            )
                            _add_version_skip_count(
                                version_skip_counts, "Enemy Drops", skipped_drop_slots,
                            )
                            availability_warnings.append(
                                "Enemy Drop files left unchanged because their layout is incompatible with this "
                                "game version: " + ", ".join(incompatible_tables) + "."
                            )
                    except EnemyDropGenerationError as error:
                        _add_version_skip_count(
                            version_skip_counts,
                            "Enemy Drops",
                            sum(
                                row.expected_slots
                                for row in enemy_drop_catalog.rows
                                if row.table in present_enemy_tables
                                and row.classification != "UNRESOLVED"
                            ),
                        )
                        source_availability = _mark_invalid_sources(
                            source_availability,
                            "enemy_drops",
                            (package for _name, package in ENEMY_DROP_TABLES if _name in present_enemy_tables),
                            str(error),
                        )
                        availability_warnings.append(f"Enemy Drops skipped: available table validation failed: {error}")
            enemy_drop_plan = enemy_drop_preparation.plan if enemy_drop_preparation else None
            if request.options.randomize_blood_codes and blood_code_catalog is not None and blood_code_catalog.slots:
                if mappings is None:
                    availability_warnings.append("Blood Codes skipped: CV2.usmap mapping is unavailable.")
                else:
                    active_blood_catalog = blood_code_catalog
                    skipped_blood_slots = 0
                    skipped_blood_packages: list[str] = []
                    while active_blood_catalog.slots:
                        try:
                            blood_code_preparation = prepare_blood_code_plan(
                                active_blood_catalog,
                                seed,
                                game_root=game_root,
                                work_root=workspace.root,
                                mappings_path=mappings,
                                game_version=detection.version_id,
                                data_revision=detection.catalog_revision,
                                extractor=asset_extractor,
                                serializer=self.blood_code_serializer,
                                mix_postgame_dlc_with_base=request.options.mix_postgame_dlc_with_base,
                                progress=progress,
                            )
                        except BloodCodeGenerationError as error:
                            message = str(error).casefold()
                            bad_packages = tuple(
                                package for package in required_blood_code_package_paths(active_blood_catalog)
                                if package.casefold() in message
                            )
                            if not is_game_data_layout_mismatch(error) or not bad_packages:
                                _add_version_skip_count(
                                    version_skip_counts, "Blood Codes", len(active_blood_catalog.slots),
                                )
                                source_availability = _mark_invalid_sources(
                                    source_availability,
                                    "blood_codes",
                                    required_blood_code_package_paths(active_blood_catalog),
                                    str(error),
                                )
                                availability_warnings.append(
                                    f"Blood Codes skipped: source validation failed: {error}"
                                )
                                blood_code_preparation = None
                                break
                            bad_keys = {package.casefold() for package in bad_packages}
                            failed_schema = bad_keys.intersection(
                                package.casefold() for package in SCENARIO_SCHEMA_PACKAGES
                            )
                            retained_slots = tuple(
                                slot for slot in active_blood_catalog.slots
                                if not (
                                    any(package.casefold() in bad_keys for package in slot.physical_source_packages)
                                    or (failed_schema and slot.location_kind == "scenario_reward")
                                )
                            )
                            removed_count = len(active_blood_catalog.slots) - len(retained_slots)
                            if removed_count <= 0:
                                _add_version_skip_count(
                                    version_skip_counts, "Blood Codes", len(active_blood_catalog.slots),
                                )
                                availability_warnings.append(
                                    "Blood Codes skipped: an incompatible source file could not be assigned to "
                                    f"specific locations ({', '.join(bad_packages)})."
                                )
                                blood_code_preparation = None
                                break
                            skipped_blood_slots += removed_count
                            skipped_blood_packages.extend(bad_packages)
                            active_packages = {
                                package for slot in retained_slots for package in slot.physical_source_packages
                            }
                            if any(slot.location_kind == "scenario_reward" for slot in retained_slots):
                                active_packages.update(SCENARIO_SCHEMA_PACKAGES)
                            active_keys = {package.casefold() for package in active_packages}
                            active_blood_catalog = replace(
                                active_blood_catalog,
                                slots=retained_slots,
                                source_package_sha256={
                                    package: hashes
                                    for package, hashes in active_blood_catalog.source_package_sha256.items()
                                    if package.casefold() in active_keys
                                },
                            )
                            continue
                        blood_code_plan = blood_code_preparation.plan
                        break
                    if skipped_blood_slots:
                        _add_version_skip_count(version_skip_counts, "Blood Codes", skipped_blood_slots)
                        availability_warnings.append(
                            "Blood Code quest files left unchanged because their layout is incompatible with this "
                            "game version: " + ", ".join(sorted(set(skipped_blood_packages), key=str.casefold)) + "."
                        )
            if request.options.randomize_blood_codes and blood_code_catalog is not None and not blood_code_catalog.slots:
                availability_warnings.append("Blood Codes skipped: no grant Quest and Blood Code Article pair is available.")
            if request.options.randomize_boosters and booster_catalog is not None and booster_catalog.slots and inventory.contains(BOOSTER_TABLE_PACKAGE):
                if mappings is None:
                    availability_warnings.append("Boosters skipped: CV2.usmap mapping is unavailable.")
                else:
                  try:
                    booster_preparation = prepare_booster_plan(
                        booster_catalog,
                        seed,
                        game_root=game_root,
                        work_root=workspace.root,
                        mappings_path=mappings,
                        game_version=detection.version_id,
                        data_revision=detection.catalog_revision,
                        extractor=asset_extractor,
                        serializer=self.booster_serializer,
                        mix_postgame_dlc_with_base=request.options.mix_postgame_dlc_with_base,
                        available_reward_packages=source_availability.available_packages,
                        progress=progress,
                    )
                  except BoosterGenerationError as error:
                    _add_version_skip_count(
                        version_skip_counts,
                        "Boosters",
                        sum(inventory.contains(slot.booster_article_path) for slot in booster_catalog.slots),
                    )
                    source_availability = _mark_invalid_sources(
                        source_availability, "boosters", (BOOSTER_TABLE_PACKAGE,), str(error),
                    )
                    availability_warnings.append(f"Boosters skipped: source validation failed: {error}")
                  else:
                    booster_plan = booster_preparation.plan
            elif request.options.randomize_boosters and booster_catalog is not None:
                availability_warnings.append("Boosters skipped: mastery table package is absent.")
            if request.options.randomize_defensive_gadget_npc_rewards and defensive_gadget_catalog is not None and defensive_gadget_catalog.grants:
                if mappings is None:
                    availability_warnings.append("Defensive Gadget grants skipped: CV2.usmap mapping is unavailable.")
                else:
                    active_gadget_catalog = defensive_gadget_catalog
                    skipped_gadget_grants = 0
                    skipped_gadget_packages: list[str] = []
                    while active_gadget_catalog.grants:
                        try:
                            defensive_gadget_preparation = prepare_defensive_gadget_npc_plan(
                                active_gadget_catalog,
                                seed,
                                game_root=game_root,
                                work_root=workspace.root,
                                mappings_path=mappings,
                                game_version=detection.version_id,
                                data_revision=detection.catalog_revision,
                                extractor=asset_extractor,
                                serializer=self.defensive_gadget_serializer,
                                mix_postgame_dlc_with_base=request.options.mix_postgame_dlc_with_base,
                                reserved_reward_identities=reward_reservations.paths_for("defensive_gadget"),
                                source_occurrences=planned_reward_source_occurrences(world_plan, shop_plan),
                                progress=progress,
                            )
                            break
                        except DefensiveGadgetGenerationError as error:
                            message = str(error).casefold()
                            bad_packages = tuple(dict.fromkeys(
                                slot.package_path
                                for slot in active_gadget_catalog.grants
                                if slot.package_path.casefold() in message
                            ))
                            if not is_game_data_layout_mismatch(error) or not bad_packages:
                                skipped_gadget_grants += len(active_gadget_catalog.grants)
                                source_availability = _mark_invalid_sources(
                                    source_availability,
                                    "defensive_gadget_grants",
                                    (slot.package_path for slot in active_gadget_catalog.grants),
                                    str(error),
                                )
                                availability_warnings.append(
                                    f"Defensive Gadget grants skipped: source validation failed: {error}"
                                )
                                defensive_gadget_preparation = None
                                break
                            bad_keys = {package.casefold() for package in bad_packages}
                            remaining_grants = tuple(
                                slot for slot in active_gadget_catalog.grants
                                if slot.package_path.casefold() not in bad_keys
                            )
                            removed = len(active_gadget_catalog.grants) - len(remaining_grants)
                            if removed <= 0:
                                skipped_gadget_grants += len(active_gadget_catalog.grants)
                                defensive_gadget_preparation = None
                                break
                            skipped_gadget_grants += removed
                            skipped_gadget_packages.extend(bad_packages)
                            active_gadget_catalog = replace(active_gadget_catalog, grants=remaining_grants)
                    if skipped_gadget_grants:
                        _add_version_skip_count(
                            version_skip_counts, "Defensive Gadget Grants", skipped_gadget_grants,
                        )
                        if skipped_gadget_packages:
                            availability_warnings.append(
                                "Defensive Gadget quest files left unchanged because their layout is incompatible "
                                "with this game version: "
                                + ", ".join(sorted(set(skipped_gadget_packages), key=str.casefold)) + "."
                            )
                    if defensive_gadget_preparation is not None:
                        original_world_plan, original_shop_plan = world_plan, shop_plan
                        try:
                            for assignment in defensive_gadget_preparation.plan.assignments:
                                if not assignment.source_occurrence_id:
                                    raise DefensiveGadgetGenerationError(
                                        f"Defensive Gadget grant {assignment.destination.slot_id} has no owned source occurrence."
                                    )
                                world_plan, shop_plan = exchange_planned_reward_occurrence(
                                    world_plan,
                                    shop_plan,
                                    source_occurrence_id=assignment.source_occurrence_id,
                                    replacement_article_path=assignment.original.asset_path,
                                    replacement_occurrence_id=(
                                        f"defensive-gadget-npc:{assignment.destination.slot_id}"
                                    ),
                                    replacement_content_class=assignment.destination.content_class,
                                )
                        except (DefensiveGadgetGenerationError, OSError, ValueError) as error:
                            world_plan, shop_plan = original_world_plan, original_shop_plan
                            defensive_gadget_preparation = None
                            availability_warnings.append(
                                f"Defensive Gadget grants skipped: source occurrence transfer failed: {error}"
                            )
                        else:
                            defensive_gadget_plan = defensive_gadget_preparation.plan
            elif request.options.randomize_defensive_gadget_npc_rewards:
                availability_warnings.append("Defensive Gadget grants skipped: no grant source packages are available.")
            world_forma_destination_ids = {
                item.world_loot_destination_id
                for item in (bequeathed_formae_catalog.world_loot_direct_rewards
                             if bequeathed_formae_catalog is not None else ())
                if item.world_loot_destination_id is not None
            }
            world_forma_assignment_present = any(
                assignment.destination.destination_id in world_forma_destination_ids
                for assignment in world_plan.assignments
            )
            if (request.options.randomize_bequeathed_formae_quest_rewards
                    and bequeathed_formae_catalog is not None
                    and (len(bequeathed_formae_catalog.separate_direct_rewards) > 1
                         or world_forma_assignment_present)):
                if mappings is None:
                    availability_warnings.append("Bequeathed Forma quest rewards skipped: CV2.usmap mapping is unavailable.")
                else:
                  try:
                    bequeathed_formae_preparation = prepare_bequeathed_formae_plan(
                        bequeathed_formae_catalog,
                        seed,
                        game_root=game_root,
                        work_root=workspace.root,
                        mappings_path=mappings,
                        game_version=detection.version_id,
                        data_revision=detection.catalog_revision,
                        extractor=asset_extractor,
                        serializer=self.bequeathed_formae_serializer,
                        world_loot_plan=world_plan,
                        progress=progress,
                    )
                  except BequeathedFormaGenerationError as error:
                    if world_forma_assignment_present:
                        raise GenerationError(
                            f"Could not validate all alternative Bequeathed Forma quest paths: {error}"
                        ) from error
                    source_availability = _mark_invalid_sources(
                        source_availability, "bequeathed_formae_grants",
                        (grant.quest_asset for item in bequeathed_formae_catalog.separate_direct_rewards for grant in item.direct_grants),
                        str(error),
                    )
                    availability_warnings.append(f"Bequeathed Forma quest rewards skipped: source validation failed: {error}")
                  else:
                    bequeathed_formae_plan = bequeathed_formae_preparation.plan
            elif request.options.randomize_bequeathed_formae_quest_rewards:
                if bequeathed_formae_catalog is None or not bequeathed_formae_catalog.direct_rewards:
                    availability_warnings.append("Bequeathed Forma quest rewards skipped: no direct reward source package is available.")
            if linked_stinger_shop_required:
                if linked_stinger_shop_product is None:
                    raise GenerationError(
                        "The Josee Stinger shop alternate could not be validated; refusing to randomize its World Loot source alone."
                    )
                try:
                    linked_assignment = build_stinger_shop_mirror_assignment(
                        world_plan, linked_stinger_shop_product,
                    )
                except ValueError as error:
                    raise GenerationError(
                        f"Could not bind Josee's Stinger shop alternate to its World Loot reward: {error}"
                    ) from error
                if linked_assignment is not None:
                    shop_plan = replace(
                        shop_plan,
                        available_families=tuple(dict.fromkeys((
                            *shop_plan.available_families, STINGER_SHOP_FAMILY,
                        ))),
                        linked_assignments=(*shop_plan.linked_assignments, linked_assignment),
                        requires_name_map_preparation=True,
                    )
            if initial_inventory_plan is not None:
                starting_jail = initial_inventory_plan.assignment("starting_jail")
                if starting_jail.assigned_article_path.casefold() != OGRE_ARTICLE.casefold():
                    if linked_ogre_shop_product is None:
                        raise GenerationError(
                            "The starting Jail changed, but Jadwiga's audited Ogre mirror row is unavailable; "
                            "refusing to package an unlinked Jail mapping."
                        )
                    try:
                        linked_assignment = build_ogre_shop_mirror_assignment(
                            starting_jail.assigned_article_path,
                            linked_ogre_shop_product,
                        )
                    except ValueError as error:
                        raise GenerationError(
                            f"Could not bind Jadwiga's Jail row to the randomized starting Jail: {error}"
                        ) from error
                    if linked_assignment is not None:
                        shop_plan = replace(
                            shop_plan,
                            available_families=tuple(dict.fromkeys((
                                *shop_plan.available_families, OGRE_SHOP_FAMILY,
                            ))),
                            linked_assignments=(*shop_plan.linked_assignments, linked_assignment),
                            requires_name_map_preparation=True,
                        )
            reproducibility = build_reproducibility_metadata(
                seed=seed,
                game_version=detection.version_id,
                catalog_revision=detection.catalog_revision,
                options=request.options,
                content_availability=source_availability,
                source_cache_metadata=cache_snapshot.metadata if cache_snapshot is not None else None,
                world_plan=world_plan,
                shop_plan=shop_plan,
                enemy_drop_plan=enemy_drop_plan,
                blood_code_plan=blood_code_plan,
                booster_plan=booster_plan,
                extractor_plan=extractor_plan,
                initial_inventory_plan=initial_inventory_plan,
                defensive_gadget_plan=defensive_gadget_plan,
                bequeathed_formae_plan=bequeathed_formae_plan,
            )
            write_assignment_manifest(workspace.assignment_manifest, reproducibility)
            LOGGER.info(
                "Reproducibility: algorithm=v%s config=%s content=%s assignment=%s",
                reproducibility.algorithm_version,
                reproducibility.config_fingerprint,
                reproducibility.content_fingerprint,
                reproducibility.assignment_manifest_sha256,
            )
            if request.dry_run:
                spoiler = _write_spoiler(
                    workspace, request.options, world_plan, shop_plan, material_plan, enemy_drop_plan,
                    blood_code_plan, booster_plan, defensive_gadget_plan, bequeathed_formae_plan, extractor_plan,
                    initial_inventory_plan,
                )
                simple_entries = _simple_spoiler_entries(
                    world_plan, shop_plan, material_plan, enemy_drop_plan,
                    blood_code_plan, booster_plan, defensive_gadget_plan,
                    bequeathed_formae_plan, extractor_plan, initial_inventory_plan,
                )
                spoiler_text_path = write_spoiler(
                    workspace.spoiler_text,
                    simple_entries,
                    seed=canonical_seed_text(seed),
                    metadata=_spoiler_metadata(detection),
                    randomizer_settings=_spoiler_settings(request.options),
                    created_date=datetime.now().astimezone().date().isoformat(),
                    summary_lines=spoiler_assignment_summary(
                        simple_entries,
                        skipped_for_game_version=(
                            sum(version_skip_counts.values())
                        ),
                        skipped_by_category=version_skip_counts,
                    ),
                )
                _write_manifest(
                    workspace,
                    seed=seed,
                    options=request.options,
                    catalogs=catalogs_for_manifest,
                    game_version=detection.version_id,
                    catalog_revision=detection.catalog_revision,
                    world_plan=world_plan,
                    world_direct_grant_results=(),
                    shop_plan=shop_plan,
                    material_plan=material_plan,
                    enemy_drop_plan=enemy_drop_plan,
                    enemy_drop_result=None,
                    blood_code_plan=blood_code_plan,
                    blood_code_result=None,
                    booster_plan=booster_plan,
                    booster_result=None,
                    extractor_plan=extractor_plan,
                    extractor_result=None,
                    initial_inventory_plan=initial_inventory_plan,
                    initial_inventory_preparation=initial_inventory_preparation,
                    initial_inventory_result=None,
                    blood_code_mastery_result=None,
                    defensive_gadget_catalog=defensive_gadget_catalog,
                    defensive_gadget_plan=defensive_gadget_plan,
                    defensive_gadget_result=None,
                    bequeathed_formae_catalog=bequeathed_formae_catalog,
                    bequeathed_formae_plan=bequeathed_formae_plan,
                    bequeathed_formae_result=None,
                    jail_catalog=jail_catalog,
                    field_enemy_catalog=field_enemy_catalog,
                    output_files=(),
                    dry_run=True,
                    content_availability=source_availability,
                    availability_warnings=tuple(availability_warnings),
                    source_cache_metadata=cache_snapshot.metadata if cache_snapshot is not None else None,
                    spoiler_text_path=spoiler_text_path,
                    reproducibility=reproducibility,
                )
                success = True
                return GenerationResult(
                    workspace, seed, detection.version_id, detection.catalog_revision, True,
                    world_plan, shop_plan, material_preparation, None, None, None,
                    output_root, (), workspace.manifest, spoiler,
                    enemy_drop_plan=enemy_drop_plan,
                    blood_code_plan=blood_code_plan,
                    booster_plan=booster_plan,
                    defensive_gadget_plan=defensive_gadget_plan,
                    bequeathed_formae_plan=bequeathed_formae_plan,
                    extractor_plan=extractor_plan,
                    extractor_result=None,
                    initial_inventory_plan=initial_inventory_plan,
                    world_direct_grant_results=(),
                    content_availability=source_availability,
                    availability_warnings=tuple(availability_warnings),
                    spoiler_text_path=spoiler_text_path,
                    assignment_manifest_path=workspace.assignment_manifest,
                    config_fingerprint=reproducibility.config_fingerprint,
                    content_fingerprint=reproducibility.content_fingerprint,
                    assignment_manifest_sha256=reproducibility.assignment_manifest_sha256,
                )

            performance["planning_and_source_preparation_seconds"] = time.perf_counter() - generation_started
            if request.options.low_blood_code_mastery_exp:
                phase_started = time.perf_counter()
                try:
                    mastery_inventory = inventory
                    prepared_mastery_packages: tuple[str, ...] = ()
                    if cache_snapshot is not None and cache_snapshot.metadata is not None:
                        source_registry = cache_snapshot.metadata.get("source_registry", {})
                        if isinstance(source_registry, dict):
                            raw_packages = source_registry.get("blood_code_installed_articles", ())
                            if isinstance(raw_packages, (list, tuple)) and all(
                                isinstance(package, str) for package in raw_packages
                            ):
                                prepared_mastery_packages = tuple(raw_packages)
                        mastery_inventory = inventory_for_blood_code_mastery(
                            game_root=game_root,
                            cached_inventory=inventory,
                            prepared_packages=prepared_mastery_packages,
                            installed_extractor=self.extractor,
                        )
                    mastery_packages = blood_code_mastery_packages(mastery_inventory)
                    if not mastery_packages:
                        availability_warnings.append(
                            "Unrestricted Blood Code Mastery skipped: no installed Blood Code Articles were found."
                        )
                    elif mappings is None:
                        availability_warnings.append(
                            "Unrestricted Blood Code Mastery skipped: CV2.usmap is unavailable."
                        )
                    else:
                        mastery_extractor = (
                            asset_extractor
                            if all(inventory.contains(package) for package in mastery_packages)
                            else self.extractor
                        )
                        blood_code_mastery_result = apply_blood_code_mastery(
                            game_root=game_root,
                            work_root=workspace.root,
                            output_content_root=output_root,
                            mappings_path=mappings,
                            game_version=detection.version_id,
                            data_revision=detection.catalog_revision,
                            extractor=mastery_extractor,
                            package_inventory=mastery_inventory,
                            serializer=self.blood_code_mastery_serializer,
                            progress=progress,
                        )
                except BloodCodeMasteryError as error:
                    if is_game_data_layout_mismatch(error):
                        availability_warnings.append(
                            "Unrestricted Blood Code Mastery skipped: the installed Article data layout is "
                            f"incompatible ({error})."
                        )
                    else:
                        raise GenerationError(f"Could not set Blood Code mastery EXP: {error}") from error
                except (OSError, RuntimeError, ValueError) as error:
                    raise GenerationError(f"Could not inspect installed Blood Code Articles: {error}") from error
                performance["blood_code_mastery_patch_seconds"] = time.perf_counter() - phase_started
            if request.options.unlock_all_booster_slots or request.options.second_weapon_unlocked:
                phase_started = time.perf_counter()
                try:
                    booster_slot_inventory = inventory_for_booster_slot_flags(
                        game_root=game_root,
                        cached_inventory=inventory,
                        installed_extractor=self.extractor,
                    )
                    if not booster_slot_inventory.contains(BOOSTER_SLOT_FLAGS_PACKAGE):
                        availability_warnings.append(
                            "Equipment-slot unlocks skipped: the StoryFlag table is absent from this game version."
                        )
                    elif mappings is None:
                        availability_warnings.append(
                            "Equipment-slot unlocks skipped: CV2.usmap is unavailable."
                        )
                    else:
                        flag_table_extractor = (
                            asset_extractor
                            if inventory.contains(BOOSTER_SLOT_FLAGS_PACKAGE)
                            else self.extractor
                        )
                        booster_slot_unlock_result = apply_booster_slot_unlocks(
                            game_root=game_root,
                            work_root=workspace.root,
                            output_content_root=output_root,
                            mappings_path=mappings,
                            game_version=detection.version_id,
                            data_revision=detection.catalog_revision,
                            extractor=flag_table_extractor,
                            package_inventory=booster_slot_inventory,
                            serializer=self.booster_slot_unlock_serializer,
                            unlock_all_booster_slots=request.options.unlock_all_booster_slots,
                            unlock_second_weapon=request.options.second_weapon_unlocked,
                            progress=progress,
                        )
                except BoosterSlotUnlockError as error:
                    if is_game_data_layout_mismatch(error):
                        availability_warnings.append(
                            "Equipment-slot unlocks skipped: the installed StoryFlag table has an "
                            f"incompatible layout ({error})."
                        )
                    else:
                        raise GenerationError(f"Could not apply equipment-slot unlocks: {error}") from error
                except (OSError, RuntimeError, ValueError) as error:
                    raise GenerationError(
                        f"Could not inspect installed packages for equipment-slot flags: {error}"
                    ) from error
                performance["equipment_slot_unlock_patch_seconds"] = time.perf_counter() - phase_started
            if request.options.disable_tutorials:
                phase_started = time.perf_counter()
                try:
                    tutorial_inventory = inventory_for_tutorial_flags(
                        game_root=game_root,
                        cached_inventory=inventory,
                        installed_extractor=self.extractor,
                    )
                    available_tutorial_tables = tuple(
                        package for package in TUTORIAL_FLAG_TABLES
                        if tutorial_inventory.contains(package)
                    )
                    if BASE_TUTORIAL_FLAG_TABLE not in available_tutorial_tables:
                        availability_warnings.append(
                            "Tutorial pop-ups left unchanged: the base StoryFlag table is absent from this game version."
                        )
                    elif mappings is None:
                        availability_warnings.append(
                            "Tutorial pop-ups left unchanged: CV2.usmap is unavailable."
                        )
                    else:
                        tutorial_extractor = (
                            asset_extractor
                            if all(inventory.contains(package) for package in available_tutorial_tables)
                            else self.extractor
                        )
                        tutorial_disable_result = apply_tutorial_disable(
                            game_root=game_root,
                            work_root=workspace.root,
                            output_content_root=output_root,
                            mappings_path=mappings,
                            game_version=detection.version_id,
                            data_revision=detection.catalog_revision,
                            extractor=tutorial_extractor,
                            package_inventory=tutorial_inventory,
                            serializer=self.tutorial_disable_serializer,
                            table_packages=available_tutorial_tables,
                            progress=progress,
                        )
                except TutorialDisableError as error:
                    if is_game_data_layout_mismatch(error):
                        availability_warnings.append(
                            "Tutorial pop-ups left unchanged: the installed StoryFlag table has an "
                            f"incompatible layout ({error})."
                        )
                    else:
                        raise GenerationError(f"Could not disable tutorial pop-ups: {error}") from error
                except (OSError, RuntimeError, ValueError) as error:
                    raise GenerationError(f"Could not inspect tutorial StoryFlag tables: {error}") from error
                performance["tutorial_disable_patch_seconds"] = time.perf_counter() - phase_started
            if request.options.tap_to_skip_dialogue:
                phase_started = time.perf_counter()
                try:
                    dialogue_skip_result = apply_tap_to_skip_dialogue(
                        game_root=game_root,
                        work_root=workspace.root,
                        output_content_root=output_root,
                        mappings_path=mappings,
                        game_version=detection.version_id,
                        data_revision=detection.catalog_revision,
                        extractor=self.extractor,
                        serializer=self.dialogue_skip_serializer,
                        progress=progress,
                    )
                    if not dialogue_skip_result.applied:
                        availability_warnings.append(
                            "Tap Instead of Holding to Skip Dialogue was skipped for this game version: "
                            f"{dialogue_skip_result.reason or 'the asset layout is unsupported.'}"
                        )
                except Exception as error:
                    generation_error = _generation_error_cause(error)
                    if generation_error is not None:
                        raise generation_error
                    LOGGER.warning(
                        "Tap Instead of Holding to Skip Dialogue was skipped after patch validation failed: %s",
                        error,
                        exc_info=True,
                    )
                    dialogue_skip_result = DialogueSkipResult((), False, str(error))
                    availability_warnings.append(
                        "Tap Instead of Holding to Skip Dialogue was skipped because its assets could not be validated: "
                        f"{error}"
                    )
                performance["dialogue_skip_patch_seconds"] = time.perf_counter() - phase_started
            if shop_stage is not None and (
                shop_plan.assignments or shop_plan.linked_assignments
                or request.options.unlock_npc_trader_shops
            ):
                if progress:
                    progress("patching-shops", 0, len(shop_stage.tables))
                shop_result = apply_shop_plan(
                    shop_stage,
                    shop_plan,
                    catalogs,
                    output_content_root=output_root,
                    mappings_path=mappings,
                    progress=progress,
                    unlock_npc_trader_shops=request.options.unlock_npc_trader_shops,
                )
            if world_plan.assignments:
                phase_started = time.perf_counter()
                world_result = apply_world_loot_plan(
                    world_plan,
                    game_root=game_root,
                    work_root=workspace.root,
                    output_content_root=output_root,
                    mappings_path=mappings,
                    game_version=detection.version_id,
                    data_revision=detection.catalog_revision,
                    resolver=self.world_loot_resolver,
                    extractor=asset_extractor,
                    dependency_index=(
                        cache_snapshot.metadata.get("world_loot_dependency_index")
                        if cache_snapshot is not None and cache_snapshot.metadata is not None
                        else None
                    ),
                    serializer=self.world_loot_serializer,
                    progress=progress,
                )
                performance["world_loot_map_patch_seconds"] = time.perf_counter() - phase_started
            if material_preparation is not None and material_preparation.plan.assignments:
                material_result = apply_material_plan(
                    material_preparation,
                    game_root=game_root,
                    work_root=workspace.root,
                    output_content_root=output_root,
                    mappings_path=mappings,
                    game_version=detection.version_id,
                    data_revision=detection.catalog_revision,
                    resolver=self.material_resolver,
                    extractor=asset_extractor,
                    serializer=self.material_serializer,
                    progress=progress,
                )
            if enemy_drop_preparation is not None:
                try:
                    enemy_drop_result = apply_enemy_drop_plan(
                        enemy_drop_preparation,
                        work_root=workspace.root,
                        output_content_root=output_root,
                        mappings_path=mappings,
                        serializer=self.enemy_drop_serializer,
                        progress=progress,
                    )
                except EnemyDropGenerationError as error:
                    raise GenerationError(f"Could not patch Enemy Drop tables: {error}") from error
            if blood_code_preparation is not None:
                try:
                    blood_code_result = apply_blood_code_plan(
                        blood_code_preparation,
                        work_root=workspace.root,
                        output_content_root=output_root,
                        mappings_path=mappings,
                        serializer=self.blood_code_serializer,
                        progress=progress,
                    )
                except BloodCodeGenerationError as error:
                    raise GenerationError(f"Could not patch Blood Code grants: {error}") from error
            if booster_preparation is not None:
                try:
                    booster_result = apply_booster_plan(
                        booster_preparation,
                        work_root=workspace.root,
                        output_content_root=output_root,
                        mappings_path=mappings,
                        serializer=self.booster_serializer,
                        progress=progress,
                    )
                except BoosterGenerationError as error:
                    raise GenerationError(f"Could not patch Booster mastery table: {error}") from error
            if extractor_preparation is not None:
                try:
                    extractor_result = apply_extractor_plan(
                        extractor_preparation,
                        work_root=workspace.root,
                        output_content_root=output_root,
                        mappings_path=mappings,
                        serializer=self.extractor_serializer,
                        one_material_each=request.options.cheaper_booster_extractor,
                        progress=progress,
                    )
                except ExtractorGenerationError as error:
                    raise GenerationError(f"Could not patch Booster Extractor recipes: {error}") from error
            if initial_inventory_preparation is not None:
                try:
                    initial_inventory_result = apply_initial_inventory_plan(
                        initial_inventory_preparation,
                        work_root=workspace.root,
                        output_content_root=output_root,
                        mappings_path=mappings,
                        serializer=self.initial_inventory_serializer,
                        progress=progress,
                    )
                except InitialInventoryGenerationError as error:
                    raise GenerationError(f"Could not patch starting Initial Inventory: {error}") from error
            if defensive_gadget_preparation is not None:
                try:
                    defensive_gadget_result = apply_defensive_gadget_npc_plan(
                        defensive_gadget_preparation,
                        work_root=workspace.root,
                        output_content_root=output_root,
                        mappings_path=mappings,
                        serializer=self.defensive_gadget_serializer,
                        progress=progress,
                    )
                except DefensiveGadgetGenerationError as error:
                    raise GenerationError(f"Could not patch Defensive Gadget NPC grants: {error}") from error
            if bequeathed_formae_preparation is not None:
                try:
                    bequeathed_formae_result = apply_bequeathed_formae_plan(
                        bequeathed_formae_preparation,
                        work_root=workspace.root,
                        output_content_root=output_root,
                        mappings_path=mappings,
                        serializer=self.bequeathed_formae_serializer,
                        progress=progress,
                    )
                except BequeathedFormaGenerationError as error:
                    raise GenerationError(f"Could not patch Bequeathed Forma quest rewards: {error}") from error
            if world_plan.assignments:
                phase_started = time.perf_counter()
                try:
                    world_direct_grant_results = apply_world_loot_direct_grants(
                        world_plan,
                        game_root=game_root,
                        work_root=workspace.root,
                        output_content_root=output_root,
                        mappings_path=mappings,
                        game_version=detection.version_id,
                        data_revision=detection.catalog_revision,
                        extractor=asset_extractor,
                        serializer=self.raw_article_grant_serializer,
                        quest_serializer=self.world_quest_article_serializer,
                        validated_quest_destination_ids=(
                            field_enemy_catalog.destination_ids if field_enemy_catalog is not None else ()
                        ),
                        compatibility_profile=detection.world_loot_compatibility_profile,
                        progress=progress,
                    )
                except RawArticleGrantGenerationError as error:
                    raise GenerationError(f"Could not patch direct World Loot rewards: {error}") from error
                performance["direct_world_loot_rewards_seconds"] = time.perf_counter() - phase_started
            phase_started = time.perf_counter()
            _copy_world_outputs(
                world_result,
                intermediate_content_root=output_root,
                output_content_root=output_root,
                material_result=material_result,
            )
            world_result = _remap_world_result(
                world_result,
                source_content_root=workspace.root / "source" / "Content",
                intermediate_content_root=output_root,
                output_content_root=output_root,
            )
            performance["world_output_copy_and_remap_seconds"] = time.perf_counter() - phase_started
            phase_started = time.perf_counter()
            expected_outputs = _expected_output_files(
                world_result, shop_result, material_result, enemy_drop_result, blood_code_result, booster_result,
                defensive_gadget_result, bequeathed_formae_result, world_direct_grant_results, extractor_result,
                initial_inventory_result, booster_slot_unlock_result,
                motorcycle_unlock_result,
                tutorial_disable_result,
                blood_code_mastery_result,
                dialogue_skip=dialogue_skip_result,
            )
            changed_package_registry = _changed_package_registry(
                output_root,
                world=world_result,
                shops=shop_result,
                materials=material_result,
                enemy_drops=enemy_drop_result,
                blood_codes=blood_code_result,
                boosters=booster_result,
                defensive_gadgets=defensive_gadget_result,
                bequeathed_formae=bequeathed_formae_result,
                direct_grants=world_direct_grant_results,
                extractor=extractor_result,
                initial_inventory=initial_inventory_result,
                booster_slot_unlock=booster_slot_unlock_result,
                motorcycle_unlock=motorcycle_unlock_result,
                tutorial_disable=tutorial_disable_result,
                blood_code_mastery=blood_code_mastery_result,
                dialogue_skip=dialogue_skip_result,
            )
            registry_paths = {
                (output_root / relative).resolve()
                for package in changed_package_registry
                for relative in package["files"]
            }
            if registry_paths != {path.resolve() for path in expected_outputs}:
                raise GenerationError("Changed-package registry does not match the subsystem output inventory.")
            performance["changed_package_registry_seconds"] = time.perf_counter() - phase_started
            phase_started = time.perf_counter()
            output_files = _validate_output_tree(output_root, expected_outputs)
            performance["staged_inventory_validation_seconds"] = time.perf_counter() - phase_started
            if blood_code_result is not None:
                phase_started = time.perf_counter()
                final_blood_code_validation = _validate_final_blood_code_artifact(
                    blood_code_result,
                    output_files=output_files,
                    output_content_root=output_root,
                    work_root=workspace.root,
                    mappings_path=mappings,
                    serializer=self.blood_code_serializer,
                )
                performance["blood_code_final_staged_validation_seconds"] = time.perf_counter() - phase_started
                performance["blood_code_final_logical_assignments_validated"] = float(
                    final_blood_code_validation["logical_assignments"]
                )
                performance["blood_code_final_packages_validated"] = float(
                    final_blood_code_validation["packages"]
                )
            if output_files:
                if progress:
                    progress("packaging-mod", 0, 1)
                phase_started = time.perf_counter()
                package_result = self.packager.package(
                    output_root,
                    work_root=workspace.root,
                    output_directory=workspace.packaged,
                    input_files=output_files,
                )
                performance["retoc_package_total_seconds"] = time.perf_counter() - phase_started
                if progress:
                    progress("packaging-mod", 1, 1)
            phase_started = time.perf_counter()
            spoiler = _write_spoiler(
                workspace, request.options, world_plan, shop_plan, material_plan, enemy_drop_plan,
                blood_code_plan, booster_plan, defensive_gadget_plan, bequeathed_formae_plan, extractor_plan,
                initial_inventory_plan,
            )
            simple_entries = _simple_spoiler_entries(
                world_plan, shop_plan, material_plan, enemy_drop_plan,
                blood_code_plan, booster_plan, defensive_gadget_plan,
                bequeathed_formae_plan, extractor_plan, initial_inventory_plan,
            )
            spoiler_text_path = write_spoiler(
                workspace.spoiler_text,
                simple_entries,
                seed=canonical_seed_text(seed),
                metadata=_spoiler_metadata(detection),
                randomizer_settings=_spoiler_settings(request.options, dialogue_skip_result),
                created_date=datetime.now().astimezone().date().isoformat(),
                summary_lines=spoiler_assignment_summary(
                    simple_entries,
                    skipped_for_game_version=(
                        sum(version_skip_counts.values())
                    ),
                    skipped_by_category=version_skip_counts,
                ),
            )
            performance["spoiler_generation_seconds"] = time.perf_counter() - phase_started
            phase_started = time.perf_counter()
            _write_manifest(
                workspace,
                seed=seed,
                options=request.options,
                catalogs=catalogs_for_manifest,
                game_version=detection.version_id,
                catalog_revision=detection.catalog_revision,
                world_plan=world_plan,
                world_direct_grant_results=world_direct_grant_results,
                shop_plan=shop_plan,
                material_plan=material_plan,
                enemy_drop_plan=enemy_drop_plan,
                enemy_drop_result=enemy_drop_result,
                blood_code_plan=blood_code_plan,
                blood_code_result=blood_code_result,
                booster_plan=booster_plan,
                booster_result=booster_result,
                extractor_plan=extractor_plan,
                extractor_result=extractor_result,
                initial_inventory_plan=initial_inventory_plan,
                initial_inventory_preparation=initial_inventory_preparation,
                initial_inventory_result=initial_inventory_result,
                motorcycle_unlock_result=motorcycle_unlock_result,
                tutorial_disable_result=tutorial_disable_result,
                blood_code_mastery_result=blood_code_mastery_result,
                defensive_gadget_catalog=defensive_gadget_catalog,
                defensive_gadget_plan=defensive_gadget_plan,
                defensive_gadget_result=defensive_gadget_result,
                bequeathed_formae_catalog=bequeathed_formae_catalog,
                bequeathed_formae_plan=bequeathed_formae_plan,
                bequeathed_formae_result=bequeathed_formae_result,
                jail_catalog=jail_catalog,
                field_enemy_catalog=field_enemy_catalog,
                output_files=output_files,
                dry_run=False,
                package_result=package_result,
                content_availability=source_availability,
                availability_warnings=tuple(availability_warnings),
                source_cache_metadata=cache_snapshot.metadata if cache_snapshot is not None else None,
                spoiler_text_path=spoiler_text_path,
                reproducibility=reproducibility,
            )
            performance["manifest_generation_seconds"] = time.perf_counter() - phase_started
            performance["total_generation_seconds"] = time.perf_counter() - generation_started
            performance["changed_package_count"] = float(len(changed_package_registry))
            performance["staged_file_count"] = float(len(output_files))
            _write_generation_performance_report(workspace, performance, changed_package_registry)
            if request.options.install_after_generation and package_result is not None:
                if progress:
                    progress("installing-mod", 0, 1)
                installation_result = self.installer.install(
                    package_result,
                    game_root=game_root,
                    spoiler_path=spoiler_text_path,
                    allow_unowned_replacement=request.replace_existing_randomizer,
                )
                try:
                    _write_manifest(
                        workspace, seed=seed, options=request.options,
                        catalogs=catalogs_for_manifest,
                        game_version=detection.version_id, catalog_revision=detection.catalog_revision,
                        world_plan=world_plan, shop_plan=shop_plan, material_plan=material_plan,
                        world_direct_grant_results=world_direct_grant_results,
                        enemy_drop_plan=enemy_drop_plan, enemy_drop_result=enemy_drop_result,
                        blood_code_plan=blood_code_plan, blood_code_result=blood_code_result,
                        booster_plan=booster_plan, booster_result=booster_result,
                        extractor_plan=extractor_plan, extractor_result=extractor_result,
                        initial_inventory_plan=initial_inventory_plan,
                        initial_inventory_preparation=initial_inventory_preparation,
                        initial_inventory_result=initial_inventory_result,
                        motorcycle_unlock_result=motorcycle_unlock_result,
                        tutorial_disable_result=tutorial_disable_result,
                        blood_code_mastery_result=blood_code_mastery_result,
                        defensive_gadget_catalog=defensive_gadget_catalog,
                        defensive_gadget_plan=defensive_gadget_plan,
                        defensive_gadget_result=defensive_gadget_result,
                        bequeathed_formae_catalog=bequeathed_formae_catalog,
                        bequeathed_formae_plan=bequeathed_formae_plan,
                        bequeathed_formae_result=bequeathed_formae_result,
                        jail_catalog=jail_catalog,
                        field_enemy_catalog=field_enemy_catalog,
                        output_files=output_files, dry_run=False, package_result=package_result,
                        installation_result=installation_result,
                        content_availability=source_availability,
                        availability_warnings=tuple(availability_warnings),
                        source_cache_metadata=cache_snapshot.metadata if cache_snapshot is not None else None,
                        spoiler_text_path=spoiler_text_path,
                        reproducibility=reproducibility,
                    )
                except Exception as error:
                    warning_path = workspace.logs / "manifest_refresh_warning.txt"
                    try:
                        warning_path.write_text(
                            f"Mod installed, but manifest status could not be refreshed: {error}\n",
                            encoding="utf-8",
                        )
                    except OSError:
                        LOGGER.debug(
                            "Could not persist the installed-manifest refresh warning.",
                            exc_info=True,
                        )
            elif request.options.install_after_generation and output_files:
                raise GenerationError("Validated staged files were not packaged and cannot be installed.")
            if installation_result is not None and progress:
                try:
                    progress("installing-mod", 1, 1)
                except Exception:
                    LOGGER.debug("Progress callback failed after install completed.", exc_info=True)
            cleanup_report: GenerationCleanupReport | None = None
            if request.options.install_after_generation:
                if progress:
                    progress("cleaning-generation-files", 0, 1)
                cleanup_report = self.workspace_manager.cleanup_successful_generation(
                    workspace,
                    spoiler_text_path,
                    assignment_manifest_source=workspace.assignment_manifest,
                )
                if cleanup_report.retained_spoiler is not None:
                    spoiler_text_path = cleanup_report.retained_spoiler
                elif installation_result is not None:
                    installed_spoiler = next(
                        (path for path in installation_result.files if path.name == "Spoiler.txt"),
                        None,
                    )
                    if installed_spoiler is not None and installed_spoiler.is_file():
                        spoiler_text_path = installed_spoiler
                else:
                    # An empty generation has no mod installation to hold the
                    # spoiler, so do not leave a second copy under app output.
                    spoiler_text_path = None
                if cleanup_report.retained_assignment_manifest is not None:
                    assignment_manifest_path = cleanup_report.retained_assignment_manifest
                else:
                    assignment_manifest_path = workspace.assignment_manifest
                for warning in cleanup_report.warnings:
                    LOGGER.warning("Successful generation cleanup was incomplete: %s", warning)
                if progress:
                    try:
                        progress("cleaning-generation-files", 1, 1)
                    except Exception:
                        LOGGER.debug("Progress callback failed after cleanup completed.", exc_info=True)
            else:
                assignment_manifest_path = workspace.assignment_manifest
            success = True
            return GenerationResult(
                workspace, seed, detection.version_id, detection.catalog_revision, False,
                world_plan, shop_plan, material_preparation, world_result, shop_result,
                material_result, output_root, output_files, workspace.manifest, spoiler,
                package_result, installation_result,
                enemy_drop_plan, enemy_drop_result,
                blood_code_plan, blood_code_result, booster_plan, booster_result,
                defensive_gadget_plan, defensive_gadget_result,
                bequeathed_formae_plan, bequeathed_formae_result,
                extractor_plan=extractor_plan,
                extractor_result=extractor_result,
                initial_inventory_plan=initial_inventory_plan,
                initial_inventory_result=initial_inventory_result,
                motorcycle_unlock_result=motorcycle_unlock_result,
                tutorial_disable_result=tutorial_disable_result,
                dialogue_skip_result=dialogue_skip_result,
                blood_code_mastery_result=blood_code_mastery_result,
                world_direct_grant_results=world_direct_grant_results,
                content_availability=source_availability,
                availability_warnings=tuple(availability_warnings),
                spoiler_text_path=spoiler_text_path,
                cleanup_report=cleanup_report,
                assignment_manifest_path=assignment_manifest_path,
                config_fingerprint=reproducibility.config_fingerprint,
                content_fingerprint=reproducibility.content_fingerprint,
                assignment_manifest_sha256=reproducibility.assignment_manifest_sha256,
            )
        except Exception as error:
            failure_message = f"Generation failed: {error}"
            if isinstance(error, GenerationError):
                raise
            raise GenerationError(failure_message) from error
        finally:
            if not success:
                _clear_output_tree(output_root, workspace.root)
                if package_result is not None:
                    _remove_package_outputs(package_result.files, workspace.packaged)
                for metadata_path in (workspace.manifest, workspace.spoiler_log, workspace.spoiler_text):
                    try:
                        metadata_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    workspace.assignment_manifest.unlink(missing_ok=True)
                except OSError:
                    pass
                failure_path = workspace.logs / "generation_failure.txt"
                try:
                    failure_path.write_text(failure_message + "\n", encoding="utf-8")
                except OSError:
                    pass

# List enabled Shop families
def _enabled_shop_families(
    options: GenerationOptions,
    products: Iterable = (),
) -> tuple[str, ...]:
    """Return catalogued non-Trader Shop families; package availability is resolved separately."""
    if not options.randomize_shops:
        return ()
    present = {
        product.family.upper()
        for product in products
        if not is_npc_trader_row(product.family, product.shop_row)
    }
    return tuple(family for family in ("NORMAL", "DLC", "LUXURY", "TRADE", "TRADE_DLC") if family in present)


# List enabled NPC Trader families
def _enabled_npc_trader_families(options: GenerationOptions, products: Iterable) -> tuple[str, ...]:
    if not options.randomize_npc_traders:
        return ()
    present = {
        product.family.upper()
        for product in products
        if is_npc_trader_row(product.family, product.shop_row) and not product.protected
    }
    return tuple(family for family in ("TRADE", "TRADE_DLC") if family in present)


# Summarize the NPC Trader plan
def _npc_trader_plan_summary(
    *,
    mode: str,
    parsed_products: tuple,
    plan: ShopRandomizationPlan,
    world_plan: WorldLootRandomizationPlan,
    requested_families: tuple[str, ...],
    unavailable_families: tuple[str, ...],
) -> NpcTraderPlanSummary:
    assignments = tuple(
        assignment for assignment in plan.assignments
        if is_npc_trader_row(assignment.destination.family, assignment.destination.shop_row)
    )
    locked = tuple(
        product for product in plan.locked_slots
        if is_npc_trader_row(product.family, product.shop_row)
    )
    unvalidated = tuple(
        product for product in plan.unvalidated_slots
        if is_npc_trader_row(product.family, product.shop_row)
    )
    reserved = tuple(
        product for product in plan.extractor_reserved_slots
        if is_npc_trader_row(product.family, product.shop_row)
    )
    cross_companion = 0
    trader_from_world = 0
    trader_from_shop = 0
    shop_from_trader = 0
    for assignment in plan.assignments:
        destination_is_trader = is_npc_trader_row(
            assignment.destination.family, assignment.destination.shop_row,
        )
        source_product = (
            assignment.source.source_product
            if isinstance(assignment.source, ArticleRewardIdentity)
            else assignment.source
        )
        source_is_trader = (
            source_product is not None
            and is_npc_trader_row(source_product.family, source_product.shop_row)
        )
        if destination_is_trader:
            if isinstance(assignment.source, ArticleRewardIdentity):
                if assignment.source.origin_kind == "WORLD":
                    trader_from_world += 1
                elif source_product is not None and not source_is_trader:
                    trader_from_shop += 1
            if source_is_trader and source_product.shop_row != assignment.destination.shop_row:
                cross_companion += 1
        elif source_is_trader and assignment.article_only:
            shop_from_trader += 1

    world_from_trader = 0
    for assignment in world_plan.assignments:
        candidate = assignment.source_candidate
        candidate_id = candidate.candidate_id if candidate is not None else ""
        prefix = "global-article:shop:"
        if not candidate_id.startswith(prefix):
            continue
        identity = candidate_id[len(prefix):]
        parts = identity.split(":", 2)
        if len(parts) == 3 and is_npc_trader_row(parts[0], parts[1]):
            world_from_trader += 1

    rows = tuple(sorted({product.shop_row for product in parsed_products}))
    available_trader_families = tuple(
        family for family in ("TRADE", "TRADE_DLC")
        if any(product.family.upper() == family for product in parsed_products)
    )
    eligible_count = len(assignments)
    # Keep the requested table inventory in diagnostics even when a family is
    # absent from the installed package inventory; parse failures are recorded
    # separately as invalid source packages by ContentAvailability.
    unavailable = tuple(family for family in requested_families if family in unavailable_families)
    return NpcTraderPlanSummary(
        mode=mode,
        available_families=available_trader_families,
        unavailable_families=unavailable,
        available_rows=rows,
        available_slots=len(parsed_products),
        eligible_slots=eligible_count,
        assignments=assignments,
        locked_slots=locked,
        unvalidated_slots=unvalidated,
        reserved_slots=reserved,
        cross_companion_assignments=cross_companion,
        trader_from_world=trader_from_world,
        world_from_trader=world_from_trader,
        trader_from_shop=trader_from_shop,
        shop_from_trader=shop_from_trader,
        disabled_slots=tuple(product for product in plan.disabled_slots
                             if is_npc_trader_row(product.family, product.shop_row)),
    )


# Remove reserved rewards from the pool
def _without_reserved_rewards(catalog, reservations: RewardReservationSet, category: str):
    """Return the canonical candidate view with this category's allocations removed."""
    return replace(
        catalog,
        items=tuple(
            item for item in catalog.items
            if not reservations.contains(category, item.asset_path)
        ),
    )


# Find available World Loot locations
def _available_world_locations(
    locations: Iterable[WorldLootLocation], inventory: PackageInventory,
) -> tuple[WorldLootLocation, ...]:
    """Keep catalogued World WHEREs whose owning packages are available."""
    return tuple(
        item for item in locations
        if inventory.contains(item.package_path)
        and (
            item.destination_kind != "BLUEPRINT_CDO_ARTICLE_BULK"
            or all(inventory.contains(parent) for parent in item.parent_blueprint_packages)
        )
        and (
            item.instance_override_source is None
            or (
                inventory.contains(item.instance_override_source.cdo_package)
                and all(inventory.contains(parent) for parent in item.instance_override_source.parent_blueprint_packages)
            )
        )
        and (
            item.location_type not in {
                "RENEWABLE_INGREDIENT", "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
            }
            or (item.structural_class_package is not None and inventory.contains(item.structural_class_package))
        )
    )


# Find available material locations
def _available_material_locations(
    locations: Iterable[MaterialLocation], inventory: PackageInventory,
) -> tuple[MaterialLocation, ...]:
    """Require installed map/class/Article sources and keep mirror pairs together."""
    available = tuple(
        item for item in locations
        if inventory.contains(item.package_path)
        and inventory.contains(item.resource_class_package)
        and inventory.contains(_package_from_asset_path(item.reward.article_path))
    )
    by_slot = {(item.package_path.casefold(), item.export_index): item for item in available}
    return tuple(
        item for item in available
        if item.mirror_of_package_path is None
        or (
            item.mirror_of_export_index is not None
            and (item.mirror_of_package_path.casefold(), item.mirror_of_export_index) in by_slot
        )
    )


# List available Shop families
def _available_shop_families(
    requested_families: Iterable[str],
    products: Iterable[ShopProduct],
    inventory: PackageInventory,
    *,
    include_protected_families: Iterable[str] = (),
) -> tuple[str, ...]:
    """Select each requested Shop table independently by package presence."""
    protected_included = {family.upper() for family in include_protected_families}
    safe_families = {
        product.family.upper() for product in products if not product.protected
    }
    return tuple(
        family for family in requested_families
        if (family in safe_families or family in protected_included)
        and inventory.contains(f"/Game/Blueprint/GameSystem/Shop/{SHOP_TABLE_NAMES[family]}")
    )


# Build package from asset path
def _package_from_asset_path(value: str) -> str:
    """Convert an Unreal object reference to its package path."""
    package = value.split("::", 1)[0].split(".", 1)[0].strip()
    if not package.startswith("/Game/"):
        raise ValueError(f"Expected a /Game asset path, got {value!r}.")
    return package


# Mark invalid reward sources
def _mark_invalid_sources(
    availability: ContentAvailability,
    subsystem: str,
    package_paths: Iterable[str],
    reason: str,
) -> ContentAvailability:
    """Record listed sources that failed extraction or structural validation."""
    result = availability
    for package in sorted(set(package_paths), key=str.casefold):
        result = result.with_invalid(package, reason, subsystem=subsystem)
    return result


# Build world loot location key
def _world_loot_location_key(package_path: str, export_index: int) -> str:
    return f"{package_path.casefold()}#{export_index}"


# Resolve world reward FName identity
def _world_reward_fname_identity(location, reward_index: int) -> dict[str, Any] | None:
    reward = location.rewards[reward_index]
    identity = reward.soft_object_path_identity
    if identity is not None:
        return {
            "package_name": {
                "value": identity.package_name.value,
                "number": identity.package_name.number,
            },
            "asset_name": {
                "value": identity.asset_name.value,
                "number": identity.asset_name.number,
            },
        }
    if reward_index == 0 and location.raw_article_layout is not None:
        layout = location.raw_article_layout
        return {
            "package_name": {
                "value": layout.expected_path_name_map_value,
                "number": layout.expected_vanilla_number,
            },
            "asset_name": {
                "value": layout.expected_object_name_map_value,
                "number": layout.expected_vanilla_number,
            },
        }
    quest_layout = next((
        layout for layout in location.quest_grant_layouts if layout.reward_index == reward_index
    ), None)
    if quest_layout is not None:
        layout = quest_layout
        return {
            "package_name": {
                "value": layout.expected_package_name_value,
                "number": layout.expected_package_name_number,
            },
            "asset_name": {
                "value": layout.expected_asset_name_value,
                "number": layout.expected_asset_name_number,
            },
        }
    return None


# Validate world loot prepared descriptors
def _incompatible_world_loot_map_packages(dependency_index: object) -> frozenset[str]:
    """Read map packages with structurally inconsistent export data."""
    if dependency_index is None:
        return frozenset()
    if not isinstance(dependency_index, dict):
        raise GenerationError("Extracted World Loot dependency index is invalid.")
    rows = dependency_index.get("incompatible_world_loot_maps", [])
    if not isinstance(rows, list):
        raise GenerationError("Extracted World Loot compatibility records are invalid.")
    packages: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise GenerationError("Extracted World Loot compatibility record is invalid.")
        package = row.get("package_path")
        if not isinstance(package, str) or not is_world_map_package(package):
            raise GenerationError("Extracted World Loot compatibility record has an invalid map package.")
        packages.add(package.casefold())
    return frozenset(packages)


# Read mismatched World Loot exports
def _incompatible_world_loot_location_keys(dependency_index: object) -> frozenset[str]:
    """Read individual World Loot exports that failed catalog checks."""
    if dependency_index is None:
        return frozenset()
    if not isinstance(dependency_index, dict):
        raise GenerationError("Extracted World Loot dependency index is invalid.")
    rows = dependency_index.get("incompatible_world_loot_locations", [])
    if not isinstance(rows, list):
        raise GenerationError("Extracted World Loot location compatibility records are invalid.")
    keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise GenerationError("Extracted World Loot location compatibility record is invalid.")
        package = row.get("package_path")
        export_index = row.get("export_index")
        if (
            not isinstance(package, str) or not is_world_map_package(package)
            or not isinstance(export_index, int) or isinstance(export_index, bool) or export_index < 1
        ):
            raise GenerationError("Extracted World Loot location compatibility record is invalid.")
        keys.add(_world_loot_location_key(package, export_index))
    return frozenset(keys)


# Filter incompatible map locations
def _compatible_world_loot_locations(
    locations: Iterable[WorldLootLocation], dependency_index: object,
) -> tuple[WorldLootLocation, ...]:
    """Keep only World Loot exports verified against this game build."""
    unsupported = _incompatible_world_loot_map_packages(dependency_index)
    unsupported_exports = _incompatible_world_loot_location_keys(dependency_index)
    return tuple(
        location for location in locations
        if (
            location.package_path.casefold() not in unsupported
            and _world_loot_location_key(location.package_path, location.export_index) not in unsupported_exports
        )
    )


# Classify a skipped destination
def _version_skip_category(location: WorldLootLocation, options: GenerationOptions) -> str | None:
    if location.mirror_of_package_path is not None or location.protected or location.protection_reasons:
        return None
    if location.location_type == "RENEWABLE_INGREDIENT":
        return "Ingredients" if options.randomize_materials else None
    if location.location_type in {
        "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
    }:
        return "Healing Items" if options.randomize_world_loot and options.randomize_healing else None
    if not options.randomize_world_loot:
        return None
    if location.destination_kind == "QUEST_ARTICLE_GRANT":
        return "Quest Rewards"
    return "World Loot"


# Add a compatibility skip count
def _add_version_skip_count(counts: dict[str, int], category: str, count: int) -> None:
    if not isinstance(category, str) or not category.strip():
        raise ValueError("Game-version skip category must be a non-empty string.")
    if type(count) is not int or count < 0:
        raise ValueError("Game-version skip counts must be non-negative integers.")
    if count:
        counts[category] = counts.get(category, 0) + count


# Check finite shop catalog offer
def _is_finite_shop_catalog_offer(product: object) -> bool:
    protected = getattr(product, "protected", False)
    uses_stock = getattr(product, "uses_stock", True)
    stock = getattr(product, "stock", 1)
    return protected is False and uses_stock is True and type(stock) is int and stock > 0


# Validate world loot prepared descriptors
def _validate_world_loot_prepared_descriptors(
    locations,
    dependency_index: dict[str, Any],
    inventory: PackageInventory,
) -> None:
    """Require prepared map descriptors; direct Article grants are validated by their patch services."""
    descriptors = dependency_index.get("world_loot_location_descriptors")
    if not isinstance(descriptors, dict):
        raise GenerationError("Extracted cache has no prepared World Loot location descriptors.")
    direct_grant_kinds = {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}
    map_locations = tuple(
        location for location in locations if location.destination_kind not in direct_grant_kinds
    )
    map_descriptors = {
        key: descriptor
        for key, descriptor in descriptors.items()
        if not (
            isinstance(descriptor, dict)
            and descriptor.get("destination_kind") in direct_grant_kinds
        )
    }
    expected_keys = {
        _world_loot_location_key(location.package_path, location.export_index)
        for location in map_locations
    }
    if set(map_descriptors) != expected_keys:
        missing = sorted(expected_keys - set(map_descriptors))[:5]
        extra = sorted(set(map_descriptors) - expected_keys)[:5]
        raise GenerationError(
            "Prepared World Loot descriptor identities differ from the active catalog; "
            f"missing={missing}, extra={extra}. Re-extract game data."
        )
    for location in map_locations:
        key = _world_loot_location_key(location.package_path, location.export_index)
        descriptor = map_descriptors.get(key)
        if not isinstance(descriptor, dict):
            raise GenerationError(f"Prepared World Loot descriptor is invalid: {key}.")
        expected_rewards = []
        for reward_index, reward in enumerate(location.rewards):
            expected_rewards.append({
                "reward_index": reward_index,
                "article_path": reward.article_path,
                "count": reward.count,
                "notification": reward.notification,
                "soft_object_path_identity": _world_reward_fname_identity(location, reward_index),
                "article_package_available": inventory.contains(reward.article_path),
            })
        if (
            descriptor.get("package_path") != location.package_path
            or descriptor.get("export_index") != location.export_index
            or descriptor.get("mirror_of_package_path") != location.mirror_of_package_path
            or descriptor.get("mirror_of_export_index") != location.mirror_of_export_index
            or descriptor.get("export_name") != location.export_name
            or descriptor.get("actor_class") != location.actor_class
            or descriptor.get("actor_category") != location.actor_category
            or descriptor.get("destination_kind") != location.destination_kind
            or descriptor.get("location_type") != location.location_type
            or descriptor.get("structural_class_package") != location.structural_class_package
            or descriptor.get("destination_id") != location.destination_id
            or descriptor.get("protected") != location.protected
            or descriptor.get("protection_reasons") != list(location.protection_reasons)
            or descriptor.get("physical_instance_count") != location.physical_instance_count
            or descriptor.get("parent_blueprint_packages") != list(location.parent_blueprint_packages)
            or descriptor.get("package_available") is not inventory.contains(location.package_path)
            or descriptor.get("rewards") != expected_rewards
        ):
            raise GenerationError(
                f"Prepared World Loot descriptor differs from the active catalog at {key}; re-extract game data."
            )
        patch_descriptor = descriptor.get("patch_descriptor")
        if (
            not isinstance(patch_descriptor, dict)
            or patch_descriptor.get("serializer") != location.destination_kind
            or patch_descriptor.get("expected_rewards") != expected_rewards
        ):
            raise GenerationError(f"Prepared World Loot patch descriptor is stale at {key}; re-extract game data.")
        if location.instance_override_source is not None:
            source = location.instance_override_source
            expected_source = {
                "cdo_package": source.cdo_package,
                "cdo_export_index": source.cdo_export_index,
                "cdo_export_name": source.cdo_export_name,
                "class_package": source.class_package,
                "parent_blueprint_packages": list(source.parent_blueprint_packages),
            }
            if patch_descriptor.get("instance_override_source") != expected_source:
                raise GenerationError(f"Prepared per-instance override source is stale at {key}; re-extract game data.")


# List known source packages
def _known_source_packages(
    catalogs: RuntimeCatalogs,
    *,
    enemy_drop_catalog: EnemyDropCatalog | None,
    blood_code_catalog: BloodCodeCatalog | None,
    booster_catalog: BoosterCatalog | None,
    extractor_catalog: ExtractorCatalog | None,
    defensive_gadget_catalog: DefensiveGadgetCatalog | None,
    jail_catalog: JailCatalog | None,
    field_enemy_catalog: FieldEnemyCatalog | None,
    canonical_article_catalog: CanonicalArticleRewardCatalog | None,
    bequeathed_formae_catalog: BequeathedFormaCatalog | None,
    initial_inventory_enabled: bool = False,
    include_booster_slot_flags: bool = False,
    include_motorcycle_unlock_packages: bool = False,
    include_tutorial_flag_tables: bool = False,
) -> dict[str, tuple[str, ...]]:
    """Enumerate the source packages already named by the runtime catalogs."""
    sources: dict[str, set[str]] = {}

    # Add the requested reward item
    def add(group: str, package: str | None) -> None:
        if package:
            sources.setdefault(group, set()).add(_package_from_asset_path(package))

    for location in catalogs.world_loot:
        add("world_loot", location.package_path)
        if location.location_type == "RENEWABLE_INGREDIENT":
            add("materials", location.structural_class_package)
        for parent_package in location.parent_blueprint_packages:
            add("world_loot_blueprint_parents", parent_package)
        if location.instance_override_source is not None:
            add("world_loot_blueprint_parents", location.instance_override_source.cdo_package)
            for parent_package in location.instance_override_source.parent_blueprint_packages:
                add("world_loot_blueprint_parents", parent_package)
        for reward in location.rewards:
            add("world_loot_articles", reward.article_path)
    # Keep original Article packages referenced by opaque, unsupported CDO
    # instances visible to Extract/cache diagnostics without promoting those
    # unsafe destinations into the randomization pool.
    for reference in getattr(catalogs, "world_loot_unsupported_cdo_sources", ()):
        add("world_loot_articles", reference.reward.article_path)
    for location in catalogs.materials:
        add("materials", location.package_path)
        add("materials", location.resource_class_package)
        add("materials_articles", location.reward.article_path)
    for product in catalogs.shops:
        add("shops", product.original_product_path)
    for table in SHOP_TABLE_NAMES.values():
        add("shops", f"/Game/Blueprint/GameSystem/Shop/{table}")
    if enemy_drop_catalog is not None:
        for _name, package in ENEMY_DROP_TABLES:
            add("enemy_drops", package)
    if blood_code_catalog is not None:
        for package in blood_code_catalog.source_packages:
            add("blood_codes", package)
        for slot in blood_code_catalog.slots:
            for package in slot.physical_source_packages:
                add("blood_codes", package)
            add("blood_code_articles", slot.original_blood_code_package)
    if booster_catalog is not None:
        add("boosters", BOOSTER_TABLE_PACKAGE)
        add("boosters", BOOSTER_ROW_STRUCT_PACKAGE)
        for slot in booster_catalog.slots:
            add("booster_articles", slot.booster_article_path)
            # The mastery requirement keeps this Blood Code Article reference
            # live in the Booster DataTable even when it is not a reward source.
            add("booster_mastery_blood_codes", slot.blood_code_article_path)
    if extractor_catalog is not None:
        for table in extractor_catalog.tables:
            add("extractor", table.virtual_package)
            for row in table.rows:
                add("extractor_articles", row.original_article_path)
    if defensive_gadget_catalog is not None:
        for item in defensive_gadget_catalog.items:
            add("defensive_gadgets", item.asset_path)
        for grant in defensive_gadget_catalog.grants:
            add("defensive_gadget_grants", grant.package_path)
    if jail_catalog is not None:
        for item in jail_catalog.items:
            add("jails", item.asset_path)
            add("jails", item.vanilla_source.package_path)
    if field_enemy_catalog is not None:
        for location in field_enemy_catalog.locations:
            add("field_enemy", location.package_path)
            for reward in location.rewards:
                add("field_enemy_articles", reward.article_path)
    if canonical_article_catalog is not None:
        for item in canonical_article_catalog.items:
            add("canonical_articles", item.asset_path)
    if bequeathed_formae_catalog is not None:
        for item in bequeathed_formae_catalog.items:
            add("bequeathed_formae", item.asset_path)
            for grant in item.direct_grants:
                add("bequeathed_formae_grants", grant.quest_asset)
    if initial_inventory_enabled:
        add("initial_inventory", INITIAL_INVENTORY_PACKAGE)
    if include_booster_slot_flags:
        add("booster_slot_unlock", BOOSTER_SLOT_FLAGS_PACKAGE)
    if include_motorcycle_unlock_packages:
        for package in MOTORCYCLE_CACHE_PACKAGES:
            add("motorcycle_unlock", package)
    if include_tutorial_flag_tables:
        for package in TUTORIAL_FLAG_TABLES:
            add("tutorial_flags", package)
    return {
        group: tuple(sorted(packages, key=str.casefold))
        for group, packages in sorted(sources.items(), key=lambda item: item[0].casefold())
    }


# List World Loot candidate FName values
def _world_loot_candidate_fname_values(
    catalogs: RuntimeCatalogs,
    *,
    installed_inventory: PackageInventory | None = None,
    field_enemy_catalog: FieldEnemyCatalog | None = None,
    canonical_article_catalog: CanonicalArticleRewardCatalog | None = None,
    extractor_catalog: ExtractorCatalog | None = None,
    defensive_gadget_catalog: DefensiveGadgetCatalog | None = None,
    jail_catalog: JailCatalog | None = None,
    bequeathed_formae_catalog: BequeathedFormaCatalog | None = None,
) -> tuple[str, ...]:
    """Return only Articles that can legally enter the shared World Loot pool.

    Blood Codes, renewable ingredient materials, and respawnable enemy-only
    tables are deliberately absent. Shop sources are limited to validated
    finite stock, and protected World/Tool Shop rewards never become names in
    every map's prepared NameMap.
    """
    paths: set[str] = set()
    excluded_package_keys = {value.casefold() for value in catalogs.unsafe_packages}
    inventory_spellings = (
        dict(installed_inventory.package_path_spellings)
        if installed_inventory is not None else {}
    )
    protected_identities = set()
    if catalogs.important_item_exclusions is not None:
        protected_identities.update(
            value.casefold()
            for value in (
                *catalogs.important_item_exclusions.key_identities,
                *catalogs.important_item_exclusions.quest_flag_identities,
            )
            if isinstance(value, str)
        )

    # Add the requested reward item
    def add(path: str | None, *, allow_unavailable: bool = False) -> None:
        if not isinstance(path, str) or not path.casefold().startswith("/game/blueprint/article/"):
            return
        if (
            "/blueprint/article/bloodcode/" in path.casefold()
            or path.casefold() in protected_identities
        ):
            return
        candidates = {path}
        if installed_inventory is not None:
            resolved, _variants = installed_inventory.resolve_package_path(path)
            if resolved is None:
                if not allow_unavailable:
                    return
            else:
                # PackageInventory stores normalized lookup keys. Preserve the
                # source spelling when a base path resolves to one numbered
                # package so both literal and FName-number encodings are ready.
                candidates.add(inventory_spellings.get(resolved.casefold(), resolved))
        paths.update(candidates)

    for location in catalogs.world_loot:
        if (
            location.protected
            or location.protection_reasons
            or location.location_type == "RENEWABLE_INGREDIENT"
            or location.source_file.casefold() in excluded_package_keys
            or location.package_path.casefold() in excluded_package_keys
            or location.destination_kind not in {
                "ARTICLE_BULK", "BLUEPRINT_CDO_ARTICLE_BULK", "INSTANCE_ARTICLE_BULK_OVERRIDE",
                # Direct grants are eligible World Loot sources too, even though
                # their destination package is not a World Partition map.
                "RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT",
            }
        ):
            continue
        for reward in location.rewards:
            add(reward.article_path)

    for product in catalogs.shops:
        if not product.protected and product.uses_stock is True and product.stock is not None and product.stock > 0:
            # Finite shop products are World Mix candidates according to the
            # existing planner. Their base Article path can be ambiguous in the
            # installed package inventory when multiple suffixed variants exist;
            # the NameMap must still support the planner's existing selection.
            add(product.original_product_path, allow_unavailable=True)

    if field_enemy_catalog is not None:
        for location in field_enemy_catalog.locations:
            if not location.protected and not location.protection_reasons:
                for reward in location.rewards:
                    add(reward.article_path)
    if canonical_article_catalog is not None:
        for item in canonical_article_catalog.world_reward_items:
            add(item.asset_path)
    if defensive_gadget_catalog is not None:
        for item in defensive_gadget_catalog.items:
            if item.world_reward_eligible:
                add(item.asset_path)
    if jail_catalog is not None:
        for item in jail_catalog.items:
            if item.world_reward_eligible:
                add(item.asset_path)
    if bequeathed_formae_catalog is not None:
        for item in bequeathed_formae_catalog.items:
            if item.world_reward_eligible:
                add(item.asset_path)
    if extractor_catalog is not None:
        # Recipe outputs can return to World Loot through the existing
        # one-for-one Extractor/World exchange when the recipe option is on.
        for row in extractor_catalog.slots:
            add(row.original_article_path)

    names: set[str] = set()
    for path in paths:
        object_name = path.rsplit("/", 1)[-1]
        names.update((path, object_name))
        stem, separator, digits = object_name.rpartition("_")
        if separator and digits.isdecimal() and stem:
            # Unreal commonly represents a literal _N suffix as FName(stem, N+1).
            # Keep both source encodings available; Generate resolves exact identity.
            names.add(stem)
            names.add(path.rsplit("/", 1)[0] + "/" + stem)
    return tuple(sorted(names))


# Find world package overrides
def _world_package_overrides(
    result: WorldLootGenerationResult | None,
    *,
    source_content_root: Path,
    intermediate_content_root: Path,
    work_root: Path,
    material_plan: MaterialRandomizationPlan | None = None,
) -> dict[str, Path]:
    if result is None:
        return {}
    source_root = source_content_root.resolve()
    intermediate_root = intermediate_content_root.resolve()
    root = work_root.resolve()
    overrides: dict[str, Path] = {}
    material_patch_packages = None
    if material_plan is not None:
        material_patch_packages = {
            assignment.destination.package_path.casefold()
            for assignment in material_plan.assignments
            if assignment.destination.resource_class_package.casefold()
            != assignment.source.resource_class_package.casefold()
        }
    for package in result.package_results:
        input_path = package.input_path.resolve(strict=True)
        package_output = package.output_path.resolve(strict=True)
        if _is_within(package_output, intermediate_root):
            # Extract-prepared binary packages are read directly from the
            # persistent cache. Their already-validated output path carries
            # the virtual Content-relative location for composition.
            relative = package_output.relative_to(intermediate_root)
        elif _is_within(input_path, source_root):
            relative = input_path.relative_to(source_root)
        else:
            raise GenerationError("World Loot returned a map outside its isolated source and output trees.")
        if relative.suffix.casefold() == ".uasset":
            # Class-default reward owners are packaged through the World Loot
            # output inventory, but do not participate in the map overlay used
            # by the Materials patcher.
            continue
        if relative.suffix.casefold() != ".umap":
            raise GenerationError("World Loot returned a non-map package for composition.")
        output_path = (intermediate_root / relative).resolve(strict=True)
        if not _is_within(output_path, intermediate_root) or not _is_within(output_path, root):
            raise GenerationError("World Loot intermediate package escaped the generation workspace.")
        virtual_path = "/Game/" + relative.with_suffix("").as_posix()
        key = virtual_path.casefold()
        if key in overrides:
            raise GenerationError("World Loot returned duplicate map packages for composition.")
        if material_patch_packages is not None and key not in material_patch_packages:
            continue
        overrides[key] = output_path
    return overrides


# Copy world outputs
def _copy_world_outputs(
    result: WorldLootGenerationResult | None,
    *,
    intermediate_content_root: Path,
    output_content_root: Path,
    material_result: MaterialGenerationResult | None,
) -> None:
    if result is None:
        return
    intermediate_root = intermediate_content_root.resolve()
    output_root = output_content_root.resolve()
    material_map_headers = {
        path.resolve().relative_to(output_root).with_suffix(".umap")
        for path in (material_result.output_packages if material_result else ())
        if path.suffix.casefold() == ".umap"
    }
    for source in result.output_packages:
        source_path = source.resolve(strict=True)
        if not _is_within(source_path, intermediate_root):
            raise GenerationError("World Loot output package escaped its intermediate Content tree.")
        relative = source_path.relative_to(intermediate_root)
        if relative.with_suffix(".umap") in material_map_headers:
            continue
        target_path = (output_root / relative).resolve()
        if source_path == target_path:
            continue
        _copy_validated_file(source_path, target_path, output_root)


# Remap world result
def _remap_world_result(
    result: WorldLootGenerationResult | None,
    *,
    source_content_root: Path,
    intermediate_content_root: Path,
    output_content_root: Path,
) -> WorldLootGenerationResult | None:
    if result is None:
        return None
    source_root = source_content_root.resolve()
    intermediate_root = intermediate_content_root.resolve()
    output_root = output_content_root.resolve()
    package_results = []
    for package in result.package_results:
        input_path = package.input_path.resolve()
        package_output = package.output_path.resolve()
        if _is_within(package_output, intermediate_root):
            # Binary World Loot outputs are already written under the target
            # Content tree, even when their input template came from the
            # persistent prepared-assets cache rather than workspace/source.
            relative = package_output.relative_to(intermediate_root)
        elif _is_within(input_path, source_root):
            relative = input_path.relative_to(source_root)
        else:
            raise GenerationError("World Loot package input/output escaped its owned Content trees.")
        final_header = (output_root / relative).resolve(strict=True)
        if not _is_within(final_header, output_root):
            raise GenerationError("Composed World Loot output escaped the final Content tree.")
        patches = tuple(replace(patch, output_path=final_header) for patch in package.patches)
        package_results.append(replace(package, output_path=final_header, patches=patches))
    output_packages = tuple(
        (output_root / path.resolve().relative_to(intermediate_root)).resolve()
        for path in result.output_packages
    )
    if any(not path.is_file() for path in output_packages):
        raise GenerationError("A World Loot package sidecar is missing after package composition.")
    return replace(result, package_results=tuple(package_results), output_packages=output_packages)

# Copy validated file
def _copy_validated_file(source: Path, target: Path, output_root: Path) -> None:
    source_path = source.resolve(strict=True)
    root = output_root.resolve()
    target_path = target.resolve()
    if not _is_within(target_path, root):
        raise GenerationError("A staged package copy escaped its owned generation workspace.")
    if not source_path.is_file() or source_path.stat().st_size <= 0:
        raise GenerationError("A staged package copy has no validated source bytes.")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        if _sha256_file(source_path) == _sha256_file(target_path):
            return
        raise GenerationError(f"Refusing to overwrite a conflicting staged file: {target_path.name}.")
    shutil.copyfile(source_path, target_path)
    if _sha256_file(source_path) != _sha256_file(target_path):
        target_path.unlink(missing_ok=True)
        raise GenerationError(f"Staged package copy failed integrity validation: {target_path.name}.")


# Parse SHA-256 file
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Find expected output files
def _expected_output_files(
    world: WorldLootGenerationResult | None,
    shops: ShopGenerationResult | None,
    materials: MaterialGenerationResult | None,
    enemy_drops: EnemyDropGenerationResult | None = None,
    blood_codes: BloodCodeGenerationResult | None = None,
    boosters: BoosterGenerationResult | None = None,
    defensive_gadgets: DefensiveGadgetGenerationResult | None = None,
    bequeathed_formae: BequeathedFormaGenerationResult | None = None,
    world_direct_grants: tuple[WorldLootDirectGrantResult, ...] = (),
    extractor: ExtractorGenerationResult | None = None,
    initial_inventory: InitialInventoryGenerationResult | None = None,
    booster_slot_unlock: BoosterSlotUnlockResult | None = None,
    motorcycle_unlock: MotorcycleUnlockResult | None = None,
    tutorial_disable: TutorialDisableResult | None = None,
    blood_code_mastery: BloodCodeMasteryResult | None = None,
    dialogue_skip: DialogueSkipResult | None = None,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    if world is not None:
        paths.extend(world.output_packages)
    if shops is not None:
        for table in shops.outputs:
            paths.extend((table.header_path, table.export_path, *table.additional_files))
    if materials is not None:
        paths.extend(materials.output_packages)
    if enemy_drops is not None:
        paths.extend(enemy_drops.output_files)
    if blood_codes is not None:
        paths.extend(blood_codes.output_files)
    if boosters is not None:
        paths.extend(boosters.output_files)
    if extractor is not None:
        paths.extend(extractor.output_files)
    if initial_inventory is not None:
        paths.extend(initial_inventory.output_files)
    if booster_slot_unlock is not None:
        paths.extend(booster_slot_unlock.output_files)
    if motorcycle_unlock is not None:
        paths.extend(motorcycle_unlock.output_files)
    if tutorial_disable is not None:
        paths.extend(tutorial_disable.output_files)
    if blood_code_mastery is not None:
        paths.extend(blood_code_mastery.output_files)
    if dialogue_skip is not None:
        paths.extend(dialogue_skip.output_files)
    if defensive_gadgets is not None:
        paths.extend(defensive_gadgets.output_files)
    if bequeathed_formae is not None:
        paths.extend(bequeathed_formae.output_files)
    for direct_grant in world_direct_grants:
        paths.extend(direct_grant.output_files)
    return tuple(sorted(set(paths), key=lambda path: str(path).casefold()))


# Find changed package registry
def _changed_package_registry(
    content_root: Path,
    *,
    world: WorldLootGenerationResult | None,
    shops: ShopGenerationResult | None,
    materials: MaterialGenerationResult | None,
    enemy_drops: EnemyDropGenerationResult | None,
    blood_codes: BloodCodeGenerationResult | None,
    boosters: BoosterGenerationResult | None,
    defensive_gadgets: DefensiveGadgetGenerationResult | None,
    bequeathed_formae: BequeathedFormaGenerationResult | None,
    direct_grants: tuple[WorldLootDirectGrantResult, ...],
    extractor: ExtractorGenerationResult | None,
    initial_inventory: InitialInventoryGenerationResult | None,
    booster_slot_unlock: BoosterSlotUnlockResult | None = None,
    motorcycle_unlock: MotorcycleUnlockResult | None = None,
    tutorial_disable: TutorialDisableResult | None = None,
    blood_code_mastery: BloodCodeMasteryResult | None = None,
    dialogue_skip: DialogueSkipResult | None = None,
) -> tuple[dict[str, object], ...]:
    """Group subsystem-owned staged outputs into the exact file inventory for Retoc."""
    root = content_root.resolve()
    entries: dict[str, dict[str, object]] = {}

    # Register the source package
    def register(source: str, files: Iterable[Path]) -> None:
        for value in files:
            path = Path(value)
            resolved = path.resolve(strict=True)
            if not _is_within(resolved, root) or not resolved.is_file():
                raise GenerationError("A changed-package registry entry is missing or outside staged Content.")
            relative = resolved.relative_to(root).as_posix()
            package_relative = Path(relative).with_suffix("").as_posix()
            virtual = "/Game/" + package_relative
            key = virtual.casefold()
            entry = entries.get(key)
            if entry is None:
                entry = {"virtual_package": virtual, "files": set(), "sources": set()}
                entries[key] = entry
            entry["files"].add(relative)
            entry["sources"].add(source)

    if world is not None:
        register("world_loot", world.output_packages)
    if shops is not None:
        for table in shops.outputs:
            register("shops", (table.header_path, table.export_path, *table.additional_files))
    if materials is not None:
        register("materials", materials.output_packages)
    if enemy_drops is not None:
        register("enemy_drops", enemy_drops.output_files)
    if blood_codes is not None:
        register("blood_codes", blood_codes.output_files)
    if boosters is not None:
        register("boosters", boosters.output_files)
    if defensive_gadgets is not None:
        register("defensive_gadgets", defensive_gadgets.output_files)
    if bequeathed_formae is not None:
        register("bequeathed_formae", bequeathed_formae.output_files)
    for grant in direct_grants:
        register("world_loot_direct", grant.output_files)
    if extractor is not None:
        register("extractor", extractor.output_files)
    if initial_inventory is not None:
        register("initial_inventory", initial_inventory.output_files)
    if booster_slot_unlock is not None:
        register("miscellaneous", booster_slot_unlock.output_files)
    if motorcycle_unlock is not None:
        register("miscellaneous", motorcycle_unlock.output_files)
    if tutorial_disable is not None:
        register("miscellaneous", tutorial_disable.output_files)
    if blood_code_mastery is not None:
        register("miscellaneous", blood_code_mastery.output_files)
    if dialogue_skip is not None:
        register("miscellaneous", dialogue_skip.output_files)

    output: list[dict[str, object]] = []
    for entry in entries.values():
        output.append({
            "virtual_package": entry["virtual_package"],
            "files": sorted(entry["files"]),
            "sources": sorted(entry["sources"]),
        })
    return tuple(sorted(output, key=lambda item: str(item["virtual_package"]).casefold()))


# Write generation performance report
def _write_generation_performance_report(
    workspace: GenerationWorkspace,
    performance: dict[str, float],
    changed_packages: tuple[dict[str, object], ...],
) -> Path:
    target = workspace.logs / "generation_performance.json"
    document = {
        "timings_seconds": performance,
        "changed_package_count": len(changed_packages),
        "staged_file_count": sum(len(item["files"]) for item in changed_packages),
        "recursive_staged_content_scans": 0,
        "changed_packages": changed_packages,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


# Validate output tree
def _validate_output_tree(output_root: Path, expected: tuple[Path, ...]) -> tuple[Path, ...]:
    root = output_root.resolve()
    expected_files: set[Path] = set()
    for path in expected:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not _is_within(resolved, root) or not resolved.is_file() or resolved.stat().st_size <= 0:
            raise GenerationError("A validated output is missing, empty, or outside staged Content.")
        parent = path.parent
        while parent != output_root and parent != parent.parent:
            if parent.is_symlink():
                raise GenerationError("Staged Content must not contain symlink directories.")
            parent = parent.parent
        if resolved.suffix.casefold() not in {".uasset", ".umap", ".uexp", ".ubulk", ".uptnl"}:
            raise GenerationError("Staged Content contains an unsupported file extension.")
        expected_files.add(resolved)
    if not root.exists():
        if expected_files:
            raise GenerationError("Expected staged files are missing because Content was not created.")
        return ()
    headers = {
        path.with_suffix(".uexp")
        for path in expected_files
        if path.suffix.casefold() in {".uasset", ".umap"}
    }
    sidecars = {path for path in expected_files if path.suffix.casefold() == ".uexp"}
    if headers != sidecars:
        missing = sorted(path.name for path in headers - sidecars)
        orphaned = sorted(path.name for path in sidecars - headers)
        raise GenerationError(f"Staged package sidecars do not match headers (missing={missing}, orphaned={orphaned}).")
    return tuple(sorted(expected_files, key=lambda path: path.relative_to(root).as_posix().casefold()))


# Validate final blood code artifact
def _validate_final_blood_code_artifact(
    result: BloodCodeGenerationResult,
    *,
    output_files: tuple[Path, ...],
    output_content_root: Path,
    work_root: Path,
    mappings_path: Path,
    serializer: BloodCodeSerializer,
) -> dict[str, int]:
    """Reopen final staged quest packages after every subsystem has finished writing."""
    changed = tuple(
        assignment for assignment in result.plan.assignments
        if assignment.destination.original_blood_code_package.casefold()
        != assignment.source.original_blood_code_package.casefold()
    )
    direct_expected: dict[tuple[str, int, int, int], tuple[object, object]] = {}
    reference_expected: dict[tuple[str, int, str, int], tuple[object, object]] = {}
    object_reference_expected: dict[tuple[str, int, str], tuple[object, object]] = {}
    for assignment in changed:
        destination = assignment.destination
        source = assignment.source
        if destination.location_kind == "gain_item":
            if destination.bulk_index is None or destination.reward_index is None:
                raise GenerationError(f"Changed Blood Code slot {destination.slot_id} lacks a physical reward index.")
            key = (
                destination.virtual_package.casefold(), destination.export_index,
                destination.bulk_index, destination.reward_index,
            )
            if key in direct_expected:
                raise GenerationError(f"Blood Code plan contains a duplicate changed direct slot: {destination.slot_id}.")
            direct_expected[key] = (destination, source)
        if destination.location_kind not in {
            "gain_item", "proficiency_reference", "scenario_reward",
        } and not getattr(destination, "physical_object_reference_targets", ()):
            raise GenerationError(f"Blood Code plan contains an unknown location kind: {destination.location_kind}.")
        for target in getattr(destination, "physical_object_reference_targets", ()):
            key = (
                target.virtual_package.casefold(), target.export_index,
                target.reference_property.casefold(),
            )
            if key in object_reference_expected:
                raise GenerationError(f"Blood Code plan contains a duplicate BuddySettings reference: {destination.slot_id}.")
            object_reference_expected[key] = (target, source)
        for target in getattr(destination, "physical_reference_targets", ()):
            key = (
                target.virtual_package.casefold(), target.export_index,
                target.reference_property.casefold(), target.reference_index,
            )
            if key in reference_expected:
                raise GenerationError(f"Blood Code plan contains a duplicate changed reference: {destination.slot_id}.")
            reference_expected[key] = (target, source)

    expected_packages = {
        key[0] for key in (*direct_expected.keys(), *reference_expected.keys(), *object_reference_expected.keys())
    }
    reported_packages = {package.casefold() for package in result.patched_packages}
    if reported_packages != expected_packages:
        raise GenerationError(
            "Blood Code patch inventory does not match changed plan targets "
            f"(expected={sorted(expected_packages)}, reported={sorted(reported_packages)})."
        )
    if not expected_packages:
        return {"logical_assignments": 0, "direct_slots": 0, "reference_slots": 0, "object_reference_slots": 0, "packages": 0}

    root = output_content_root.resolve(strict=True)
    staged_headers: dict[str, Path] = {}
    for path in output_files:
        resolved = path.resolve(strict=True)
        if resolved.suffix.casefold() not in {".uasset", ".umap"}:
            continue
        try:
            relative = resolved.relative_to(root)
        except ValueError as error:
            raise GenerationError("Final Blood Code validation received a package outside staged Content.") from error
        virtual = "/Game/" + relative.with_suffix("").as_posix()
        staged_headers[virtual.casefold()] = resolved
    missing = expected_packages - set(staged_headers)
    if missing:
        raise GenerationError(f"Final staged Blood Code package(s) are missing: {', '.join(sorted(missing))}.")

    direct_virtuals = {
        destination.virtual_package.casefold(): destination.virtual_package
        for destination, _source in direct_expected.values()
    }
    direct_packages = {
        virtual: staged_headers[folded]
        for folded, virtual in direct_virtuals.items()
    }
    if direct_expected:
        snapshots = serializer.inspect_packages(
            work_root=work_root,
            mappings_path=mappings_path,
            package_files=direct_packages,
        )
        observed: dict[tuple[str, int, int, int], object] = {}
        for snapshot in snapshots:
            key = (
                snapshot.virtual_package.casefold(), snapshot.export_index,
                snapshot.bulk_index, snapshot.reward_index,
            )
            if key in observed:
                raise GenerationError(f"Final staged Blood Code reward is duplicated at {key[0]}#{key[1]}.")
            observed[key] = snapshot
        for key, (destination, source) in direct_expected.items():
            snapshot = observed.get(key)
            if snapshot is None or (
                snapshot.reward_package.casefold() != source.original_blood_code_package.casefold()
                or snapshot.reward_asset.casefold() != source.original_blood_code_asset.casefold()
                or snapshot.count != destination.count
                or snapshot.notification is not destination.b_notification
                or snapshot.package_name_value.casefold() != source.original_blood_code_package.casefold()
                or snapshot.package_name_number != 0
                or snapshot.asset_name_value.casefold() != source.original_blood_code_asset.casefold()
                or snapshot.asset_name_number != 0
                or snapshot.sub_path_string
            ):
                raise GenerationError(
                    f"Final staged Blood Code assignment was lost or changed at {destination.slot_id} "
                    f"({destination.virtual_package}#{destination.export_index})."
                )

    if reference_expected:
        targets_by_package = {
            target.virtual_package: staged_headers[target.virtual_package.casefold()]
            for target, _source in reference_expected.values()
        }
        targets = tuple(target for target, _source in reference_expected.values())
        snapshots = serializer.inspect_reference_packages(
            work_root=work_root,
            mappings_path=mappings_path,
            package_files=targets_by_package,
            targets=targets,
        )
        observed_references: dict[tuple[str, int, str, int], object] = {}
        for snapshot in snapshots:
            key = (
                snapshot.virtual_package.casefold(), snapshot.export_index,
                snapshot.reference_property.casefold(), snapshot.reference_index,
            )
            if key in observed_references:
                raise GenerationError(f"Final staged Blood Code reference is duplicated at {key[0]}#{key[1]}.")
            observed_references[key] = snapshot
        for key, (target, source) in reference_expected.items():
            snapshot = observed_references.get(key)
            if snapshot is None or (
                snapshot.export_name != target.export_name
                or snapshot.array_count != target.expected_array_count
                or snapshot.reward_package.casefold() != source.original_blood_code_package.casefold()
                or snapshot.reward_asset.casefold() != source.original_blood_code_asset.casefold()
                or snapshot.package_name_value.casefold() != source.original_blood_code_package.casefold()
                or snapshot.package_name_number != 0
                or snapshot.asset_name_value.casefold() != source.original_blood_code_asset.casefold()
                or snapshot.asset_name_number != 0
                or snapshot.sub_path_string
            ):
                raise GenerationError(
                    f"Final staged Blood Code assignment was lost or changed at reference "
                    f"{target.virtual_package}#{target.export_index}.{target.reference_property}[{target.reference_index}]."
                )

    if object_reference_expected:
        targets_by_package = {
            target.virtual_package: staged_headers[target.virtual_package.casefold()]
            for target, _source in object_reference_expected.values()
        }
        targets = tuple(
            replace(
                target,
                expected_import_package=source.original_blood_code_package,
                expected_import_asset=source.original_blood_code_asset,
                expected_package_name_number=0,
                expected_asset_name_number=0,
            )
            for target, source in object_reference_expected.values()
        )
        snapshots = serializer.inspect_object_reference_packages(
            work_root=work_root,
            mappings_path=mappings_path,
            package_files=targets_by_package,
            targets=targets,
        )
        observed_objects: dict[tuple[str, int, str], object] = {}
        for snapshot in snapshots:
            key = (
                snapshot.virtual_package.casefold(), snapshot.export_index,
                snapshot.reference_property.casefold(),
            )
            if key in observed_objects:
                raise GenerationError(f"Final BuddySettings Blood Code reference is duplicated at {key[0]}#{key[1]}.")
            observed_objects[key] = snapshot
        for key, (target, source) in object_reference_expected.items():
            snapshot = observed_objects.get(key)
            if snapshot is None or (
                snapshot.export_name != target.export_name
                or snapshot.property_schema_index != target.property_schema_index
                or snapshot.reward_package.casefold() != source.original_blood_code_package.casefold()
                or snapshot.reward_asset.casefold() != source.original_blood_code_asset.casefold()
                or snapshot.package_name_number != 0
                or snapshot.asset_name_number != 0
                or snapshot.import_class_name != target.expected_import_class_name
                or snapshot.import_class_package != target.expected_import_class_package
                or snapshot.owner_exports != (target.export_index,)
            ):
                raise GenerationError(
                    f"Final staged BuddySettings Blood Code assignment was lost or changed at "
                    f"{target.virtual_package}#{target.export_index}.{target.reference_property}."
                )

    return {
        "logical_assignments": len(changed),
        "direct_slots": len(direct_expected),
        "reference_slots": len(reference_expected),
        "object_reference_slots": len(object_reference_expected),
        "packages": len(expected_packages),
    }

# Write manifest
def _write_manifest(
    workspace: GenerationWorkspace,
    *,
    seed: NormalizedSeed,
    options: GenerationOptions,
    catalogs: RuntimeCatalogs,
    game_version: str,
    catalog_revision: str,
    world_plan: WorldLootRandomizationPlan,
    shop_plan: ShopRandomizationPlan,
    material_plan: MaterialRandomizationPlan,
    output_files: tuple[Path, ...],
    dry_run: bool,
    world_direct_grant_results: tuple[WorldLootDirectGrantResult, ...] = (),
    enemy_drop_plan: EnemyDropPlan | None = None,
    enemy_drop_result: EnemyDropGenerationResult | None = None,
    blood_code_plan: BloodCodePlan | None = None,
    blood_code_result: BloodCodeGenerationResult | None = None,
    booster_plan: BoosterPlan | None = None,
    booster_result: BoosterGenerationResult | None = None,
    extractor_plan: ExtractorPlan | None = None,
    extractor_result: ExtractorGenerationResult | None = None,
    initial_inventory_plan: InitialInventoryPlan | None = None,
    initial_inventory_preparation: InitialInventoryPreparation | None = None,
    initial_inventory_result: InitialInventoryGenerationResult | None = None,
    motorcycle_unlock_result: MotorcycleUnlockResult | None = None,
    tutorial_disable_result: TutorialDisableResult | None = None,
    blood_code_mastery_result: BloodCodeMasteryResult | None = None,
    defensive_gadget_catalog: DefensiveGadgetCatalog | None = None,
    defensive_gadget_plan: DefensiveGadgetNpcPlan | None = None,
    defensive_gadget_result: DefensiveGadgetGenerationResult | None = None,
    bequeathed_formae_catalog: BequeathedFormaCatalog | None = None,
    bequeathed_formae_plan: BequeathedFormaPlan | None = None,
    bequeathed_formae_result: BequeathedFormaGenerationResult | None = None,
    jail_catalog: JailCatalog | None = None,
    field_enemy_catalog: FieldEnemyCatalog | None = None,
    package_result: PackageResult | None = None,
    installation_result: InstallationResult | None = None,
    content_availability: ContentAvailability | None = None,
    availability_warnings: tuple[str, ...] = (),
    source_cache_metadata: dict[str, object] | None = None,
    spoiler_text_path: Path | None = None,
    reproducibility: ReproducibilityMetadata | None = None,
) -> None:
    output_root = workspace.staging / "Content"
    if installation_result is not None:
        installation_directory = installation_result.mods_directory.resolve().relative_to(
            installation_result.game_root.resolve()
        ).as_posix()
        installation_status = "installed"
    elif dry_run and options.install_after_generation:
        installation_directory = None
        installation_status = "not_performed_dry_run"
    elif options.install_after_generation and package_result is None and not output_files:
        installation_directory = None
        installation_status = "not_required_no_changes"
    elif options.install_after_generation:
        installation_directory = None
        installation_status = "pending"
    else:
        installation_directory = None
        installation_status = "not_requested"
    bequeathed_formae_document = _bequeathed_formae_manifest(
        bequeathed_formae_catalog,
        bequeathed_formae_plan,
        bequeathed_formae_result,
        enabled=options.randomize_bequeathed_formae_quest_rewards,
        dry_run=dry_run,
    )
    initial_inventory_document = _initial_inventory_manifest(
        initial_inventory_plan,
        initial_inventory_preparation,
        initial_inventory_result,
        enabled=options.randomize_initial_inventory,
            motorcycle_unlocked=options.motorcycle_unlocked,
        dry_run=dry_run,
        catalog_revision=catalog_revision,
    )
    npc_traders = shop_plan.npc_traders
    mixed_trader_slots = (
        npc_traders.eligible_slots
        if npc_traders is not None and npc_traders.mode == "mixed" and shop_plan.mix_shops_with_world
        else 0
    )
    ordinary_global_shop_slots = max(0, shop_plan.global_shop_locations - mixed_trader_slots)
    ordinary_global_assignments = sum(
        item.article_only and not is_npc_trader_row(item.destination.family, item.destination.shop_row)
        for item in shop_plan.assignments
    )
    document = {
        "schema": "cv2-randomizer-generation-v1",
        "status": "assignments_only" if dry_run else (
            "installed" if installation_result is not None else (
                "package_validated" if package_result is not None else "staging_validated"
            )
        ),
        "app_name": APP_NAME,
        "app_version": __version__,
        "randomizer_algorithm_version": RANDOMIZER_ALGORITHM_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "seed": {"display": seed.display_value, "generated": seed.generated},
        "game_version": game_version,
        "version_validation": (
            "local-unverified" if game_version.startswith("local-unverified-") else "signature-verified"
        ),
        "catalog_revision": catalog_revision,
        "vanilla_cache": _vanilla_cache_manifest(source_cache_metadata),
        "options": options.to_dict(),
        "reproducibility": (
            {
                "algorithm_version": reproducibility.algorithm_version,
                "config_fingerprint": reproducibility.config_fingerprint,
                "content_fingerprint": reproducibility.content_fingerprint,
                "assignment_manifest_sha256": reproducibility.assignment_manifest_sha256,
            }
            if reproducibility is not None else None
        ),
        "content_availability": {
            **_content_availability_summary(catalogs),
            "source_packages": content_availability.to_dict() if content_availability is not None else {},
            "skipped_feature_groups": list(availability_warnings),
        },
        "dry_run": dry_run,
        "counts": {
            "world_loot": {
                "assignments": len(world_plan.assignments),
                "locked": len(world_plan.locked_locations),
                "excluded": len(world_plan.excluded_locations),
                "disabled": len(world_plan.disabled_locations),
                "unmatched_where": len(world_plan.unmatched_locations),
                "eligible_vanilla_what_sources": world_plan.eligible_reward_source_count,
                "unavailable_what_sources": len(world_plan.unavailable_what_locations),
                "canonical_reward_candidates": sum(
                    stats.canonical_candidates for stats in world_plan.candidate_pool_stats
                ),
                "canonical_rewards_injected": sum(
                    stats.injected_count for stats in world_plan.candidate_pool_stats
                ),
                "canonical_duplicate_sources_removed": sum(
                    stats.duplicate_sources_removed for stats in world_plan.candidate_pool_stats
                ),
                "canonical_overlay_displaced_payloads": world_plan.overlay_displaced_payloads,
                "defensive_gadget_candidates": world_plan.gadget_candidate_count,
                "defensive_gadget_injected": world_plan.gadget_injected_count,
                "defensive_gadget_duplicate_sources": world_plan.gadget_duplicate_sources,
                "defensive_gadget_displaced_payloads": world_plan.gadget_displaced_payloads,
                "booster_candidates": world_plan.booster_candidate_count,
                "booster_candidates_injected": world_plan.booster_injected_count,
                "booster_duplicate_sources_removed": world_plan.booster_duplicate_sources,
                "direct_article_grants": _direct_world_grants_manifest(
                    world_plan,
                    world_direct_grant_results,
                    enabled=options.randomize_world_loot,
                    dry_run=dry_run,
                    output_root=output_root,
                ),
            },
            "materials": {
                "assignments": len(material_plan.assignments),
                "locked": len(material_plan.locked_locations),
                "disabled": len(material_plan.disabled_locations),
            },
            "shops": {
                "randomization_enabled": options.randomize_shops,
                "assignments": sum(
                    not is_npc_trader_row(item.destination.family, item.destination.shop_row)
                    for item in shop_plan.assignments
                ),
                "linked_world_reward_alternates_patched": sum(
                    getattr(item.source, "origin_kind", None) == "LINKED_WORLD_REWARD"
                    for item in shop_plan.linked_assignments
                ),
                "linked_starting_inventory_mirrors_patched": sum(
                    getattr(item.source, "origin_kind", None) == "LINKED_INITIAL_INVENTORY"
                    for item in shop_plan.linked_assignments
                ),
                "locked": sum(not is_npc_trader_row(item.family, item.shop_row) for item in shop_plan.locked_slots),
                "unvalidated": sum(not is_npc_trader_row(item.family, item.shop_row) for item in shop_plan.unvalidated_slots),
                "disabled": sum(not is_npc_trader_row(item.family, item.shop_row) for item in shop_plan.disabled_slots),
                "mix_shops_with_world": shop_plan.mix_shops_with_world,
                "extractor_reserved_slots": sum(
                    not is_npc_trader_row(item.family, item.shop_row)
                    for item in shop_plan.extractor_reserved_slots
                ),
                "participating_locations": (
                    ordinary_global_shop_slots if shop_plan.mix_shops_with_world else sum(
                        not is_npc_trader_row(item.destination.family, item.destination.shop_row)
                        for item in shop_plan.assignments
                    )
                ),
                "candidate_rewards_participating": ordinary_global_shop_slots if shop_plan.mix_shops_with_world else 0,
                "locations_mixed_globally": ordinary_global_assignments if shop_plan.mix_shops_with_world else 0,
                "available_families": list(shop_plan.available_families),
                "unavailable_families": list(shop_plan.unavailable_families),
                "protected_rows": _shop_protection_diagnostics(shop_plan, include_stock_locks=False),
                "slot_lock_reasons": _shop_protection_diagnostics(shop_plan),
            },
            "npc_traders": _npc_trader_manifest(shop_plan.npc_traders),
            "important_items": _important_items_manifest(catalogs),
            "global_article_pool": {
                "enabled": shop_plan.mix_shops_with_world,
                "validation": "validated" if shop_plan.mix_shops_with_world else "not_enabled",
                "world_locations": shop_plan.global_world_locations,
                "shop_locations": shop_plan.global_shop_locations,
                "candidate_inventory": shop_plan.global_candidate_count,
                "world_destinations_from_shop": shop_plan.global_world_from_shop,
                "shop_destinations_from_world": shop_plan.global_shop_from_world,
            },
            "planning_priority": {
                "extractor_before_flexible_pool": extractor_plan is not None,
                "extractor_boosters_consumed": 0 if extractor_plan is None else len(extractor_plan.assignments),
                "extractor_remaining_boosters_exposed": 0 if extractor_plan is None else len(extractor_plan.remaining_candidates),
                "reserved_world_source_locations": sum(
                    item.reason == "EXTRACTOR_BOOSTER_RESERVED" for item in world_plan.locked_locations
                ),
                "reserved_shop_source_locations": len(shop_plan.extractor_reserved_slots),
                "reserved_npc_trader_source_locations": 0 if npc_traders is None else len(npc_traders.reserved_slots),
                "consumed_boosters_reused_globally": False,
                "initial_inventory_after_extractor": initial_inventory_plan is not None,
                "initial_inventory_before_world_shop_jail_mixed": initial_inventory_plan is not None,
                "initial_inventory_rewards_reserved": 0 if initial_inventory_plan is None else len(initial_inventory_plan.assignments),
            },
            "enemy_drops": _enemy_drop_manifest_counts(enemy_drop_plan, enemy_drop_result),
            "blood_codes": _blood_code_manifest_counts(blood_code_plan, blood_code_result),
            "boosters": _booster_manifest_counts(booster_plan, booster_result),
            "extractor": _extractor_manifest_counts(
                extractor_plan,
                extractor_result,
                randomize_recipes=options.randomize_extractor_recipes,
            ),
            "cheaper_booster_extractor": {
                "enabled": options.cheaper_booster_extractor,
                "applied": bool(extractor_result and extractor_result.one_material_each),
                "material_quantity_per_ingredient": 1 if options.cheaper_booster_extractor else None,
                "haze_cost_kept": True,
            },
            "initial_inventory": initial_inventory_document,
            "motorcycle_unlock": {
                "enabled": options.motorcycle_unlocked,
                "patched_flags": list(motorcycle_unlock_result.patched_flags) if motorcycle_unlock_result else [],
                "patched_mount_gate": bool(motorcycle_unlock_result and motorcycle_unlock_result.patched_mount_gate),
                "reopened_and_validated": bool(motorcycle_unlock_result and motorcycle_unlock_result.reopened),
            },
            "tutorials": {
                "disabled": options.disable_tutorials,
                "patched_tables": list(tutorial_disable_result.patched_tables) if tutorial_disable_result else [],
                "patched_flag_count": tutorial_disable_result.patched_flag_count if tutorial_disable_result else 0,
                "reopened_and_validated": bool(tutorial_disable_result and tutorial_disable_result.reopened),
            },
            "blood_code_mastery": {
                "enabled": options.low_blood_code_mastery_exp,
                "lou01_original_max_proficiency": (
                    blood_code_mastery_result.lou01_original_proficiency
                    if blood_code_mastery_result else None
                ),
                "target_max_proficiency": (
                    blood_code_mastery_result.target_proficiency if blood_code_mastery_result else None
                ),
                "target_appropriate_level": (
                    LOW_BLOOD_CODE_MASTERY_APPROPRIATE_LEVEL if options.low_blood_code_mastery_exp else None
                ),
                "patched_packages": list(blood_code_mastery_result.patched_packages) if blood_code_mastery_result else [],
                "reopened_and_validated": bool(blood_code_mastery_result and blood_code_mastery_result.reopened),
            },
            "defensive_gadgets": _defensive_gadget_manifest_counts(
                defensive_gadget_catalog, defensive_gadget_plan, defensive_gadget_result, world_plan,
                options=options, dry_run=dry_run,
            ),
            "jails": _jail_manifest_counts(jail_catalog, world_plan, options=options),
            "field_enemy": _field_enemy_manifest_counts(
                field_enemy_catalog, world_plan, options=options, direct_results=world_direct_grant_results,
            ),
            "bequeathed_formae": {
                "catalog_count": bequeathed_formae_document["catalog_count"],
                "direct_logical_rewards": bequeathed_formae_document["direct_logical_rewards"],
                "direct_physical_packages": bequeathed_formae_document["direct_physical_packages"],
                "assignments": len(bequeathed_formae_document["direct_assignments"]),
                "changed": sum(item["changed"] for item in bequeathed_formae_document["direct_assignments"]),
            },
        },
        "skipped": {
            "world_loot": len(world_plan.locked_locations) + len(world_plan.excluded_locations)
            + len(world_plan.disabled_locations) + len(world_plan.unmatched_locations),
            "materials": len(material_plan.locked_locations) + len(material_plan.disabled_locations),
            "shops": sum(
                not is_npc_trader_row(item.family, item.shop_row)
                for item in (*shop_plan.locked_slots, *shop_plan.unvalidated_slots, *shop_plan.disabled_slots)
            ),
            "npc_traders": 0 if shop_plan.npc_traders is None else (
                len(shop_plan.npc_traders.locked_slots) + len(shop_plan.npc_traders.unvalidated_slots)
            ),
            "enemy_drops": _enemy_drop_manifest_counts(enemy_drop_plan, enemy_drop_result)["excluded_rows"],
            "blood_codes": 0,
            "boosters": 0,
            "extractor": 0 if extractor_plan is None else len(extractor_plan.assignments) - extractor_plan.changed_rows,
            "defensive_gadgets": 0 if defensive_gadget_plan is None else len(defensive_gadget_plan.pending_grants),
        },
        "defensive_gadgets": _defensive_gadget_manifest_counts(
            defensive_gadget_catalog, defensive_gadget_plan, defensive_gadget_result, world_plan,
            options=options, dry_run=dry_run,
        ),
        "jails": _jail_manifest_counts(jail_catalog, world_plan, options=options),
        "field_enemy": _field_enemy_manifest_counts(
            field_enemy_catalog, world_plan, options=options, direct_results=world_direct_grant_results,
        ),
        "bequeathed_formae": bequeathed_formae_document,
        "initial_inventory": initial_inventory_document,
        "errors": 0,
        "spoiler_file": workspace.spoiler_log.name if options.generate_spoiler_log else None,
        "spoiler_txt_file": spoiler_text_path.name if spoiler_text_path is not None else None,
        "output_files": [
            path.resolve().relative_to(output_root.resolve()).as_posix()
            for path in output_files
        ],
        "output_filenames": [
            path.name for path in (package_result.files if package_result is not None else output_files)
        ],
        "package": None if package_result is None else {
            "name": package_result.package_name,
            "files": [path.resolve().relative_to(workspace.root.resolve()).as_posix()
                      for path in package_result.files],
            "validated_package_paths": len(package_result.package_paths),
        },
        "installation": {
            "requested": options.install_after_generation,
            "status": installation_status,
            "directory": installation_directory,
            "files": [] if installation_result is None else [path.name for path in installation_result.files],
        },
    }
    _atomic_write_text(workspace.manifest, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


# Return content availability summary
def _content_availability_summary(catalogs: RuntimeCatalogs) -> dict[str, object]:
    """Expose class coverage as metadata; source availability controls eligibility."""
    # Count records by class
    def class_counts(values: Iterable[str]) -> dict[str, int]:
        counts = {name: 0 for name in ("BASE", "DLC", "AFTER_ENDING", "UNKNOWN")}
        for value in values:
            key = value.strip().upper() if isinstance(value, str) else "UNKNOWN"
            counts[key if key in counts else "UNKNOWN"] += 1
        return counts

    world_where = class_counts(location.content_class for location in catalogs.world_loot)
    world_what = class_counts(
        reward.content_class for location in catalogs.world_loot for reward in location.rewards
    )
    materials_where = class_counts(location.where_content_class for location in catalogs.materials)
    materials_resource = class_counts(location.resource_content_class for location in catalogs.materials)
    materials_reward = class_counts(location.reward_content_class for location in catalogs.materials)
    materials_article = class_counts(location.reward.content_class for location in catalogs.materials)
    shop_where = class_counts(product.where_content_class for product in catalogs.shops)
    shop_what = class_counts(product.what_content_class for product in catalogs.shops)

    return {
        "eligibility_rule": "exact source-package availability plus existing structural and safety rules",
        "world_loot": {
            "where": world_where,
            "what": world_what,
            "unknown_is_blocking": False,
        },
        "materials": {
            "where": materials_where,
            "resource_what": materials_resource,
            "reward_what": materials_reward,
            "article_what": materials_article,
            "unknown_is_blocking": False,
        },
        "shops": {
            "where": shop_where,
            "what": shop_what,
            "unknown_is_blocking": False,
        },
    }


# Build spoiler metadata
def _spoiler_metadata(detection: VersionDetection) -> dict[str, str]:
    """Return readable game version and validation status for the spoiler."""
    metadata = {
        "App Version": f"v{__version__}",
        "Game Version": (
            detection.display_version
            or ("Local Validation (unverified)" if detection.local_unverified else None)
            or detection.version_id
            or "Unknown"
        ),
    }
    if detection.local_unverified:
        metadata["Version Validation"] = "Local, unverified"
    return metadata


# Find spoiler settings
def _spoiler_settings(
    options: GenerationOptions,
    dialogue_skip_result: DialogueSkipResult | None = None,
) -> tuple[str, ...]:
    """Return every player-facing option as a short Enabled/Disabled line."""
    settings = (
        ("World Loot (always on)", options.randomize_world_loot),
        ("Initial Inventory", options.randomize_initial_inventory),
        ("Blood Codes", options.randomize_blood_codes),
        ("Mix Boosters with World Loot", options.randomize_boosters),
        ("Booster Extractor", options.randomize_extractor_recipes),
        ("Enemy Drops", options.randomize_enemy_drops),
        ("Healing Items", options.randomize_healing),
        ("Renewable Ingredients", options.randomize_materials),
        ("Shops and NPC Traders", options.randomize_shops),
        ("Mix Shop rewards with World Loot", options.mix_shops_with_world),
        ("Unlock all 6 Booster Slots", options.unlock_all_booster_slots),
        ("Unlock all NPC Trader Shop Tiers", options.unlock_npc_trader_shops),
        ("Unlock Motorcycle at Start", options.motorcycle_unlocked),
        ("Unlock Second Weapon Slot", options.second_weapon_unlocked),
        ("Disable Tutorial Pop-ups", options.disable_tutorials),
        (
            "Tap Instead of Holding to Skip Dialogue",
            options.tap_to_skip_dialogue and (
                dialogue_skip_result is None or dialogue_skip_result.applied
            ),
        ),
        ("Unrestricted Blood Code Mastery", options.low_blood_code_mastery_exp),
        ("Cheaper Booster Extractor", options.cheaper_booster_extractor),
    )
    return tuple(
        f"{'Enabled' if enabled else 'Disabled'} {label}"
        for label, enabled in settings
    )


# Write spoiler
def _write_spoiler(
    workspace: GenerationWorkspace,
    options: GenerationOptions,
    world_plan: WorldLootRandomizationPlan,
    shop_plan: ShopRandomizationPlan,
    material_plan: MaterialRandomizationPlan,
    enemy_drop_plan: EnemyDropPlan | None = None,
    blood_code_plan: BloodCodePlan | None = None,
    booster_plan: BoosterPlan | None = None,
    defensive_gadget_plan: DefensiveGadgetNpcPlan | None = None,
    bequeathed_formae_plan: BequeathedFormaPlan | None = None,
    extractor_plan: ExtractorPlan | None = None,
    initial_inventory_plan: InitialInventoryPlan | None = None,
) -> Path | None:
    if not options.generate_spoiler_log:
        return None
    rows: list[tuple[str, str, str, str, str, str, str, str]] = []
    for assignment in world_plan.assignments:
        destination = assignment.destination
        original = ";".join(reward.article_path for reward in destination.rewards)
        assigned = ";".join(reward.article_path for reward in assignment.assigned_rewards)
        source_candidate = assignment.source_candidate
        candidate_note = ""
        if source_candidate is not None:
            candidate_note = f"reward_candidate={source_candidate.candidate_id}"
            reward_categories = source_candidate.reward_categories or (
                (source_candidate.candidate_category,)
                if source_candidate.candidate_category is not None else ()
            )
            for category in dict.fromkeys(value for value in reward_categories if value is not None):
                candidate_note += f"; category={category}"
            if source_candidate.candidate_id.startswith("global-article:shop:"):
                candidate_note += "; Shop Article identity; World Count/notification remain at destination"
            elif source_candidate.candidate_id.startswith("global-article:"):
                candidate_note += "; Article-only global assignment; World Count/notification remain at destination"
            if source_candidate.candidate_id.startswith("hot-spring-repeat-recipe-filler:"):
                candidate_note += "; repeated one-time recipe unlock replaced with ordinary World Loot"
        rows.append((
            "WORLD_LOOT", destination.display_name or f"{destination.package_path}#{destination.export_index}",
            original, assigned, "", "", "",
            candidate_note,
        ))
    for assignment in material_plan.assignments:
        destination = assignment.destination
        source = assignment.source
        if (
            destination.mirror_of_package_path is not None
            or destination.resource_class_package.casefold() == source.resource_class_package.casefold()
        ):
            continue
        rows.append((
            "RENEWABLE_INGREDIENT",
            f"{destination.package_path}#{destination.export_index}",
            destination.resource_class_package,
            source.resource_class_package,
            "", "", "", "destination reward and UniqueSaveID stay local",
        ))
    shop_spoiler_assignments = shop_plan.assignments
    for assignment in shop_spoiler_assignments:
        destination = assignment.destination
        source = assignment.source
        destination_is_trader = is_npc_trader_row(destination.family, destination.shop_row)
        stock_note = "stock set to 1" if assignment.stock_override is not None else "destination stock preserved"
        if assignment.article_only:
            assigned_path = source.canonical_path
            source_note = f"origin={source.origin_kind}:{source.origin_label}; Article identity only; destination price/unlock preserved; {stock_note}"
        else:
            assigned_path = source.article_path
            source_note = f"origin=SHOP:{source.shop_row}:{source.product_index}; Article identity only; destination price/unlock preserved; {stock_note}"
        stock_behavior = destination.uses_stock
        stock_count = assignment.stock_override if assignment.stock_override is not None else destination.stock
        rows.append((
            "NPC_TRADER" if destination_is_trader else "SHOP",
            f"{destination.shop_row.rsplit('.', 1)[-1]} [{destination.product_index}]"
            if destination_is_trader else
            f"{destination.family}:{destination.shop_row}:{destination.product_index}",
            destination.article_path,
            assigned_path,
            str(destination.price), str(stock_behavior), str(stock_count), source_note,
        ))
    if enemy_drop_plan is not None:
        for assignment in enemy_drop_plan.assignments:
            destination = assignment.destination
            source = assignment.source
            rows.append((
                "ENEMY_DROP",
                f"{destination.table}:{destination.row_id}:{destination.slot_index}",
                destination.bundle_fingerprint,
                source.bundle_fingerprint,
                "", "", "",
                f"source={source.row_id}[{source.slot_index}]; full Articles[] bundle; Weight preserved",
            ))
    if blood_code_plan is not None:
        for assignment in blood_code_plan.assignments:
            destination = assignment.destination
            source = assignment.source
            rows.append((
                "BLOOD_CODE",
                destination.display_name or f"{destination.virtual_package}#{destination.export_index}:{destination.export_name}",
                destination.original_blood_code_package.rsplit("/", 1)[-1],
                source.original_blood_code_package.rsplit("/", 1)[-1],
                "", "", "", destination.content_class,
            ))
    if booster_plan is not None:
        for assignment in booster_plan.assignments:
            destination = assignment.destination
            source = assignment.source
            rows.append((
                "BOOSTER",
                destination.row_name,
                destination.original_reward.story_flag,
                source.original_reward.story_flag,
                "", "", "", f"{destination.content_class}; mastery={destination.mastery_owner} {destination.mastery_rank}",
            ))
    if extractor_plan is not None:
        for assignment in extractor_plan.assignments:
            rows.append((
                "EXTRACTOR_BOOSTER_RECIPE",
                f"{assignment.destination.table_name}:{assignment.destination.row_name}",
                assignment.destination.original_article_path,
                assignment.assigned_reward.article_path,
                "", "", "",
                f"{assignment.destination.content_class}; immutable recipe fields preserved",
            ))
    if initial_inventory_plan is not None:
        for assignment in initial_inventory_plan.assignments:
            rows.append((
                "INITIAL_INVENTORY",
                assignment.display_name,
                assignment.vanilla_article_path,
                assignment.assigned_article_path,
                "", "", "",
                f"canonical identity={assignment.assigned_article_path.rsplit('/', 1)[-1]}; category={assignment.category}",
            ))
    if defensive_gadget_plan is not None:
        for assignment in defensive_gadget_plan.assignments:
            rows.append((
                "DEFENSIVE_GADGET_NPC",
                assignment.destination.slot_id,
                assignment.original.object,
                assignment.assigned.object,
                "", "", "",
                f"{assignment.destination.family}; {assignment.destination.content_class}; direct NPC/quest reward",
            ))
        for pending in defensive_gadget_plan.pending_grants:
            rows.append((
                "DEFENSIVE_GADGET_NPC_PENDING",
                pending,
                "", "", "", "", "",
                "Exact source slot not structurally confirmed; left vanilla/empty",
            ))
    if bequeathed_formae_plan is not None:
        for assignment in bequeathed_formae_plan.assignments:
            rows.append((
                "BEQUEATHED_FORMA_QUEST",
                assignment.destination.logical_reward_group or assignment.destination.object,
                assignment.destination.game_name,
                assignment.source.game_name,
                "", "", "",
                "physical_grants=" + ";".join(
                    grant.relative_file for grant in assignment.destination.direct_grants
                ),
            ))
    temporary = workspace.spoiler_log.with_name(workspace.spoiler_log.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow((
                "feature", "destination", "original", "assigned", "preserved_price",
                "assigned_uses_stock", "assigned_stock", "notes",
            ))
            writer.writerows(rows)
        temporary.replace(workspace.spoiler_log)
    finally:
        temporary.unlink(missing_ok=True)
    return workspace.spoiler_log


# Read simple spoiler entries
def _simple_spoiler_entries(
    world_plan: WorldLootRandomizationPlan,
    shop_plan: ShopRandomizationPlan,
    material_plan: MaterialRandomizationPlan,
    enemy_drop_plan: EnemyDropPlan | None,
    blood_code_plan: BloodCodePlan | None,
    booster_plan: BoosterPlan | None,
    defensive_gadget_plan: DefensiveGadgetNpcPlan | None,
    bequeathed_formae_plan: BequeathedFormaPlan | None,
    extractor_plan: ExtractorPlan | None,
    initial_inventory_plan: InitialInventoryPlan | None,
) -> tuple[SpoilerEntry, ...]:
    """Build short, human-readable replacements without exposing patch diagnostics."""
    entries: list[SpoilerEntry] = []
    for assignment in world_plan.assignments:
        destination = assignment.destination
        # Alternative MainWorld/DlcWorld copies share one logical pickup. The
        # canonical destination is enough to tell the player where its reward is.
        if destination.mirror_of_package_path is not None:
            continue
        assigned_rewards = assignment.assigned_rewards
        original, original_label = _world_payload_identity_and_label(destination.rewards)
        assigned, assigned_label = _world_payload_identity_and_label(assigned_rewards)
        system = _world_spoiler_system(
            destination,
            assigned_rewards,
            assignment.source_candidate,
        )
        destination_label = _world_spoiler_location(destination)
        entries.append(SpoilerEntry(
            system, original, assigned,
            destination_label,
            randomized_label=assigned_label,
            vanilla_label=original_label,
            stable_sort_key=(
                f"{destination.package_path}#{destination.export_index}:{destination.export_name}"
            ),
        ))
    ingredient_type_mappings: dict[tuple[str, str], list[MaterialAssignment]] = {}
    for assignment in material_plan.assignments:
        destination, source = assignment.destination, assignment.source
        if destination.mirror_of_package_path is not None:
            continue
        original = destination.resource_class_package.casefold()
        randomized = source.resource_class_package.casefold()
        if original == randomized:
            continue
        ingredient_type_mappings.setdefault((original, randomized), []).append(assignment)
    for (original, randomized), assignments in sorted(ingredient_type_mappings.items()):
        destination, source = assignments[0].destination, assignments[0].source
        entries.append(SpoilerEntry(
            "Ingredients",
            destination.reward.article_path,
            source.reward.article_path,
            f"All {len(assignments)} ground pickups",
            stable_sort_key=f"{original}->{randomized}",
            summary_count=len(assignments),
        ))
    entries.extend(_shop_spoiler_entry(assignment) for assignment in shop_plan.assignments)
    if enemy_drop_plan is not None:
        for assignment in enemy_drop_plan.assignments:
            destination, source = assignment.destination, assignment.source
            entries.append(SpoilerEntry(
                "Enemy Drops",
                destination.bundle_fingerprint,
                source.bundle_fingerprint,
                destination.row_id,
                vanilla_label=_label_bundle(destination.reward_labels),
                randomized_label=_label_bundle(source.reward_labels),
                stable_sort_key=f"{destination.table}:{destination.row_id}:{destination.slot_index}",
            ))
    if blood_code_plan is not None:
        for assignment in blood_code_plan.assignments:
            destination = assignment.destination
            if destination.display_name:
                location = destination.display_name
            elif destination.location_kind == "gain_item":
                location = _quest_spoiler_location(
                    destination.virtual_package,
                    destination.export_index,
                )
            else:
                location = f"Blood Code reward ({destination.location_kind.replace('_', ' ')})"
            entries.append(SpoilerEntry(
                "Blood Codes",
                destination.original_blood_code_package,
                assignment.source.original_blood_code_package,
                location,
                stable_sort_key=(
                    f"{destination.virtual_package}#{destination.export_index}:"
                    f"{destination.export_name}:{destination.location_kind}:"
                    f"{destination.bulk_index}:{destination.reward_index}:{destination.reference_property}"
                ),
            ))
    if booster_plan is not None:
        for assignment in booster_plan.assignments:
            entries.append(SpoilerEntry(
                "Boosters",
                assignment.destination.original_reward.article_path,
                assignment.assigned_reward.article_path,
                assignment.destination.row_name,
                stable_sort_key=f"{assignment.destination.row_index}:{assignment.destination.row_name}",
            ))
    if extractor_plan is not None:
        for assignment in extractor_plan.assignments:
            entries.append(SpoilerEntry(
                "Extractor", assignment.destination.original_article_path,
                assignment.assigned_reward.article_path,
                assignment.destination.row_name,
                stable_sort_key=(
                    f"{assignment.destination.virtual_package}#{assignment.destination.row_index}:"
                    f"{assignment.destination.row_name}"
                ),
            ))
    if initial_inventory_plan is not None:
        for assignment in initial_inventory_plan.assignments:
            entries.append(SpoilerEntry(
                "Initial Inventory", assignment.vanilla_article_path,
                assignment.assigned_article_path, assignment.display_name,
                stable_sort_key=assignment.slot_id,
            ))
    if defensive_gadget_plan is not None:
        for assignment in defensive_gadget_plan.assignments:
            entries.append(SpoilerEntry(
                "Defensive Gadgets", assignment.original.object,
                assignment.assigned.object, assignment.destination.slot_id,
                stable_sort_key=assignment.destination.slot_id,
            ))
    if bequeathed_formae_plan is not None:
        for assignment in bequeathed_formae_plan.assignments:
            entries.append(SpoilerEntry(
                "Bequeathed Formae", assignment.destination.asset_path,
                assignment.source.asset_path,
                assignment.destination.logical_reward_group or assignment.destination.game_name,
                randomized_label=item_label(assignment.source.asset_path),
                vanilla_label=item_label(assignment.destination.asset_path),
                stable_sort_key=(assignment.destination.logical_reward_group or assignment.destination.object),
            ))
    return tuple(entries)


# Find shop spoiler entry
def _shop_spoiler_entry(assignment: ShopAssignment) -> SpoilerEntry:
    """Build a destination-oriented entry with the exact assigned Shop Article."""
    destination, source = assignment.destination, assignment.source

    # Resolve exact identity
    def exact_identity(
        article_path: str,
        path_number: int,
        item_object: str,
        object_number: int,
    ) -> tuple[str, str]:
        package_name = FNameIdentity(article_path, path_number).resolved
        object_name = FNameIdentity(item_object, object_number).resolved
        return f"{package_name}.{object_name}", item_object

    # Build slot label
    def slot_label(family: str, row: str, index: int) -> str:
        if is_npc_trader_row(family, row):
            trader = row.rsplit(".", 1)[-1]
            return f"{trader} NPC Trader, slot {index + 1}"
        if row.startswith("Shop_MM_"):
            return f"MagMell shop, slot {index + 1}"
        if row.startswith("Shop_Jadwiga_"):
            shop_type = row.removeprefix("Shop_Jadwiga_").replace("_", " ")
            shop_type = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", shop_type)
            return f"MagMell, Jadwiga's {shop_type.title()} shop, slot {index + 1}"
        row_label = row.removeprefix("Shop_").replace("_", " ")
        row_label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", row_label).strip()
        return f"{family.title()} shop, {row_label}, slot {index + 1}"

    original, original_label = exact_identity(
        destination.article_path,
        destination.article_path_number,
        destination.item_object,
        destination.item_object_number,
    )
    randomized, randomized_label = exact_identity(
        source.article_path,
        source.article_path_number,
        source.item_object,
        source.item_object_number,
    )

    return SpoilerEntry(
        "NPC Traders" if is_npc_trader_row(destination.family, destination.shop_row) else "Shops",
        original,
        randomized,
        slot_label(destination.family, destination.shop_row, destination.product_index),
        randomized_label=randomized_label,
        stable_sort_key=f"{destination.family.upper()}:{destination.shop_row}:{destination.product_index}",
        vanilla_label=original_label,
    )


# Find world payload identity and label
def _world_payload_identity_and_label(rewards: Iterable) -> tuple[str, str]:
    payloads = tuple(rewards)
    identity = "\x1f".join(
        f"{reward.article_path.casefold()}|{reward.count}|{int(reward.notification)}"
        for reward in payloads
    )
    labels = []
    for reward in payloads:
        label = item_label(reward.article_path)
        if reward.count > 1:
            label = f"{label} x{reward.count}"
        labels.append(label)
    return identity or "empty", " + ".join(labels) if labels else "Empty"


# Format a World Loot location label
def _world_spoiler_location(destination: WorldLootLocation) -> str:
    """Use a clean curated label or a compact reference without internal package codes."""
    if destination.display_name:
        label = re.split(r"/game/", destination.display_name, maxsplit=1, flags=re.IGNORECASE)[0]
        label = re.sub(r"[\s—–:|\-]+$", "", label).strip()
        if label:
            return label

    package = destination.package_path
    folded = package.casefold()
    if "/map/mainworld/" in folded:
        area = "DLC World" if "/dlcworld/" in folded else "Main World"
        return f"{area} pickup #{destination.export_index}"

    if "/quest/" in folded:
        return _quest_spoiler_location(package, destination.export_index)

    return f"World Loot, {destination.export_name} #{destination.export_index}"


# Format a quest location label
def _quest_spoiler_location(package: str, export_index: int) -> str:
    """Render a quest grant as a short player-facing label, never a UE property path."""
    folded = package.casefold()
    character = next(
        (name for name in ("Josee", "Noah", "Lou", "Lyle", "Holly", "Zenon", "Iris", "Jadwiga")
         if name.casefold() in folded),
        None,
    )
    if character:
        return f"Quest {character}, reward #{export_index}"
    package_name = package.rstrip("/").rsplit("/", 1)[-1]
    package_name = re.sub(r"^(QST_|QST)", "", package_name)
    package_name = re.sub(r"_e\d+$", "", package_name, flags=re.IGNORECASE)
    package_name = re.sub(r"(?<=[a-z])(?=[A-Z])|_", " ", package_name).strip()
    if not package_name:
        return f"Quest reward #{export_index}"
    return f"Quest {package_name}, reward #{export_index}"


# Group spoiler entries by item type
def _world_spoiler_system(
    destination: WorldLootLocation,
    rewards: tuple[RewardPayload, ...],
    source_candidate: WorldLootRewardCandidate | None,
) -> str:
    """Group cross-pool items under their item category while keeping other rewards by source."""
    candidate_categories: tuple[str | None, ...] = ()
    if source_candidate is not None:
        candidate_categories = source_candidate.reward_categories or (
            (source_candidate.candidate_category,) * len(rewards)
            if source_candidate.candidate_category is not None else ()
        )
    if (
        len(candidate_categories) == len(rewards)
        and candidate_categories
        and all(category is not None for category in candidate_categories)
    ):
        normalized = {category.casefold() for category in candidate_categories}
        if len(normalized) == 1:
            section = _WORLD_SPOILER_CATEGORY_BY_CANDIDATE.get(next(iter(normalized)))
            if section is not None:
                return section

    item_sections = tuple(spoiler_item_category(reward.article_path) for reward in rewards)
    if item_sections and all(section is not None for section in item_sections):
        unique_sections = set(item_sections)
        if len(unique_sections) == 1:
            return next(iter(unique_sections))

    if destination.location_type in {
        "HEALING_UPGRADE_GOLDEN_SEED",
        "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
    }:
        return "Healing"
    return "World Loot"


# Read label bundle
def _label_bundle(labels: Iterable[str]) -> str:
    return " + ".join(item_label(label) for label in labels if label.strip()) or "Unknown reward bundle"


# Build vanilla cache manifest
def _vanilla_cache_manifest(metadata: dict[str, object] | None) -> dict[str, object] | None:
    if metadata is None:
        return None
    keys = (
        "cache_format_version", "cache_revision", "game_version",
        "game_executable_sha256", "game_archive_sha256", "catalog_revision",
        "extracted_at_utc", "required_sources", "available_sources",
        "unavailable_optional_sources", "invalid_sources", "extracted_package_inventory",
    )
    return {key: metadata.get(key) for key in keys}


# Build important items manifest
def _important_items_manifest(catalogs: RuntimeCatalogs) -> dict[str, object]:
    exclusions = catalogs.important_item_exclusions
    audit = catalogs.important_item_audit or {}
    return {
        "key_exclusions_in_metadata": 0 if exclusions is None else len(exclusions.keys),
        "quest_flag_exclusions_in_metadata": 0 if exclusions is None else len(exclusions.quest_flag_exclusions),
        "eligible_article_count": audit.get("eligible_articles", 0),
        "eligible_reward_occurrences": audit.get("eligible_occurrences", 0),
        "key_excluded_reward_occurrences": audit.get("key_excluded_occurrences", 0),
        "quest_flag_excluded_reward_occurrences": audit.get("quest_flag_excluded_occurrences", 0),
        "other_safety_locked_reward_occurrences": audit.get("other_safety_locked_occurrences", 0),
        "world_locations_unlocked": audit.get("important_world_locations_unlocked", 0),
        "shop_products_unlocked": audit.get("important_shop_products_unlocked", 0),
        "world_locations_other_safety_locked": audit.get("important_world_locations_other_locked", 0),
        "shop_products_other_safety_locked": audit.get("important_shop_products_other_locked", 0),
        "classification": "validated_exact_identity_policy",
        "actual_reward_classifications": list(catalogs.important_item_classifications),
    }


# Count enemy drop manifest counts
def _enemy_drop_manifest_counts(
    plan: EnemyDropPlan | None,
    result: EnemyDropGenerationResult | None,
) -> dict[str, object]:
    if plan is None:
        return {
            "enabled": False,
            "catalog_revision": None,
            "assignments": 0,
            "changed_slots": 0,
            "unchanged_slots": 0,
            "eligible_rows": 0,
            "eligible_slots": 0,
            "excluded_rows": 0,
            "excluded_slots": 0,
            "active_tables": [],
            "patched_tables": [],
            "reopened": False,
        }
    total_rows = sum(table.source_rows for table in plan.table_stats)
    total_slots = sum(table.source_slots for table in plan.table_stats)
    return {
        "enabled": True,
        "catalog_revision": plan.catalog_revision,
        "assignments": len(plan.assignments),
        "changed_slots": plan.changed_slots,
        "unchanged_slots": plan.unchanged_slots,
        "eligible_rows": plan.eligible_rows,
        "eligible_slots": plan.eligible_slots,
        "excluded_rows": total_rows - plan.eligible_rows,
        "excluded_slots": total_slots - plan.eligible_slots,
        "active_tables": list(plan.active_tables),
        "patched_tables": [] if result is None else list(result.patched_tables),
        "reopened": False if result is None else result.reopened,
    }


# Count blood code manifest counts
def _blood_code_manifest_counts(
    plan: BloodCodePlan | None,
    result: BloodCodeGenerationResult | None,
) -> dict[str, object]:
    if plan is None:
        return {"enabled": False, "catalog_revision": None, "assignments": 0, "changed_slots": 0, "self_maps": 0, "patched_packages": [], "reopened": False}
    return {
        "enabled": True,
        "catalog_revision": plan.catalog_revision,
        "assignments": len(plan.assignments),
        "changed_slots": plan.changed_slots,
        "self_maps": plan.self_maps,
        "pools": [{"content_class": pool.content_class, "slots": pool.slot_count, "changed": pool.changed_count} for pool in plan.pool_stats],
        "patched_packages": [] if result is None else list(result.patched_packages),
        "reopened": False if result is None else result.reopened,
    }


# Count booster manifest counts
def _booster_manifest_counts(
    plan: BoosterPlan | None,
    result: BoosterGenerationResult | None,
) -> dict[str, object]:
    if plan is None:
        return {"enabled": False, "catalog_revision": None, "assignments": 0, "changed_rows": 0, "self_maps": 0, "patched_rows": 0, "reopened": False}
    return {
        "enabled": True,
        "catalog_revision": plan.catalog_revision,
        "assignments": len(plan.assignments),
        "changed_rows": plan.changed_slots,
        "self_maps": plan.self_maps,
        "pools": [{"content_class": pool.content_class, "rows": pool.slot_count, "changed": pool.changed_count} for pool in plan.pool_stats],
        "patched_rows": 0 if result is None else result.patched_rows,
        "reopened": False if result is None else result.reopened,
    }


# Count extractor manifest counts
def _extractor_manifest_counts(
    plan: ExtractorPlan | None,
    result: ExtractorGenerationResult | None,
    *,
    randomize_recipes: bool = False,
) -> dict[str, object]:
    return {
        "enabled": plan is not None,
        "randomize_recipes": randomize_recipes,
        "catalog_revision": None if plan is None else plan.catalog_revision,
        "base_destinations": 0 if plan is None else sum(item.destination.content_class == "BASE" for item in plan.assignments),
        "dlc_destinations": 0 if plan is None else sum(item.destination.content_class == "DLC" for item in plan.assignments),
        "assignments": 0 if plan is None else len(plan.assignments),
        "changed_rows": 0 if plan is None else plan.changed_rows,
        "self_maps": 0 if plan is None else plan.self_maps,
        "canonical_booster_candidates_before_allocation": 0 if plan is None else plan.canonical_candidate_count,
        "allocated_unique_boosters": 0 if plan is None else len(plan.assignments),
        "unallocated_boosters_returned_to_world_pool": 0 if plan is None else len(plan.remaining_candidates),
        "pools": [{"content_class": item.pool, "candidates": item.candidate_count,
                   "destinations": item.destination_count, "unallocated": item.unallocated_count}
                  for item in plan.pool_stats] if plan is not None else [],
        "patched_rows": 0 if result is None else result.patched_rows,
        "patched_tables": [] if result is None else list(result.table_packages),
        "reopened": False if result is None else result.reopened,
    }


# Build NPC trader manifest
def _npc_trader_manifest(summary: NpcTraderPlanSummary | None) -> dict[str, object]:
    if summary is None:
        return {"included_in_shops": False, "available_rows": 0, "available_slots": 0,
                "eligible_slots": 0, "assignments": 0, "locked": 0, "unvalidated": 0,
                "reserved_reward_slots": 0}
    return {
        "included_in_shops": True,
        "mixed_with_world_pool": summary.mode == "mixed",
        "available_families": list(summary.available_families),
        "unavailable_families": list(summary.unavailable_families),
        "available_rows": len(summary.available_rows),
        "row_ids": list(summary.available_rows),
        "available_slots": summary.available_slots,
        "eligible_slots": summary.eligible_slots,
        "assignments": len(summary.assignments),
        "locked": len(summary.locked_slots),
        "unvalidated": len(summary.unvalidated_slots),
        "disabled": len(summary.disabled_slots),
        "reserved_reward_slots": len(summary.reserved_slots),
        "cross_companion_assignments": summary.cross_companion_assignments,
        "trader_from_world": summary.trader_from_world,
        "world_from_trader": summary.world_from_trader,
        "trader_from_shop": summary.trader_from_shop,
        "shop_from_trader": summary.shop_from_trader,
        "only_mode_payload": "ArticleDataPath + item FName",
        "mixed_mode_payload": "Article identity only",
        "destination_metadata": [
            "Price", "bIsUseStock", "Stock", "row/NPC", "product_index", "ReplenishInfo",
            "evaluation_formula", "evaluation_raw",
        ],
    }


# Find shop protection diagnostics
def _shop_protection_diagnostics(
    plan: ShopRandomizationPlan,
    *,
    include_stock_locks: bool = True,
) -> list[dict[str, object]]:
    """Summarize protected, infinite, and stock-uncertain rows for diagnostics."""
    grouped: dict[tuple[str, str, str], set[int]] = {}
    for product in (*plan.locked_slots, *plan.disabled_slots, *plan.unvalidated_slots):
        if not include_stock_locks and not product.protected:
            continue
        if product in plan.disabled_slots and not product.protected:
            continue
        reason = shop_slot_lock_reason(product)
        if reason is None:
            continue
        key = (product.family.upper(), product.shop_row, reason)
        grouped.setdefault(key, set()).add(product.product_index)
    return [
        {
            "family": family,
            "row": row,
            "reason": reason,
            "slot_count": len(indices),
            "product_indices": sorted(indices),
        }
        for (family, row, reason), indices in sorted(grouped.items())
    ]


# Build initial inventory manifest
def _initial_inventory_manifest(
    plan: InitialInventoryPlan | None,
    preparation: InitialInventoryPreparation | None,
    result: InitialInventoryGenerationResult | None,
    *,
    enabled: bool,
    motorcycle_unlocked: bool,
    dry_run: bool,
    catalog_revision: str,
) -> dict[str, object]:
    status = (
        "not_requested" if not enabled and not motorcycle_unlocked else
        "skipped_unavailable_or_invalid" if preparation is None else
        "plan_only" if dry_run else
        "reopened_and_validated" if result is not None and result.reopened else
        "not_validated"
    )
    source_fingerprint = (
        preparation.source_fingerprint if preparation is not None else
        result.source_fingerprint if result is not None else None
    )
    assignments = [] if plan is None else [
        {
            "slot_id": item.slot_id,
            "category": item.category,
            "vanilla_article_path": item.vanilla_article_path,
            "canonical_identity": item.assigned_article_path.rsplit("/", 1)[-1],
            "assigned_article_path": item.assigned_article_path,
        }
        for item in plan.assignments
    ]
    return {
        "enabled": enabled,
        "motorcycle_unlock_enabled": motorcycle_unlocked,
        "motorcycle_forma_added": bool(result and result.motorcycle_unlocked),
        "status": status,
        "source_package": INITIAL_INVENTORY_PACKAGE,
        "source_fingerprint_sha256": source_fingerprint,
        "catalog_revision": catalog_revision,
        "availability": "available_and_structurally_validated" if preparation is not None else status,
        "candidate_counts": {} if plan is None else dict(plan.candidate_counts),
        "rng_domains": [] if plan is None else list(plan.rng_domains),
        "assignments": assignments,
        "patched_slots": 0 if result is None else result.patched_slots,
        "reopened_and_validated": False if result is None else result.reopened,
        "staged_files": [] if result is None else [path.name for path in result.output_files],
        "preserved_vanilla_entries": [
            "DA_BodyLight", "DA_Item_BasicRecovery", "DA_BloodCode_Lou_01",
        ],
    }


# Handle initial inventory preparation failure
def _handle_initial_inventory_preparation_failure(
    error: Exception,
    *,
    availability_warnings: list[str],
) -> None:
    availability_warnings.append(f"Initial Inventory skipped: source occurrence transfer failed: {error}")


# Build direct world grants manifest
def _direct_world_grants_manifest(
    world_plan: WorldLootRandomizationPlan,
    results: tuple[WorldLootDirectGrantResult, ...],
    *,
    enabled: bool,
    dry_run: bool,
    output_root: Path,
) -> dict[str, object]:
    matches = tuple(
        assignment for assignment in world_plan.assignments
        if assignment.destination.destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}
    )
    results_by_id = {result.destination_id: result for result in results}
    if len(results_by_id) != len(results):
        raise GenerationError("Generation returned duplicate direct Article grant results.")
    planned_ids = [assignment.destination.destination_id for assignment in matches]
    if len(set(planned_ids)) != len(planned_ids):
        raise GenerationError("World Loot plan contains duplicate direct Article grant destinations.")
    if not set(results_by_id).issubset(planned_ids):
        raise GenerationError("Generation returned an unplanned direct Article grant result.")
    destinations: list[dict[str, object]] = []
    for assignment in matches:
        destination = assignment.destination
        destination_id = destination.destination_id
        if (
            destination_id is None or not destination.rewards
            or len(assignment.assigned_rewards) != len(destination.rewards)
        ):
            raise GenerationError("World Loot plan has an invalid direct Article grant assignment.")
        originals = [reward.article_path for reward in destination.rewards]
        assigned_paths = [reward.article_path for reward in assignment.assigned_rewards]
        original = originals[0]
        assigned = assigned_paths[0]
        changed = originals != assigned_paths
        result = results_by_id.get(destination_id)
        if not enabled:
            status = "not_requested"
        elif dry_run:
            status = "plan_only"
        elif not changed:
            status = "self_map_no_package_required"
        elif result is not None and result.reopened_and_validated:
            status = "output_reopened_and_validated"
        else:
            status = "not_validated"
        staged_files = [] if result is None else [
            path.resolve().relative_to(output_root.resolve()).as_posix()
            for path in result.output_files
        ]
        destinations.append({
            "destination_id": destination_id,
            "display_name": destination.display_name,
            "virtual_package": destination.package_path,
            "vanilla_reward": original,
            "assigned_reward": assigned,
            "vanilla_rewards": originals,
            "assigned_rewards": assigned_paths,
            "assignments": len(assigned_paths),
            "changed": changed,
            "staged_files": staged_files,
            "reopened_and_validated": None if result is None else result.reopened_and_validated,
            "serialization": (
                "structured ArticleDataPath"
                if destination.destination_kind == "QUEST_ARTICLE_GRANT"
                else "literal canonical path and object in NameMap; both FName Number values are zero"
            ),
            "validation_status": status,
        })
    return {
        "enabled": enabled,
        "assignments": len(destinations),
        "changed": sum(bool(row["changed"]) for row in destinations),
        "destinations": destinations,
    }


# Count defensive gadget manifest counts
def _defensive_gadget_manifest_counts(
    catalog: DefensiveGadgetCatalog | None,
    plan: DefensiveGadgetNpcPlan | None,
    result: DefensiveGadgetGenerationResult | None,
    world_plan: WorldLootRandomizationPlan,
    *,
    options: GenerationOptions,
    dry_run: bool,
) -> dict[str, object]:
    if catalog is None:
        return {
            "catalog_count": 0,
            "base_count": 0,
            "dlc_count": 0,
            "aliases_normalized": {},
            "candidates_excluded_by_content": 0,
            "world_loot_enabled": options.randomize_world_loot,
            "world_loot": {
                "canonical_catalog_candidates": world_plan.gadget_candidate_count,
                "existing_source_occurrences": 0,
                "injected_canonical_candidates": world_plan.gadget_injected_count,
                "duplicate_sources_removed": world_plan.gadget_duplicate_sources,
                "displaced_payloads": world_plan.gadget_displaced_payloads,
                "shared_candidate_overlay_displaced_payloads": world_plan.overlay_displaced_payloads,
            },
            "included_with_world_loot": options.randomize_world_loot,
            "npc_grants": {
                "assignments": 0, "changed": 0, "self_maps": 0, "pending": [],
                "patched_packages": [], "source_packages_inspected": False,
                "output_reopened_validation": None, "validation_status": "not_requested",
            },
        }
    aliases = {
        alias: item.object
        for item in catalog.items
        for alias in item.aliases
    }
    excluded_by_content = 0
    pending = [
        {
            "slot_id": item.slot_id,
            "family": item.family,
            "content_class": item.content_class,
            "status": item.status,
            "source_description": item.source_description,
        }
        for item in catalog.pending_grants
    ]
    if not options.randomize_defensive_gadget_npc_rewards:
        validation_status = "not_requested"
    elif dry_run:
        validation_status = "source_packages_inspected; output_patch_not_performed_dry_run"
    elif result is not None and result.reopened:
        validation_status = "output_reopened_and_validated"
    elif result is not None and not result.patched_packages:
        validation_status = "all_confirmed_assignments_self_map; no_output_package_required"
    else:
        validation_status = "not_validated"
    return {
        "catalog_count": len(catalog.items),
        "base_count": sum(item.content_class == "BASE" for item in catalog.items),
        "dlc_count": sum(item.content_class == "DLC" for item in catalog.items),
        "aliases_normalized": aliases,
        "candidates_excluded_after_ending": excluded_by_content,
        "world_loot_enabled": options.randomize_world_loot,
        "world_loot": {
            "canonical_catalog_candidates": world_plan.gadget_candidate_count,
            "existing_source_occurrences": catalog.world_reward_evidence_count,
            "injected_canonical_candidates": world_plan.gadget_injected_count,
            "duplicate_sources_removed": world_plan.gadget_duplicate_sources,
            "displaced_payloads": world_plan.gadget_displaced_payloads,
            "shared_candidate_overlay_displaced_payloads": world_plan.overlay_displaced_payloads,
        },
        "included_with_world_loot": options.randomize_world_loot,
        "npc_grants": {
            "assignments": 0 if plan is None else len(plan.assignments),
            "changed": 0 if plan is None else plan.changed_slots,
            "self_maps": 0 if plan is None else plan.self_maps,
            "candidate_count": 0 if plan is None else plan.candidate_count,
            "pending": pending,
            "patched_packages": [] if result is None else list(result.patched_packages),
            "source_packages_inspected": plan is not None,
            "output_reopened_validation": None if result is None else result.reopened,
            "validation_status": validation_status,
        },
    }


# Count jail manifest counts
def _jail_manifest_counts(
    catalog: JailCatalog | None,
    world_plan: WorldLootRandomizationPlan,
    *,
    options: GenerationOptions,
) -> dict[str, object]:
    if catalog is None:
        return {
            "enabled_by_world_loot": options.randomize_world_loot,
            "catalog_revision": None,
            "catalog_count": 0,
            "base_count": 0,
            "dlc_count": 0,
            "world_reward_eligible": 0,
            "world_loot": {
                "already_present_in_movable_world_pool": 0,
                "already_present_movable_candidates": [],
                "externally_injected_candidates": [],
                "protected_vanilla_sources": [],
                "duplicate_sources_removed": 0,
                "shared_overlay_displaced_payloads": world_plan.overlay_displaced_payloads,
            },
            "starting_jail_randomization": {"enabled": False},
            "game_assets_packaged": False,
        }
    stats = world_plan.candidate_stats("jail")
    items_by_path = {item.asset_path.casefold(): item for item in catalog.items}
    present_items = [items_by_path[path.casefold()] for path in stats.already_present_identities]
    injected_items = [items_by_path[path.casefold()] for path in stats.injected_identities]
    return {
        "enabled_by_world_loot": options.randomize_world_loot,
        "catalog_revision": catalog.revision,
        "catalog_count": len(catalog.items),
        "base_count": catalog.base_count,
        "dlc_count": catalog.dlc_count,
        "world_reward_eligible": sum(item.world_reward_eligible for item in catalog.items),
        "world_loot": {
            "canonical_candidates": stats.canonical_candidates,
            "already_present_in_movable_world_pool": stats.already_present_count,
            "already_present_movable_candidates": [item.object for item in present_items],
            "externally_injected_candidates": [item.object for item in injected_items],
            "injected_asset_paths": [item.asset_path for item in injected_items],
            "duplicate_sources_removed": stats.duplicate_sources_removed,
            "protected_vanilla_sources": [item.object for item in catalog.protected_world_sources],
            "protected_vanilla_source_count": len(catalog.protected_world_sources),
            "shared_overlay_displaced_payloads": world_plan.overlay_displaced_payloads,
            "injected_payload": {"count": 1, "notification": True},
            "content_policy": "all canonical BASE and DLC Jails follow the existing World Loot pool policy",
        },
        "starting_jail_randomization": {"enabled": False, "vanilla": "DA_Jail_Ogre"},
        "game_assets_packaged": False,
    }


# Count field enemy manifest counts
def _field_enemy_manifest_counts(
    catalog: FieldEnemyCatalog | None,
    world_plan: WorldLootRandomizationPlan,
    *,
    options: GenerationOptions,
    direct_results: tuple[WorldLootDirectGrantResult, ...] = (),
) -> dict[str, object]:
    if catalog is None:
        return {
            "enabled_by_world_loot": options.randomize_world_loot,
            "enabled_by_enemy_drops": options.randomize_enemy_drops,
            "catalog_revision": None,
            "catalog_eligible_locations": 0,
            "protected_exclusions": 0,
            "randomizable_destinations": 0,
            "eligible_where_destinations": 0,
            "disabled_due_to_missing_sources": 0,
            "locked_destinations": 0,
            "unmatched_destinations": 0,
            "required_quest_packages_for_patch": 0,
            "changed_rewards": 0,
            "patched_packages": 0,
            "protected_exclusions_extracted": 0,
            "where_content_classes": {},
            "what_content_classes": {},
            "unknown_what_locations": 0,
        }
    where_content_classes: dict[str, int] = {}
    what_content_classes: dict[str, int] = {}
    for location in catalog.locations:
        where_class = location.content_class.strip().upper()
        where_content_classes[where_class] = where_content_classes.get(where_class, 0) + 1
        reward_class = location.rewards[0].content_class.strip().upper()
        what_content_classes[reward_class] = what_content_classes.get(reward_class, 0) + 1
    destination_ids = catalog.destination_ids
    assignments = [
        assignment for assignment in world_plan.assignments
        if assignment.destination.destination_id in destination_ids
    ]
    disabled_ids = {
        location.destination_id for location in world_plan.disabled_locations
        if location.destination_id in destination_ids
    }
    locked_ids = {
        item.location.destination_id for item in world_plan.locked_locations
        if item.location.destination_id in destination_ids
    }
    unmatched_ids = {
        location.destination_id for location in world_plan.unmatched_locations
        if location.destination_id in destination_ids
    }
    excluded_ids = {
        location.destination_id for location in world_plan.excluded_locations
        if location.destination_id in destination_ids
    }
    active_where_ids = destination_ids - disabled_ids - locked_ids - excluded_ids
    changed = [item for item in direct_results if item.destination_id in destination_ids and item.changed]
    patched_packages = {
        path.stem.casefold()
        for item in changed for path in item.output_files
    }
    return {
        "enabled_by_world_loot": options.randomize_world_loot,
        "enabled_by_enemy_drops": options.randomize_enemy_drops,
        "catalog_revision": catalog.revision,
        "catalog_eligible_locations": len(catalog.locations),
        "eligible_where_destinations": len(active_where_ids),
        "protected_exclusions": len(catalog.protected_exclusions),
        "randomizable_destinations": len(assignments),
        "disabled_due_to_missing_sources": len(disabled_ids),
        "locked_destinations": len(locked_ids),
        "unmatched_destinations": len(unmatched_ids),
        "required_quest_packages_for_patch": len({
            assignment.destination.package_path.casefold()
            for assignment in assignments
            if assignment.assigned_rewards != assignment.destination.rewards
        }),
        "changed_rewards": len(changed),
        "patched_packages": len(patched_packages),
        "protected_exclusions_extracted": 0,
        "canonical_reward_candidates_are_destination_only": True,
        "category_counts": dict(catalog.category_counts),
        "quest_group_counts": dict(catalog.quest_group_counts),
        "where_content_classes": where_content_classes,
        "what_content_classes": what_content_classes,
        "unknown_what_locations": what_content_classes.get("UNKNOWN", 0),
    }


# Build bequeathed formae manifest
def _bequeathed_formae_manifest(
    catalog: BequeathedFormaCatalog | None,
    plan: BequeathedFormaPlan | None,
    result: BequeathedFormaGenerationResult | None,
    *,
    enabled: bool,
    dry_run: bool,
) -> dict[str, object]:
    assignments: list[dict[str, object]] = []
    if plan is not None:
        for assignment in plan.assignments:
            changed = assignment.destination.asset_path.casefold() != assignment.source.asset_path.casefold()
            packages = [grant.relative_file for grant in assignment.destination.direct_grants]
            assignments.append({
                "logical_reward": assignment.destination.logical_reward_group,
                "vanilla": assignment.destination.object,
                "assigned": assignment.source.object,
                "changed": changed,
                "target_packages": packages,
                "patched_packages": packages if changed and result is not None else [],
                "would_patch_packages": packages if changed else [],
            })
    return {
        "enabled": enabled,
        "catalog_revision": None if catalog is None else catalog.revision,
        "catalog_count": 0 if catalog is None else len(catalog.items),
        "base_count": 0 if catalog is None else sum(item.content_class == "BASE" for item in catalog.items),
        "dlc_count": 0 if catalog is None else sum(item.content_class == "DLC" for item in catalog.items),
        "world_sources": 0 if catalog is None else len(catalog.world_sources),
        "shop_sources": 0 if catalog is None else len(catalog.shop_sources),
        "direct_logical_rewards": 0 if catalog is None else len(catalog.direct_rewards),
        "separate_direct_logical_rewards": 0 if catalog is None else len(catalog.separate_direct_rewards),
        "world_loot_direct_logical_rewards": 0 if catalog is None else len(catalog.world_loot_direct_rewards),
        "direct_physical_packages": 0 if catalog is None else catalog.direct_physical_grant_count,
        "separate_direct_physical_packages": 0 if catalog is None else sum(
            len(item.direct_grants) for item in catalog.separate_direct_rewards
        ),
        "world_loot_direct_physical_packages": 0 if catalog is None else sum(
            len(item.direct_grants) for item in catalog.world_loot_direct_rewards
        ),
        "world_shop_mix_enabled": False,
        "source_validation": {
            "world": catalog is not None,
            "shops": catalog is not None,
        },
        "direct_assignments": assignments,
        "patched_packages": [] if result is None else list(result.patched_packages),
        "serialization_reopened": None if result is None else result.reopened,
        "dry_run": dry_run,
        "gameplay_validation": {
            "lou_reward_path": "moved to the shared World Loot quest Article pipeline; in-game retest pending",
            "executioner_reward": "preserved vanilla; the isolated direct pool has no second identity",
        },
    }

# Read atomic write text
def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


# Remove package outputs
def _remove_package_outputs(paths: tuple[Path, ...], packaged_root: Path) -> None:
    root = packaged_root.resolve()
    for path in paths:
        resolved = path.resolve()
        if _is_within(resolved, root) and resolved.is_file():
            resolved.unlink()


# Clear output tree
def _clear_output_tree(output_root: Path, work_root: Path) -> None:
    resolved = output_root.resolve()
    if not _is_within(resolved, work_root.resolve()):
        raise GenerationError("Refusing to clear an output tree outside the generation workspace.")
    if resolved.exists():
        shutil.rmtree(resolved)


# Check path stays under root
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
