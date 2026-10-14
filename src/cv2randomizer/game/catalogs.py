"""Runtime catalog models, integrity checks, and fail-closed loading."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cv2randomizer.core.resources import application_data_path
from .fname_identity import FNameIdentity, SoftObjectPathIdentity
from .important_items import (
    ImportantItemExclusionError,
    ImportantItemExclusionStore,
    ImportantItemExclusions,
)

_CATALOG_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SHOP_FAMILIES = {"NORMAL", "DLC", "LUXURY", "TRADE", "TRADE_DLC"}
_WORLD_MAP_PACKAGE_ROOT = "/Game/Map/"
_WORLD_MAP_PACKAGE_ADDITIONAL_ROOTS = ("/Game/User/", "/Game/Cinematics/")
_LEGACY_GENERATED_MAP_PREFIX = "/Game/Map/MainWorld/MainWorld/_Generated_/"
_HEALING_UPGRADE_ARTICLES = {
    "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryCountUp_1": "HEALING_UPGRADE_GOLDEN_SEED",
    "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryAmountUp_1": "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
}
SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS = "ARTICLE_ONLY_PRESERVE_FLAGS"
SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO = "ARTICLE_ONLY_PRESERVE_SCENARIO"
FLAG_PICKUP_ARTICLE_ONLY_ALLOWLIST = frozenset({
    "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV11_RecoveryOnHit",
    "/Game/Blueprint/Article/Item/Support/DA_Item_DefUpIce_1",
    "/Game/Blueprint/Article/Item/Growth/DA_Item_ExpA_09",
    "/Game/Blueprint/Article/Item/Growth/DA_Item_ExpA_10",
    "/Game/Blueprint/Article/Item/Material/DA_Item_JadwigaCoin_1",
    "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpJailA_02",
    "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpJailA_06",
    "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpJailA_07",
    "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpWeaponA_01",
    "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpWeaponA_07",
    "/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatLevelUpWeaponB_02",
    "/Game/Blueprint/Article/Item/Recover/DA_Item_RemoveRinne_1",
    "/Game/Blueprint/Article/Item/Recover/DA_Item_RemoveSlow_1",
    "/Game/Blueprint/Article/Item/Support/DA_Item_ResUpMoon_1",
    "/Game/Blueprint/Article/Item/Attack/DA_Item_ThrowingKnife_1",
    "/Game/Blueprint/Article/Item/Attack/DA_Item_Trap_Capture_1",
    "/Game/Blueprint/Article/Item/Attack/DA_Item_Trap_Stun_1",
})
_FLAG_PICKUP_ACTOR_CLASSES = frozenset({
    "BP_PickupItem_C",
    "BP_PickupItem_WithFlagChange_C",
    "BP_it00_001_A_0_C",
    "BP_it00_002_A_0_C",
})
_SCRIPTED_REWARD_ONLY_SLOT_SPECS = {
    (
        "/Game/Map/MainWorld/MainWorld/_Generated_/61Y5LAV87NWVD213F35X2VK87".casefold(),
        272,
    ): {
        "source_file": "61Y5LAV87NWVD213F35X2VK87.umap",
        "export_name": "BP_PickupItem_WithFlagChange_C_UAID_A8A159A4A14B9D8902_1822678819_3eb3acb6c4c82637",
        "save_id": "5500DB0E-438B-2632-00A8-A5B994AAFAA2",
        "mirror_of_package_path": None,
        "mirror_of_export_index": None,
    },
    (
        "/Game/Map/MainWorld/DlcWorld/_Generated_/3Z2KPIGCUSLGYDYC5DQHT6AFA".casefold(),
        42,
    ): {
        "source_file": "3Z2KPIGCUSLGYDYC5DQHT6AFA.umap",
        "export_name": "BP_PickupItem_WithFlagChange_C_UAID_A8A159A4A14B9D8902_1822678819_3eb3acb6c4c82637",
        "save_id": "5500DB0E-438B-2632-00A8-A5B994AAFAA2",
        "mirror_of_package_path": "/Game/Map/MainWorld/MainWorld/_Generated_/61Y5LAV87NWVD213F35X2VK87",
        "mirror_of_export_index": 272,
    },
}
_SCRIPTED_SCENARIO_REWARD_SLOT_SPECS = {
    ("/Game/Map/MainWorld/MainWorld/_Generated_/9LFQFB2HAT6FVDAA311S0L2ZD".casefold(), 5): {
        "source_file": "9LFQFB2HAT6FVDAA311S0L2ZD.umap",
        "export_name": "BP_PickupItem_C_UAID_A85E45A159BE92D501_1561481296",
        "actor_class": "BP_PickupItem_C",
        "save_id": "374A2D4D-47A1-0343-C5A0-17BD50660D17",
        "article_path": "/Game/Blueprint/Article/Item/DA_Report_B",
        "mirror_of_package_path": None,
        "mirror_of_export_index": None,
    },
    ("/Game/Map/MainWorld/MainWorld/_Generated_/9LFQFB2HAT6FVDAA311S0L2ZD".casefold(), 6): {
        "source_file": "9LFQFB2HAT6FVDAA311S0L2ZD.umap",
        "export_name": "BP_PickupItem_C_UAID_A85E45A159BE92D501_1561602358",
        "actor_class": "BP_PickupItem_C",
        "save_id": "1D775F9F-410A-8070-F4C6-E095B42346F4",
        "article_path": "/Game/Blueprint/Article/Item/DA_Report_C",
        "mirror_of_package_path": None,
        "mirror_of_export_index": None,
    },
    ("/Game/Map/MainWorld/MainWorld/_Generated_/9LFQFB2HAT6FVDAA311S0L2ZD".casefold(), 7): {
        "source_file": "9LFQFB2HAT6FVDAA311S0L2ZD.umap",
        "export_name": "BP_PickupItem_WithSmallScenario_C_UAID_A85E45A159BE92D501_1562599900",
        "actor_class": "BP_PickupItem_WithSmallScenario_C",
        "save_id": "3D622596-4CF2-AAFD-96F2-45A6D5E124A9",
        "article_path": "/Game/Blueprint/Article/Item/DA_Report_A",
        "mirror_of_package_path": None,
        "mirror_of_export_index": None,
    },
    ("/Game/Map/MainWorld/DlcWorld/_Generated_/AJH345RIDQ5AW61DCGZIYS6DT".casefold(), 5): {
        "source_file": "AJH345RIDQ5AW61DCGZIYS6DT.umap",
        "export_name": "BP_PickupItem_C_UAID_A85E45A159BE92D501_1561481296",
        "actor_class": "BP_PickupItem_C",
        "save_id": "374A2D4D-47A1-0343-C5A0-17BD50660D17",
        "article_path": "/Game/Blueprint/Article/Item/DA_Report_B",
        "mirror_of_package_path": "/Game/Map/MainWorld/MainWorld/_Generated_/9LFQFB2HAT6FVDAA311S0L2ZD",
        "mirror_of_export_index": 5,
    },
    ("/Game/Map/MainWorld/DlcWorld/_Generated_/AJH345RIDQ5AW61DCGZIYS6DT".casefold(), 6): {
        "source_file": "AJH345RIDQ5AW61DCGZIYS6DT.umap",
        "export_name": "BP_PickupItem_C_UAID_A85E45A159BE92D501_1561602358",
        "actor_class": "BP_PickupItem_C",
        "save_id": "1D775F9F-410A-8070-F4C6-E095B42346F4",
        "article_path": "/Game/Blueprint/Article/Item/DA_Report_C",
        "mirror_of_package_path": "/Game/Map/MainWorld/MainWorld/_Generated_/9LFQFB2HAT6FVDAA311S0L2ZD",
        "mirror_of_export_index": 6,
    },
    ("/Game/Map/MainWorld/DlcWorld/_Generated_/AJH345RIDQ5AW61DCGZIYS6DT".casefold(), 7): {
        "source_file": "AJH345RIDQ5AW61DCGZIYS6DT.umap",
        "export_name": "BP_PickupItem_WithSmallScenario_C_UAID_A85E45A159BE92D501_1562599900",
        "actor_class": "BP_PickupItem_WithSmallScenario_C",
        "save_id": "3D622596-4CF2-AAFD-96F2-45A6D5E124A9",
        "article_path": "/Game/Blueprint/Article/Item/DA_Report_A",
        "mirror_of_package_path": "/Game/Map/MainWorld/MainWorld/_Generated_/9LFQFB2HAT6FVDAA311S0L2ZD",
        "mirror_of_export_index": 7,
    },
}
_REQUIRED_FILES = {
    "world_loot.json": ("locations", "world_loot"),
    "materials.json": ("locations", "materials"),
    "shops.json": ("products", "shops"),
    "exclusions.json": ("packages", None),
    "pickup_types.json": ("maps", None),
}


class CatalogError(ValueError):
    """Raised when a catalog is incomplete, mismatched, or unsafe to use."""


# Check world map package
def is_world_map_package(package_path: str) -> bool:
    """Return whether a virtual path is a safe installed map package path."""
    normalized = package_path.casefold()
    roots = (_WORLD_MAP_PACKAGE_ROOT, *_WORLD_MAP_PACKAGE_ADDITIONAL_ROOTS)
    root = next((item for item in roots if normalized.startswith(item.casefold())), None)
    if root is None:
        return False
    relative = package_path[len(root):]
    parts = relative.split("/")
    return bool(relative) and all(
        part not in {"", ".", ".."} and re.fullmatch(r"[A-Za-z0-9_-]+", part)
        for part in parts
    )


# List world map package for source
def world_map_package_for_source(map_file: str, package_path: str | None = None) -> str:
    """Resolve an exact map package, retaining legacy catalogs' MainWorld default."""
    source = PurePosixPath(map_file)
    if (
        not map_file.casefold().endswith(".umap")
        or "\\" in map_file
        or source.is_absolute()
        or any(part in {"", ".", ".."} for part in source.parts)
        or (len(source.parts) > 1 and source.parts[0].casefold() != "map")
    ):
        raise CatalogError("World source file must be a map filename.")
    resolved = package_path or (_LEGACY_GENERATED_MAP_PREFIX + Path(map_file).stem)
    if (
        not is_world_map_package(resolved)
        or resolved.rsplit("/", 1)[-1].casefold() != Path(map_file).stem.casefold()
    ):
        raise CatalogError("World map package must be a safe /Game/Map package matching its source file.")
    return resolved


# Validate a text value
def _string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise CatalogError(f"{label} must be a non-empty string.")
    return value


# Parse an integer value
def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CatalogError(f"{label} must be an integer of at least {minimum}.")
    return value


# Parse a boolean value
def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise CatalogError(f"{label} must be a boolean.")
    return value


# Read a JSON object
def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be an object.")
    return value


# Parse string array
def _string_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CatalogError(f"{label} must be an array.")
    return tuple(_string(item, label) for item in value)


# Return content class
def _content_class(value: Any, label: str) -> str:
    normalized = _string(value, label).upper()
    if normalized not in {"BASE", "DLC", "AFTER_ENDING", "UNKNOWN"}:
        raise CatalogError(f"{label} has an unsupported content class: {normalized}.")
    return normalized


# Compare reward payloads
def _same_reward_payloads(actual: tuple[RewardPayload, ...], expected: tuple[RewardPayload, ...]) -> bool:
    """Compare researched payload identity/flags independently from class metadata."""
    return tuple((item.article_path, item.count, item.notification) for item in actual) == tuple(
        (item.article_path, item.count, item.notification) for item in expected
    )


# Build world loot slot key
def world_loot_slot_key(package_path: str, export_index: int) -> tuple[str, int]:
    """Normalized package/export key for cross-catalog physical mirror checks."""
    return package_path.casefold(), export_index


@dataclass(frozen=True, slots=True)
class RewardPayload:
    """Article identity plus the grant metadata stored at a reward slot."""

    article_path: str
    count: int
    notification: bool
    content_class: str = "BASE"
    soft_object_path_identity: SoftObjectPathIdentity | None = None


@dataclass(frozen=True, slots=True)
class WorldLootLocation:
    """One map actor and the full reward list attached to it."""

    package_path: str
    source_file: str
    export_index: int
    export_name: str
    actor_class: str
    actor_category: str
    save_id: str | None
    protected: bool
    protection_reasons: tuple[str, ...]
    rewards: tuple[RewardPayload, ...]
    destination_kind: str = "ARTICLE_BULK"
    destination_id: str | None = None
    display_name: str | None = None
    raw_article_layout: "RawArticleGrantLayout | None" = None
    quest_article_layout: "QuestArticleGrantLayout | None" = None
    content_class: str = "BASE"
    physical_instance_count: int = 1
    parent_blueprint_package: str | None = None
    parent_blueprint_packages: tuple[str, ...] = ()
    location_type: str = "NORMAL_WORLD_LOOT"
    structural_class_package: str | None = None
    instance_override_source: "WorldLootInstanceOverrideSource | None" = None
    mirror_of_package_path: str | None = None
    mirror_of_export_index: int | None = None
    scripted_reward_mode: str | None = None
    quest_article_layouts: tuple["QuestArticleGrantLayout", ...] = ()

    # Find quest grant layouts
    @property
    def quest_grant_layouts(self) -> tuple["QuestArticleGrantLayout", ...]:
        """Return the validated structured grant layout for each quest reward."""
        if self.quest_article_layouts:
            return self.quest_article_layouts
        return (self.quest_article_layout,) if self.quest_article_layout is not None else ()


# Check valid scripted reward only location
def is_valid_scripted_reward_only_location(location: WorldLootLocation) -> bool:
    """Allow only exact audited scripted pickups with Article-only patch semantics."""
    if (
        location.scripted_reward_mode not in {
            SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS,
            SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO,
        }
        or location.actor_category not in {"GROUND_PICKUP", "TREASURE_BOX"}
        or location.destination_kind != "ARTICLE_BULK"
        or location.protected
        or location.protection_reasons
        or len(location.rewards) != 1
    ):
        return False
    if location.scripted_reward_mode == SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO:
        spec = _SCRIPTED_SCENARIO_REWARD_SLOT_SPECS.get(
            (location.package_path.casefold(), location.export_index),
        )
        if spec is None:
            return False
        article_path = spec["article_path"]
        return bool(
            location.source_file == spec["source_file"]
            and location.export_name == spec["export_name"]
            and location.actor_class == spec["actor_class"]
            and location.save_id == spec["save_id"]
            and location.rewards[0].article_path == article_path
            and location.rewards[0].count == 1
            and location.rewards[0].notification is True
            and location.rewards[0].soft_object_path_identity == SoftObjectPathIdentity.from_literal_path(article_path)
            and location.mirror_of_package_path == spec["mirror_of_package_path"]
            and location.mirror_of_export_index == spec["mirror_of_export_index"]
        )
    spec = _SCRIPTED_REWARD_ONLY_SLOT_SPECS.get(
        (location.package_path.casefold(), location.export_index),
    )
    if spec is not None:
        return bool(
            location.source_file == spec["source_file"]
            and location.export_name == spec["export_name"]
            and location.actor_class == "BP_PickupItem_WithFlagChange_C"
            and location.save_id == spec["save_id"]
            and location.rewards[0].article_path == "/Game/Blueprint/Article/Jail/DA_Jail_Stinger"
            and location.rewards[0].count == 1
            and location.rewards[0].notification is True
            and location.rewards[0].soft_object_path_identity == SoftObjectPathIdentity.from_literal_path(
                "/Game/Blueprint/Article/Jail/DA_Jail_Stinger",
            )
            and location.mirror_of_package_path == spec["mirror_of_package_path"]
            and location.mirror_of_export_index == spec["mirror_of_export_index"]
        )

    article_path = location.rewards[0].article_path
    identity = location.rewards[0].soft_object_path_identity
    source_stem = location.source_file[:-5] if location.source_file.casefold().endswith(".umap") else ""
    return bool(
        article_path.casefold() in {value.casefold() for value in FLAG_PICKUP_ARTICLE_ONLY_ALLOWLIST}
        and location.actor_class in _FLAG_PICKUP_ACTOR_CLASSES
        and location.save_id
        and is_world_map_package(location.package_path)
        and source_stem == location.package_path.rsplit("/", 1)[-1]
        and identity is not None
        and identity.resolved_path == article_path
        and identity.resolved_asset_name == article_path.rsplit("/", 1)[-1]
    )


@dataclass(frozen=True, slots=True)
class WorldLootInstanceOverrideSource:
    """Shared CDO whose ArticleBulkInfo is cloned onto one physical actor."""

    cdo_package: str
    cdo_export_index: int
    cdo_export_name: str
    class_package: str
    parent_blueprint_packages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorldLootCdoSourceReference:
    """Vanilla Article reference for a World Loot CDO we cannot safely patch."""

    blueprint_package: str
    blueprint_class: str
    cdo_export_index: int
    cdo_export_name: str
    physical_instance_count: int
    unsupported_instance_count: int
    reason: str
    reward: RewardPayload


@dataclass(frozen=True, slots=True)
class RawArticleGrantLayout:
    """Version-bound byte layout for one researched direct Article grant."""

    export_name: str
    serial_size: int
    path_name_index_offset: int
    path_number_offset: int
    object_name_index_offset: int
    object_number_offset: int
    expected_path_name_index: int
    expected_object_name_index: int
    expected_path_name_map_value: str
    expected_object_name_map_value: str
    expected_vanilla_article_path: str
    expected_vanilla_number: int
    source_uasset_sha256: str
    source_uexp_sha256: str


@dataclass(frozen=True, slots=True)
class QuestArticleGrantLayout:
    """Expected structured GainItem path and vanilla FName representation."""

    export_name: str
    bulk_index: int
    reward_index: int
    expected_article_path: str
    expected_package_name_value: str
    expected_package_name_number: int
    expected_asset_name_value: str
    expected_asset_name_number: int
    count: int
    notification: bool
    expected_sub_path_string: str = ""
    expected_articles_count: int | None = None
    preserved_export_index: int | None = None
    preserved_export_name: str | None = None
    mirrored_exports: tuple["QuestArticleGrantMirror", ...] = ()
    require_strict_export_snapshots: bool = False
    source_uasset_sha256: str | None = None
    source_uexp_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class QuestArticleGrantMirror:
    """An alternative serialized export that mirrors one logical Article slot."""

    export_index: int
    export_name: str


_RAW_ARTICLE_GRANT_SPECS = {
    "jadwiga_servant_lightning_barrage_reward": {
        "package_path": "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Common/Test/JadwigaServant/BP_SmallScenario_JadwigaServant1",
        "source_file": "Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Common/Test/JadwigaServant/BP_SmallScenario_JadwigaServant1.uasset",
        "export_name": "Default__BP_SmallScenario_JadwigaServant1_C",
        "actor_class": "BP_SmallScenario_JadwigaServant1_C",
        "display_name": "Jadwiga Servant - Lightning Barrage Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicLongRange/DA_GM_CV02_LightningBarrage",
        "path_name_map_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicLongRange/DA_GM_CV02_LightningBarrage",
        "object_name_map_value": "DA_GM_CV02_LightningBarrage",
        "source_uasset_sha256": "97ad9e4403de3933993175e42381a8dfea5a6cc646df865105227ee5d3392f3d",
        "source_uexp_sha256": "999507cbe374382cbcf7e585e565bb53416d55822c677634a0484afe9d2076a1",
        "vanilla_number": 0,
    },
    "jadwiga_servant_booster_dex_reward": {
        "package_path": "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Common/Test/JadwigaServant/BP_SmallScenario_JadwigaServant2",
        "source_file": "Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Common/Test/JadwigaServant/BP_SmallScenario_JadwigaServant2.uasset",
        "export_name": "Default__BP_SmallScenario_JadwigaServant2_C",
        "actor_class": "BP_SmallScenario_JadwigaServant2_C",
        "display_name": "Jadwiga Servant - Booster Dex Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/Basic/DA_Booster_Dex_1",
        "path_name_map_value": "/Game/Blueprint/Article/Booster/Basic/DA_Booster_Dex",
        "object_name_map_value": "DA_Booster_Dex",
        "source_uasset_sha256": "88afd1283169d1e3620a169e191112fc803e13aca499602cc023158efde32fcd",
        "source_uexp_sha256": "124c3baaf309e6b6568eeef7ef4050ead4a5ab2f681436674c9f36437877517d",
        "vanilla_number": 2,
    },
    "jadwiga_servant_initial_parry_reward": {
        "package_path": "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Common/Test/JadwigaServant/BP_SmallScenario_JadwigaServant3",
        "source_file": "Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Common/Test/JadwigaServant/BP_SmallScenario_JadwigaServant3.uasset",
        "export_name": "Default__BP_SmallScenario_JadwigaServant3_C",
        "actor_class": "BP_SmallScenario_JadwigaServant3_C",
        "display_name": "Jadwiga Servant - Initial Parry Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Parry_1",
        "path_name_map_value": "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_Parry",
        "object_name_map_value": "DA_DefGadget_Parry",
        "source_uasset_sha256": "fc6fc87534a026926a3281750ccce92d7f3cd36c48893946c68d271f2e1d1c9b",
        "source_uexp_sha256": "1a34124c4b48274b13178a656958717ae8734cf70955900c0c23872c7c45a43b",
        "vanilla_number": 2,
    },
}


_QUEST_ARTICLE_GRANT_SPECS = {
    "bequeathed_lou_axe_magmell_quest": {
        "package_path": "/Game/Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10365",
        "source_file": "Quest/MainEpisode_Beginning/QST_MainEpisode_Beginning_e10365.uasset",
        "export_index": 28,
        "export_name": "QSTask_GainItem_1",
        "actor_class": "QSTask_GainItem_1",
        "display_name": "MagMell Beginning Quest - Battle Axe: Idris's Conceit",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/StandAlone/DA_LouAxe",
        "package_name_value": "/Game/Blueprint/Article/Gadget/StandAlone/DA_LouAxe",
        "package_name_number": 0,
        "asset_name_value": "DA_LouAxe",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
        "require_strict_export_snapshots": True,
    },
    "bequeathed_executioner_lyle_quest": {
        "package_path": "/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00382",
        "source_file": "Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00382.uasset",
        "export_index": 6,
        "export_name": "QSTask_GainItem_1",
        "actor_class": "QSTask_GainItem_1",
        "display_name": "Lyle Quest - Absolute Executioner",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/StandAlone/DA_ExecutionSword",
        "package_name_value": "/Game/Blueprint/Article/Gadget/StandAlone/DA_ExecutionSword",
        "package_name_number": 0,
        "asset_name_value": "DA_ExecutionSword",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
        "require_strict_export_snapshots": True,
    },
    "dlc_e90360_gift_heal_point_up_reward": {
        "package_path": "/Game/Quest/DLC/QST_MainEpisode_DLC_e90360",
        "source_file": "Quest/DLC/QST_MainEpisode_DLC_e90360.uasset",
        "export_index": 83,
        "export_name": "QSTask_GainItem_1",
        "actor_class": "QSTask_GainItem_1",
        "display_name": "DLC e90360 - Gift Heal Point Up Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/DLC_Other/DA_Booster_GiftHealPointUp_1",
        "package_name_value": "/Game/Blueprint/Article/Booster/DLC_Other/DA_Booster_GiftHealPointUp",
        "package_name_number": 2,
        "asset_name_value": "DA_Booster_GiftHealPointUp",
        "asset_name_number": 2,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
    },
    "one_time_enemy_dlc_e90153_female_knight": {
        "package_path": "/Game/Quest/DLC/QST_MainEpisode_DLC_e90153",
        "source_file": "Quest/DLC/QST_MainEpisode_DLC_e90153.uasset",
        "export_index": 25,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - DLC e90153: ABD Female Knight",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/DLC/WeaponAddOn/Support/DA_GB_UNQ09_IchorRevoveryOnJustGuard",
        "package_name_value": "/Game/Blueprint/Article/Gadget/DLC/WeaponAddOn/Support/DA_GB_UNQ09_IchorRevoveryOnJustGuard",
        "package_name_number": 0,
        "asset_name_value": "DA_GB_UNQ09_IchorRevoveryOnJustGuard",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_dlc_e90193_zenon_servant": {
        "package_path": "/Game/Quest/DLC/QST_MainEpisode_DLC_e90193",
        "source_file": "Quest/DLC/QST_MainEpisode_DLC_e90193.uasset",
        "export_index": 25,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - DLC e90193: Zenon Servant",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/DLC/WeaponAddOn/DA_SpinFireSlam",
        "package_name_value": "/Game/Blueprint/Article/Gadget/DLC/WeaponAddOn/DA_SpinFireSlam",
        "package_name_number": 0,
        "asset_name_value": "DA_SpinFireSlam",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "harmony_moon_holly_reward": {
        "package_path": "/Game/Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Holly",
        "source_file": "Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Holly.uasset",
        "export_index": 39,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "Harmony Moon - Holly Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Holly_1",
        "package_name_value": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Holly",
        "package_name_number": 2,
        "asset_name_value": "DA_Booster_HarmonyMoon_Holly",
        "asset_name_number": 2,
        "bulk_index": 0,
        "reward_index": 1,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 2,
        "preserved_export_index": 40,
        "preserved_export_name": "QSTask_GainMultiplayReward_0",
    },
    "harmony_moon_josee_reward": {
        "package_path": "/Game/Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Josee",
        "source_file": "Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Josee.uasset",
        "export_index": 39,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "Harmony Moon - Josee Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Josee_1",
        "package_name_value": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Josee",
        "package_name_number": 2,
        "asset_name_value": "DA_Booster_HarmonyMoon_Josee",
        "asset_name_number": 2,
        "bulk_index": 0,
        "reward_index": 1,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 2,
        "preserved_export_index": 40,
        "preserved_export_name": "QSTask_GainMultiplayReward_0",
    },
    "harmony_moon_lyle_reward": {
        "package_path": "/Game/Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Lyle",
        "source_file": "Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Lyle.uasset",
        "export_index": 39,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "Harmony Moon - Lyle Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Lyle_1",
        "package_name_value": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Lyle",
        "package_name_number": 2,
        "asset_name_value": "DA_Booster_HarmonyMoon_Lyle",
        "asset_name_number": 2,
        "bulk_index": 0,
        "reward_index": 1,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 2,
        "preserved_export_index": 40,
        "preserved_export_name": "QSTask_GainMultiplayReward_0",
    },
    "harmony_moon_noah_reward": {
        "package_path": "/Game/Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Noah_02",
        "source_file": "Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Noah_02.uasset",
        "export_index": 39,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "Harmony Moon - Noah Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Noah_1",
        "package_name_value": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Noah",
        "package_name_number": 2,
        "asset_name_value": "DA_Booster_HarmonyMoon_Noah",
        "asset_name_number": 2,
        "bulk_index": 0,
        "reward_index": 1,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 2,
        "preserved_export_index": 40,
        "preserved_export_name": "QSTask_GainMultiplayReward_0",
    },
    "harmony_moon_zenon_reward": {
        "package_path": "/Game/Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Zenon",
        "source_file": "Quest/LastEpisode_HarmonyMoon/QST_LastEpisode_HarmonyMoon_Memory_Zenon.uasset",
        "export_index": 39,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "Harmony Moon - Zenon Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Zenon_1",
        "package_name_value": "/Game/Blueprint/Article/Booster/Other/DA_Booster_HarmonyMoon_Zenon",
        "package_name_number": 2,
        "asset_name_value": "DA_Booster_HarmonyMoon_Zenon",
        "asset_name_number": 2,
        "bulk_index": 0,
        "reward_index": 1,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 2,
        "preserved_export_index": 40,
        "preserved_export_name": "QSTask_GainMultiplayReward_0",
    },
    "iris_character_episode_initial_reward": {
        "package_path": "/Game/Quest/CharacterEpisode/CharacterEpisode_Iris/QST_CharacterEpisode_Iris_c00010",
        "source_file": "Quest/CharacterEpisode/CharacterEpisode_Iris/QST_CharacterEpisode_Iris_c00010.uasset",
        "export_index": 16,
        "export_name": "QSTask_GainItem_1",
        "actor_class": "QSTask_GainItem_1",
        "display_name": "Iris Character Episode - Initial Reward",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIrisSample_1",
        "package_name_value": "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIrisSample",
        "package_name_number": 2,
        "asset_name_value": "DA_Booster_CharaEpisodeIrisSample",
        "asset_name_number": 2,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
        "mirrored_exports": [],
        "require_strict_export_snapshots": True,
    },
    "iris_reward_1": {
        "package_path": "/Game/Quest/CharacterEpisode/CharacterEpisode_Iris/QST_CharacterEpisode_Iris_c00070",
        "source_file": "Quest/CharacterEpisode/CharacterEpisode_Iris/QST_CharacterEpisode_Iris_c00070.uasset",
        "export_index": 39,
        "export_name": "QSTask_GainItem_2",
        "actor_class": "QSTask_GainItem_2",
        "display_name": "Iris Character Episode - Reward 1",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIris_1",
        "package_name_value": "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIris",
        "package_name_number": 2,
        "asset_name_value": "DA_Booster_CharaEpisodeIris",
        "asset_name_number": 2,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 2,
        "mirrored_exports": [{"export_index": 40, "export_name": "QSTask_GainItem_3"}],
        "require_strict_export_snapshots": True,
    },
    "iris_reward_2": {
        "package_path": "/Game/Quest/CharacterEpisode/CharacterEpisode_Iris/QST_CharacterEpisode_Iris_c00070",
        "source_file": "Quest/CharacterEpisode/CharacterEpisode_Iris/QST_CharacterEpisode_Iris_c00070.uasset",
        "export_index": 40,
        "export_name": "QSTask_GainItem_3",
        "actor_class": "QSTask_GainItem_3",
        "display_name": "Iris Character Episode - Reward 2",
        "vanilla_article_path": "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIris_2",
        "package_name_value": "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIris",
        "package_name_number": 3,
        "asset_name_value": "DA_Booster_CharaEpisodeIris",
        "asset_name_number": 3,
        "bulk_index": 0,
        "reward_index": 1,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 2,
        "mirrored_exports": [{"export_index": 39, "export_name": "QSTask_GainItem_2"}],
        "require_strict_export_snapshots": True,
    },
    "one_time_enemy_foc_mountainsidecave": {
        "package_path": "/Game/Quest/Dungeon/FOC/QST_QuestDungeon_FoC_MountainsideCave_Boss",
        "source_file": "Quest/Dungeon/FOC/QST_QuestDungeon_FoC_MountainsideCave_Boss.uasset",
        "export_index": 28,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - FoC: Mountainside Cave",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/WeaponAction/DA_ChargeCrash_V2",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/WeaponAction/DA_ChargeCrash_V2",
        "package_name_number": 0,
        "asset_name_value": "DA_ChargeCrash_V2",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_foc_outskirtsobservationpost": {
        "package_path": "/Game/Quest/Dungeon/FOC/QST_QuestDungeon_FoC_OutskirtsObservationPost_Boss",
        "source_file": "Quest/Dungeon/FOC/QST_QuestDungeon_FoC_OutskirtsObservationPost_Boss.uasset",
        "export_index": 28,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - FoC: Outskirts Observation Post",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicLongRange/DA_GM_New29_BuddyBoomerang",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicLongRange/DA_GM_New29_BuddyBoomerang",
        "package_name_number": 0,
        "asset_name_value": "DA_GM_New29_BuddyBoomerang",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_foc_patientsvillagefacility": {
        "package_path": "/Game/Quest/Dungeon/FOC/QST_QuestDungeon_FoC_PatientsVillageFacility_Boss",
        "source_file": "Quest/Dungeon/FOC/QST_QuestDungeon_FoC_PatientsVillageFacility_Boss.uasset",
        "export_index": 28,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - FoC: Patients Village Facility",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportAttack/DA_GB_CV17_HPtoAtkUp",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportAttack/DA_GB_CV17_HPtoAtkUp",
        "package_name_number": 0,
        "asset_name_value": "DA_GB_CV17_HPtoAtkUp",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_foc_riversidechurch": {
        "package_path": "/Game/Quest/Dungeon/FOC/QST_QuestDungeon_FoC_RiversideChurch_Boss",
        "source_file": "Quest/Dungeon/FOC/QST_QuestDungeon_FoC_RiversideChurch_Boss.uasset",
        "export_index": 36,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - FoC: Riverside Church",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportDefense/DA_GB_New24_PreHeal",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportDefense/DA_GB_New24_PreHeal",
        "package_name_number": 0,
        "asset_name_value": "DA_GB_New24_PreHeal",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_mm_medium08": {
        "package_path": "/Game/Quest/Dungeon/MM/QST_QuestDungeon_MM_Medium08_Boss",
        "source_file": "Quest/Dungeon/MM/QST_QuestDungeon_MM_Medium08_Boss.uasset",
        "export_index": 28,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - MagMell: Alexis the Heartless Bodyguard (Adrenaline Formae)",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportAttack/DA_GB_CV19_AtkUpFixed",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportAttack/DA_GB_CV19_AtkUpFixed",
        "package_name_number": 0,
        "asset_name_value": "DA_GB_CV19_AtkUpFixed",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_mm_pbmedium01": {
        "package_path": "/Game/Quest/Dungeon/MM/QST_QuestDungeon_MM_PBMedium01_Boss",
        "source_file": "Quest/Dungeon/MM/QST_QuestDungeon_MM_PBMedium01_Boss.uasset",
        "export_index": 36,
        "export_name": "QSTask_GainItem_1",
        "actor_class": "QSTask_GainItem_1",
        "display_name": "One-Time Enemy Reward - MagMell: PB Medium 01",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportDefense/DA_GB_CV46_BloodDefUp",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportDefense/DA_GB_CV46_BloodDefUp",
        "package_name_number": 0,
        "asset_name_value": "DA_GB_CV46_BloodDefUp",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_sc_arcadebuilding": {
        "package_path": "/Game/Quest/Dungeon/MM/QST_QuestDungeon_SC_ArcadeBuilding_Boss",
        "source_file": "Quest/Dungeon/MM/QST_QuestDungeon_SC_ArcadeBuilding_Boss.uasset",
        "export_index": 36,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - MagMell: Arcade Building",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicLongRange/DA_GM_New15_ChainLightning",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicLongRange/DA_GM_New15_ChainLightning",
        "package_name_number": 0,
        "asset_name_value": "DA_GM_New15_ChainLightning",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_sc_buildinghill": {
        "package_path": "/Game/Quest/Dungeon/SC/QST_QuestDungeon_SC_BuildingHill_Boss",
        "source_file": "Quest/Dungeon/SC/QST_QuestDungeon_SC_BuildingHill_Boss.uasset",
        "export_index": 28,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - SC: Building Hill",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportDefense/DA_GB_CV71_AtkToDef",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportDefense/DA_GB_CV71_AtkToDef",
        "package_name_number": 0,
        "asset_name_value": "DA_GB_CV71_AtkToDef",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_sc_mountainsidefacility": {
        "package_path": "/Game/Quest/Dungeon/SC/QST_QuestDungeon_SC_MountainsideFacility_Boss",
        "source_file": "Quest/Dungeon/SC/QST_QuestDungeon_SC_MountainsideFacility_Boss.uasset",
        "export_index": 28,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - SC: Mountainside Facility",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV27_BPRecoveryUp",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV27_BPRecoveryUp",
        "package_name_number": 0,
        "asset_name_value": "DA_GB_CV27_BPRecoveryUp",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_voa_northobservationpost": {
        "package_path": "/Game/Quest/Dungeon/VoA/QST_QuestDungeon_VoA_NorthObservationPost",
        "source_file": "Quest/Dungeon/VoA/QST_QuestDungeon_VoA_NorthObservationPost.uasset",
        "export_index": 36,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - VoA: North Observation Post",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportAttack/DA_GB_CV26_DefPiercingOnce",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportAttack/DA_GB_CV26_DefPiercingOnce",
        "package_name_number": 0,
        "asset_name_value": "DA_GB_CV26_DefPiercingOnce",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_voa_riverportobservationpost": {
        "package_path": "/Game/Quest/Dungeon/VoA/QST_QuestDungeon_VoA_RiverPortObservationPost",
        "source_file": "Quest/Dungeon/VoA/QST_QuestDungeon_VoA_RiverPortObservationPost.uasset",
        "export_index": 28,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - VoA: River Port Observation Post",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicShortMiddleRange/DA_GM_CV19_BloodBlade",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicShortMiddleRange/DA_GM_CV19_BloodBlade",
        "package_name_number": 0,
        "asset_name_value": "DA_GM_CV19_BloodBlade",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_voa_undergroundfacility": {
        "package_path": "/Game/Quest/Dungeon/VoA/QST_QuestDungeon_VoA_UndergroundFacility",
        "source_file": "Quest/Dungeon/VoA/QST_QuestDungeon_VoA_UndergroundFacility.uasset",
        "export_index": 36,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - VoA: Underground Facility",
        "vanilla_article_path": "/Game/Blueprint/Article/Weapon/RuneBlade/DA_RuneBlade_BurningSword_1",
        "package_name_value": "/Game/Blueprint/Article/Weapon/RuneBlade/DA_RuneBlade_BurningSword",
        "package_name_number": 2,
        "asset_name_value": "DA_RuneBlade_BurningSword",
        "asset_name_number": 2,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
    "one_time_enemy_voa_undergroundshelter": {
        "package_path": "/Game/Quest/Dungeon/VoA/QST_QuestDungeon_VoA_UndergroundShelter",
        "source_file": "Quest/Dungeon/VoA/QST_QuestDungeon_VoA_UndergroundShelter.uasset",
        "export_index": 36,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": "One-Time Enemy Reward - VoA: Underground Shelter",
        "vanilla_article_path": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV36_JustDodgeStaminaRecovery",
        "package_name_value": "/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV36_JustDodgeStaminaRecovery",
        "package_name_number": 0,
        "asset_name_value": "DA_GB_CV36_JustDodgeStaminaRecovery",
        "asset_name_number": 0,
        "bulk_index": 0,
        "reward_index": 0,
        "count": 1,
        "notification": True,
        "sub_path_string": "",
        "articles_count": 1,
    },
}

# Hot Springs award bundled `Articles` from one QSTask_GainItem task. Keep the
# physical destinations and each serialized reward index explicit so this
# opt-in support never depends on display names or a runtime audit file.
_HOT_SPRING_QUEST_DESTINATIONS = (
    ("hot_spring_city", "/Game/Quest/Common/HotSpring/HotSpringCity", "Hot Spring - City", 10,
     ("ResUpStun", "ResUpSun")),
    ("hot_spring_city02", "/Game/Quest/Common/HotSpring/HotSpringCity02", "Hot Spring - City 02", 10,
     ("ResUpCapture", "ResUpEclipse")),
    ("hot_spring_forest01", "/Game/Quest/Common/HotSpring/HotSpringForest01", "Hot Spring - Forest 01", 10,
     ("ResUpDeadlyVenom", "ResUpSlow")),
    ("hot_spring_forest02", "/Game/Quest/Common/HotSpring/HotSpringForest02", "Hot Spring - Forest 02", 10,
     ("ResUpDeadlyVenom", "ResUpSlow")),
    ("hot_spring_forest03", "/Game/Quest/Common/HotSpring/HotSpringForest03", "Hot Spring - Forest 03", 10,
     ("ResUpDeadlyVenom", "ResUpSlow")),
    ("hot_spring_forest04", "/Game/Quest/Common/HotSpring/HotSpringForest04", "Hot Spring - Forest 04", 10,
     ("ResUpDeadlyVenom", "ResUpSlow")),
    ("hot_spring_forest05", "/Game/Quest/Common/HotSpring/HotSpringForest05", "Hot Spring - Forest 05", 10,
     ("ResUpFatalLeak", "ResUpVanish")),
    ("hot_spring_magmel", "/Game/Quest/Common/HotSpring/HotSpringMagmel", "Hot Spring - MagMell", 10,
     ("ResUpStone", "ResUpStall")),
    ("hot_spring_valley", "/Game/Quest/Common/HotSpring/HotSpringValley", "Hot Spring - Valley", 10,
     ("ResUpAcid", "ResUpDissonance")),
    ("hot_spring_dlc_evileyeforest", "/Game/Quest/DLC/Common/HotSpring/HotSpringEvileyeforest", "Hot Spring - DLC Evil Eye Forest", 9,
     ("DLC_ReturnBuddy",)),
)

for _destination_id, _package_path, _display_name, _export_index, _recipe_names in _HOT_SPRING_QUEST_DESTINATIONS:
    _reward_layouts = []
    for _reward_index, _recipe_name in enumerate(_recipe_names):
        _asset_base = f"DA_Item_Important_Recipe_{_recipe_name}"
        _article_path = f"/Game/Blueprint/Article/Item/Important/{_asset_base}_1"
        _reward_layouts.append({
            "reward_index": _reward_index,
            "vanilla_article_path": _article_path,
            "package_name_value": f"/Game/Blueprint/Article/Item/Important/{_asset_base}",
            "package_name_number": 2,
            "asset_name_value": _asset_base,
            "asset_name_number": 2,
            "bulk_index": 0,
            "count": 1,
            "notification": True,
            "sub_path_string": "",
            "articles_count": len(_recipe_names),
        })
    _QUEST_ARTICLE_GRANT_SPECS[_destination_id] = {
        "package_path": _package_path,
        "source_file": _package_path.removeprefix("/Game/") + ".uasset",
        "export_index": _export_index,
        "export_name": "QSTask_GainItem_0",
        "actor_class": "QSTask_GainItem_0",
        "display_name": _display_name,
        "reward_layouts": _reward_layouts,
    }
del _destination_id, _package_path, _display_name, _export_index, _recipe_names
del _reward_layouts, _reward_index, _recipe_name, _asset_base, _article_path

_QUEST_ARTICLE_GRANT_SPECS.update(
{'quest_reward_qst_characterepisode_holly_c05020_48': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Holly/QST_CharacterEpisode_Holly_c05020',
                                                       'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Holly/QST_CharacterEpisode_Holly_c05020.uasset',
                                                       'export_index': 48,
                                                       'export_name': 'QSTask_GainItem_1',
                                                       'actor_class': 'QSTask_GainItem_1',
                                                       'display_name': 'Quest Holly, reward #48',
                                                       'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV02_MndWilAttributeUp',
                                                       'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV02_MndWilAttributeUp',
                                                       'package_name_number': 0,
                                                       'asset_name_value': 'DA_GB_CV02_MndWilAttributeUp',
                                                       'asset_name_number': 0,
                                                       'bulk_index': 0,
                                                       'reward_index': 0,
                                                       'count': 1,
                                                       'notification': True,
                                                       'sub_path_string': '',
                                                       'articles_count': 1},
 'quest_reward_qst_characterepisode_holly_c05060_44': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Holly/QST_CharacterEpisode_Holly_c05060',
                                                       'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Holly/QST_CharacterEpisode_Holly_c05060.uasset',
                                                       'export_index': 44,
                                                       'export_name': 'QSTask_GainItem_0',
                                                       'actor_class': 'QSTask_GainItem_0',
                                                       'display_name': 'Quest Holly, reward #44',
                                                       'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV60_HPToIchor',
                                                       'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV60_HPToIchor',
                                                       'package_name_number': 0,
                                                       'asset_name_value': 'DA_GB_CV60_HPToIchor',
                                                       'asset_name_number': 0,
                                                       'bulk_index': 0,
                                                       'reward_index': 0,
                                                       'count': 1,
                                                       'notification': True,
                                                       'sub_path_string': '',
                                                       'articles_count': 1},
 'quest_reward_qst_characterepisode_holly_c05090_45': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Holly/QST_CharacterEpisode_Holly_c05090',
                                                       'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Holly/QST_CharacterEpisode_Holly_c05090.uasset',
                                                       'export_index': 45,
                                                       'export_name': 'QSTask_GainItem_0',
                                                       'actor_class': 'QSTask_GainItem_0',
                                                       'display_name': 'Quest Holly, reward #45',
                                                       'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV63_BuffDurationUp',
                                                       'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV63_BuffDurationUp',
                                                       'package_name_number': 0,
                                                       'asset_name_value': 'DA_GB_CV63_BuffDurationUp',
                                                       'asset_name_number': 0,
                                                       'bulk_index': 0,
                                                       'reward_index': 0,
                                                       'count': 1,
                                                       'notification': True,
                                                       'sub_path_string': '',
                                                       'articles_count': 1},
 'quest_reward_qst_characterepisode_holly_c05120_52': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Holly/QST_CharacterEpisode_Holly_c05120',
                                                       'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Holly/QST_CharacterEpisode_Holly_c05120.uasset',
                                                       'export_index': 52,
                                                       'export_name': 'QSTask_GainItem_1',
                                                       'actor_class': 'QSTask_GainItem_1',
                                                       'display_name': 'Quest Holly, reward #52',
                                                       'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicShortMiddleRange/DA_GM_Emy06_HollyIcePillar',
                                                       'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicShortMiddleRange/DA_GM_Emy06_HollyIcePillar',
                                                       'package_name_number': 0,
                                                       'asset_name_value': 'DA_GM_Emy06_HollyIcePillar',
                                                       'asset_name_number': 0,
                                                       'bulk_index': 0,
                                                       'reward_index': 0,
                                                       'count': 1,
                                                       'notification': True,
                                                       'sub_path_string': '',
                                                       'articles_count': 1},
 'quest_reward_qst_characterepisode_josee_c06060_46': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Josee/QST_CharacterEpisode_Josee_c06060',
                                                       'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Josee/QST_CharacterEpisode_Josee_c06060.uasset',
                                                       'export_index': 46,
                                                       'export_name': 'QSTask_GainItem_1',
                                                       'actor_class': 'QSTask_GainItem_1',
                                                       'display_name': 'Quest Josee, reward #46',
                                                       'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV32_StaminaOveruse',
                                                       'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV32_StaminaOveruse',
                                                       'package_name_number': 0,
                                                       'asset_name_value': 'DA_GB_CV32_StaminaOveruse',
                                                       'asset_name_number': 0,
                                                       'bulk_index': 0,
                                                       'reward_index': 1,
                                                       'count': 1,
                                                       'notification': True,
                                                       'sub_path_string': '',
                                                       'articles_count': 2},
 'quest_reward_qst_characterepisode_lise_c02030_47': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Lise/QST_CharacterEpisode_Lise_c02030',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Lise/QST_CharacterEpisode_Lise_c02030.uasset',
                                                      'export_index': 47,
                                                      'export_name': 'QSTask_GainItem_0',
                                                      'actor_class': 'QSTask_GainItem_0',
                                                      'display_name': 'Quest Lise, reward #47',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_New19_StatusDurationUp',
                                                      'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_New19_StatusDurationUp',
                                                      'package_name_number': 0,
                                                      'asset_name_value': 'DA_GB_New19_StatusDurationUp',
                                                      'asset_name_number': 0,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_lyle_c04140_12': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Lyle/QST_CharacterEpisode_Lyle_c04140',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Lyle/QST_CharacterEpisode_Lyle_c04140.uasset',
                                                      'export_index': 12,
                                                      'export_name': 'QSTask_GainItem_0',
                                                      'actor_class': 'QSTask_GainItem_0',
                                                      'display_name': 'Quest Lyle, reward #12',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportDefense/DA_GB_New12_JustBleedDamage',
                                                      'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportDefense/DA_GB_New12_JustBleedDamage',
                                                      'package_name_number': 0,
                                                      'asset_name_value': 'DA_GB_New12_JustBleedDamage',
                                                      'asset_name_number': 0,
                                                      'bulk_index': 0,
                                                      'reward_index': 1,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 2},
 'quest_reward_qst_characterepisode_noah_c01010_15': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01010',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01010.uasset',
                                                      'export_index': 15,
                                                      'export_name': 'QSTask_GainItem_1',
                                                      'actor_class': 'QSTask_GainItem_1',
                                                      'display_name': 'Quest Noah, reward #15',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_SingleSword_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01020_19': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01020',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01020.uasset',
                                                      'export_index': 19,
                                                      'export_name': 'QSTask_GainItem_0',
                                                      'actor_class': 'QSTask_GainItem_0',
                                                      'display_name': 'Quest Noah, reward #19',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/GreatSword/DA_GreatSword_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/GreatSword/DA_GreatSword_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_GreatSword_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01020_20': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01020',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01020.uasset',
                                                      'export_index': 20,
                                                      'export_name': 'QSTask_GainItem_1',
                                                      'actor_class': 'QSTask_GainItem_1',
                                                      'display_name': 'Quest Noah, reward #20',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Hammer/DA_Hammer_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/Hammer/DA_Hammer_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_Hammer_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01020_21': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01020',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01020.uasset',
                                                      'export_index': 21,
                                                      'export_name': 'QSTask_GainItem_2',
                                                      'actor_class': 'QSTask_GainItem_2',
                                                      'display_name': 'Quest Noah, reward #21',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Bayonet/DA_Bayonet_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/Bayonet/DA_Bayonet_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_Bayonet_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01030_24': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030.uasset',
                                                      'export_index': 24,
                                                      'export_name': 'QSTask_GainItem_1',
                                                      'actor_class': 'QSTask_GainItem_1',
                                                      'display_name': 'Quest Noah, reward #24',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/GreatSword/DA_GreatSword_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/GreatSword/DA_GreatSword_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_GreatSword_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01030_25': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030.uasset',
                                                      'export_index': 25,
                                                      'export_name': 'QSTask_GainItem_2',
                                                      'actor_class': 'QSTask_GainItem_2',
                                                      'display_name': 'Quest Noah, reward #25',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Hammer/DA_Hammer_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/Hammer/DA_Hammer_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_Hammer_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01030_26': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030.uasset',
                                                      'export_index': 26,
                                                      'export_name': 'QSTask_GainItem_4',
                                                      'actor_class': 'QSTask_GainItem_4',
                                                      'display_name': 'Quest Noah, reward #26',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Hammer/DA_Hammer_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/Hammer/DA_Hammer_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_Hammer_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01030_27': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030.uasset',
                                                      'export_index': 27,
                                                      'export_name': 'QSTask_GainItem_5',
                                                      'actor_class': 'QSTask_GainItem_5',
                                                      'display_name': 'Quest Noah, reward #27',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Bayonet/DA_Bayonet_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/Bayonet/DA_Bayonet_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_Bayonet_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01030_28': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01030.uasset',
                                                      'export_index': 28,
                                                      'export_name': 'QSTask_GainItem_6',
                                                      'actor_class': 'QSTask_GainItem_6',
                                                      'display_name': 'Quest Noah, reward #28',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Bayonet/DA_Bayonet_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/Bayonet/DA_Bayonet_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_Bayonet_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01040_17': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01040',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01040.uasset',
                                                      'export_index': 17,
                                                      'export_name': 'QSTask_GainItem_1',
                                                      'actor_class': 'QSTask_GainItem_1',
                                                      'display_name': 'Quest Noah, reward #17',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/GreatSword/DA_GreatSword_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/GreatSword/DA_GreatSword_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_GreatSword_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01040_18': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01040',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01040.uasset',
                                                      'export_index': 18,
                                                      'export_name': 'QSTask_GainItem_4',
                                                      'actor_class': 'QSTask_GainItem_4',
                                                      'display_name': 'Quest Noah, reward #18',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Hammer/DA_Hammer_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/Hammer/DA_Hammer_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_Hammer_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01040_19': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01040',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01040.uasset',
                                                      'export_index': 19,
                                                      'export_name': 'QSTask_GainItem_5',
                                                      'actor_class': 'QSTask_GainItem_5',
                                                      'display_name': 'Quest Noah, reward #19',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Bayonet/DA_Bayonet_FranzCustom_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/Bayonet/DA_Bayonet_FranzCustom',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_Bayonet_FranzCustom',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_characterepisode_noah_c01070_30': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01070',
                                                      'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Noah/QST_CharacterEpisode_Noah_c01070.uasset',
                                                      'export_index': 30,
                                                      'export_name': 'QSTask_GainItem_0',
                                                      'actor_class': 'QSTask_GainItem_0',
                                                      'display_name': 'Quest Noah, reward #30',
                                                      'reward_layouts': [{'vanilla_article_path': '/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatEnchantWeaponReduceLoad_1',
                                                                          'package_name_value': '/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatEnchantWeaponReduceLoad',
                                                                          'package_name_number': 2,
                                                                          'asset_name_value': 'DA_Item_MatEnchantWeaponReduceLoad',
                                                                          'asset_name_number': 2,
                                                                          'bulk_index': 0,
                                                                          'reward_index': 0,
                                                                          'count': 1,
                                                                          'notification': True,
                                                                          'sub_path_string': '',
                                                                          'articles_count': 2},
                                                                         {'vanilla_article_path': '/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatEnchantDefGadgetReduceLoad_1',
                                                                          'package_name_value': '/Game/Blueprint/Article/Item/EnhancementMaterial/DA_Item_MatEnchantDefGadgetReduceLoad',
                                                                          'package_name_number': 2,
                                                                          'asset_name_value': 'DA_Item_MatEnchantDefGadgetReduceLoad',
                                                                          'asset_name_number': 2,
                                                                          'bulk_index': 0,
                                                                          'reward_index': 1,
                                                                          'count': 1,
                                                                          'notification': True,
                                                                          'sub_path_string': '',
                                                                          'articles_count': 2}]},
 'quest_reward_qst_characterepisode_valentin_c10040_82': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_c10040',
                                                          'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_c10040.uasset',
                                                          'export_index': 82,
                                                          'export_name': 'QSTask_GainItem_0',
                                                          'actor_class': 'QSTask_GainItem_0',
                                                          'display_name': 'Quest Valentin, reward #82',
                                                          'reward_layouts': [{'vanilla_article_path': '/Game/Blueprint/Article/Item/Recover/DA_Item_HpRecover_1',
                                                                              'package_name_value': '/Game/Blueprint/Article/Item/Recover/DA_Item_HpRecover',
                                                                              'package_name_number': 2,
                                                                              'asset_name_value': 'DA_Item_HpRecover',
                                                                              'asset_name_number': 2,
                                                                              'bulk_index': 0,
                                                                              'reward_index': 0,
                                                                              'count': 1,
                                                                              'notification': True,
                                                                              'sub_path_string': '',
                                                                              'articles_count': 2},
                                                                             {'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV02_StrDexAttributeUp',
                                                                              'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV02_StrDexAttributeUp',
                                                                              'package_name_number': 0,
                                                                              'asset_name_value': 'DA_GB_CV02_StrDexAttributeUp',
                                                                              'asset_name_number': 0,
                                                                              'bulk_index': 0,
                                                                              'reward_index': 1,
                                                                              'count': 1,
                                                                              'notification': True,
                                                                              'sub_path_string': '',
                                                                              'articles_count': 2}]},
 'quest_reward_qst_characterepisode_valentin_c10260_9': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_c10260',
                                                         'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_c10260.uasset',
                                                         'export_index': 9,
                                                         'export_name': 'QSTask_GainItem_0',
                                                         'actor_class': 'QSTask_GainItem_0',
                                                         'display_name': 'Quest Valentin, reward #9',
                                                         'vanilla_article_path': '/Game/Blueprint/Article/Item/Recover/DA_Item_HpRecover_2',
                                                         'package_name_value': '/Game/Blueprint/Article/Item/Recover/DA_Item_HpRecover',
                                                         'package_name_number': 3,
                                                         'asset_name_value': 'DA_Item_HpRecover',
                                                         'asset_name_number': 3,
                                                         'bulk_index': 0,
                                                         'reward_index': 0,
                                                         'count': 1,
                                                         'notification': True,
                                                         'sub_path_string': '',
                                                         'articles_count': 1},
 'quest_reward_qst_characterepisode_valentin_riftfoc_65': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_RiftFoC',
                                                           'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_RiftFoC.uasset',
                                                           'export_index': 65,
                                                           'export_name': 'QSTask_GainItem_0',
                                                           'actor_class': 'QSTask_GainItem_0',
                                                           'display_name': 'Quest Valentin, reward #65',
                                                           'reward_layouts': [{'vanilla_article_path': '/Game/Blueprint/Article/Item/Support/DA_Item_ExpGainUp_1',
                                                                               'package_name_value': '/Game/Blueprint/Article/Item/Support/DA_Item_ExpGainUp',
                                                                               'package_name_number': 2,
                                                                               'asset_name_value': 'DA_Item_ExpGainUp',
                                                                               'asset_name_number': 2,
                                                                               'bulk_index': 0,
                                                                               'reward_index': 0,
                                                                               'count': 1,
                                                                               'notification': True,
                                                                               'sub_path_string': '',
                                                                               'articles_count': 2},
                                                                              {'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicShortMiddleRange/DA_GM_New33_IceGatling',
                                                                               'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicShortMiddleRange/DA_GM_New33_IceGatling',
                                                                               'package_name_number': 0,
                                                                               'asset_name_value': 'DA_GM_New33_IceGatling',
                                                                               'asset_name_number': 0,
                                                                               'bulk_index': 0,
                                                                               'reward_index': 1,
                                                                               'count': 1,
                                                                               'notification': True,
                                                                               'sub_path_string': '',
                                                                               'articles_count': 2}]},
 'quest_reward_qst_characterepisode_valentin_riftsc_69': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_RiftSC',
                                                          'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_RiftSC.uasset',
                                                          'export_index': 69,
                                                          'export_name': 'QSTask_GainItem_0',
                                                          'actor_class': 'QSTask_GainItem_0',
                                                          'display_name': 'Quest Valentin, reward #69',
                                                          'reward_layouts': [{'vanilla_article_path': '/Game/Blueprint/Article/Item/Support/DA_Item_StaminaCostZero_2',
                                                                              'package_name_value': '/Game/Blueprint/Article/Item/Support/DA_Item_StaminaCostZero',
                                                                              'package_name_number': 3,
                                                                              'asset_name_value': 'DA_Item_StaminaCostZero',
                                                                              'asset_name_number': 3,
                                                                              'bulk_index': 0,
                                                                              'reward_index': 0,
                                                                              'count': 1,
                                                                              'notification': True,
                                                                              'sub_path_string': '',
                                                                              'articles_count': 2},
                                                                             {'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicShortMiddleRange/DA_GM_New14_ScorchedEarth',
                                                                              'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/MagicShortMiddleRange/DA_GM_New14_ScorchedEarth',
                                                                              'package_name_number': 0,
                                                                              'asset_name_value': 'DA_GM_New14_ScorchedEarth',
                                                                              'asset_name_number': 0,
                                                                              'bulk_index': 0,
                                                                              'reward_index': 1,
                                                                              'count': 1,
                                                                              'notification': True,
                                                                              'sub_path_string': '',
                                                                              'articles_count': 2}]},
 'quest_reward_qst_characterepisode_valentin_riftvoa_69': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_RiftVoA',
                                                           'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Valentin/QST_CharacterEpisode_Valentin_RiftVoA.uasset',
                                                           'export_index': 69,
                                                           'export_name': 'QSTask_GainItem_2',
                                                           'actor_class': 'QSTask_GainItem_2',
                                                           'display_name': 'Quest Valentin, reward #69',
                                                           'vanilla_article_path': '/Game/Blueprint/Article/Item/Recover/DA_Item_IchorRecover_2',
                                                           'package_name_value': '/Game/Blueprint/Article/Item/Recover/DA_Item_IchorRecover',
                                                           'package_name_number': 3,
                                                           'asset_name_value': 'DA_Item_IchorRecover',
                                                           'asset_name_number': 3,
                                                           'bulk_index': 0,
                                                           'reward_index': 0,
                                                           'count': 1,
                                                           'notification': True,
                                                           'sub_path_string': '',
                                                           'articles_count': 1},
 'quest_reward_qst_characterepisode_zenon_c07010_13': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Zenon/QST_CharacterEpisode_Zenon_c07010',
                                                       'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Zenon/QST_CharacterEpisode_Zenon_c07010.uasset',
                                                       'export_index': 13,
                                                       'export_name': 'QSTask_GainItem_0',
                                                       'actor_class': 'QSTask_GainItem_0',
                                                       'display_name': 'Quest Zenon, reward #13',
                                                       'vanilla_article_path': '/Game/Blueprint/Article/Item/Growth/DA_Item_ExpA_17',
                                                       'package_name_value': '/Game/Blueprint/Article/Item/Growth/DA_Item_ExpA',
                                                       'package_name_number': 18,
                                                       'asset_name_value': 'DA_Item_ExpA',
                                                       'asset_name_number': 18,
                                                       'bulk_index': 0,
                                                       'reward_index': 0,
                                                       'count': 1,
                                                       'notification': True,
                                                       'sub_path_string': '',
                                                       'articles_count': 1},
 'quest_reward_qst_characterepisode_zenon_c07070_28': {'package_path': '/Game/Quest/CharacterEpisode/CharacterEpisode_Zenon/QST_CharacterEpisode_Zenon_c07070',
                                                       'source_file': 'Quest/CharacterEpisode/CharacterEpisode_Zenon/QST_CharacterEpisode_Zenon_c07070.uasset',
                                                       'export_index': 28,
                                                       'export_name': 'QSTask_GainItem_1',
                                                       'actor_class': 'QSTask_GainItem_1',
                                                       'display_name': 'Quest Zenon, reward #28',
                                                       'vanilla_article_path': '/Game/Blueprint/Article/Item/Growth/DA_Item_ExpA_17',
                                                       'package_name_value': '/Game/Blueprint/Article/Item/Growth/DA_Item_ExpA',
                                                       'package_name_number': 18,
                                                       'asset_name_value': 'DA_Item_ExpA',
                                                       'asset_name_number': 18,
                                                       'bulk_index': 0,
                                                       'reward_index': 0,
                                                       'count': 1,
                                                       'notification': True,
                                                       'sub_path_string': '',
                                                       'articles_count': 1},
 'quest_reward_qst_characterepisode_aftered_lou_c31040_10': {'package_path': '/Game/Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31040',
                                                             'source_file': 'Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31040.uasset',
                                                             'export_index': 10,
                                                             'export_name': 'QSTask_GainItem_0',
                                                             'actor_class': 'QSTask_GainItem_0',
                                                             'display_name': 'Quest Lou, reward #10',
                                                             'reward_layouts': [{'vanilla_article_path': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_DualSword_Noah_AED_1',
                                                                                 'package_name_value': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_DualSword_Noah_AED',
                                                                                 'package_name_number': 2,
                                                                                 'asset_name_value': 'DA_DualSword_Noah_AED',
                                                                                 'asset_name_number': 2,
                                                                                 'bulk_index': 0,
                                                                                 'reward_index': 0,
                                                                                 'count': 1,
                                                                                 'notification': True,
                                                                                 'sub_path_string': '',
                                                                                 'articles_count': 2},
                                                                                {'vanilla_article_path': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_RuneBlade_Valentin_AED_1',
                                                                                 'package_name_value': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_RuneBlade_Valentin_AED',
                                                                                 'package_name_number': 2,
                                                                                 'asset_name_value': 'DA_RuneBlade_Valentin_AED',
                                                                                 'asset_name_number': 2,
                                                                                 'bulk_index': 0,
                                                                                 'reward_index': 1,
                                                                                 'count': 1,
                                                                                 'notification': True,
                                                                                 'sub_path_string': '',
                                                                                 'articles_count': 2}]},
 'quest_reward_qst_characterepisode_aftered_lou_c31050_41': {'package_path': '/Game/Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31050',
                                                             'source_file': 'Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31050.uasset',
                                                             'export_index': 41,
                                                             'export_name': 'QSTask_GainItem_1',
                                                             'actor_class': 'QSTask_GainItem_1',
                                                             'display_name': 'Quest Lou, reward #41',
                                                             'vanilla_article_path': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_GreatSword_Josee_AED_1',
                                                             'package_name_value': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_GreatSword_Josee_AED',
                                                             'package_name_number': 2,
                                                             'asset_name_value': 'DA_GreatSword_Josee_AED',
                                                             'asset_name_number': 2,
                                                             'bulk_index': 0,
                                                             'reward_index': 0,
                                                             'count': 1,
                                                             'notification': True,
                                                             'sub_path_string': '',
                                                             'articles_count': 1},
 'quest_reward_qst_characterepisode_aftered_lou_c31070_41': {'package_path': '/Game/Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31070',
                                                             'source_file': 'Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31070.uasset',
                                                             'export_index': 41,
                                                             'export_name': 'QSTask_GainItem_1',
                                                             'actor_class': 'QSTask_GainItem_1',
                                                             'display_name': 'Quest Lou, reward #41',
                                                             'vanilla_article_path': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_SingleSword_Lyle_AED_1',
                                                             'package_name_value': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_SingleSword_Lyle_AED',
                                                             'package_name_number': 2,
                                                             'asset_name_value': 'DA_SingleSword_Lyle_AED',
                                                             'asset_name_number': 2,
                                                             'bulk_index': 0,
                                                             'reward_index': 0,
                                                             'count': 1,
                                                             'notification': True,
                                                             'sub_path_string': '',
                                                             'articles_count': 1},
 'quest_reward_qst_characterepisode_aftered_lou_c31090_40': {'package_path': '/Game/Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31090',
                                                             'source_file': 'Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31090.uasset',
                                                             'export_index': 40,
                                                             'export_name': 'QSTask_GainItem_1',
                                                             'actor_class': 'QSTask_GainItem_1',
                                                             'display_name': 'Quest Lou, reward #40',
                                                             'vanilla_article_path': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_Bayonet_Holly_AED_1',
                                                             'package_name_value': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_Bayonet_Holly_AED',
                                                             'package_name_number': 2,
                                                             'asset_name_value': 'DA_Bayonet_Holly_AED',
                                                             'asset_name_number': 2,
                                                             'bulk_index': 0,
                                                             'reward_index': 0,
                                                             'count': 1,
                                                             'notification': True,
                                                             'sub_path_string': '',
                                                             'articles_count': 1},
 'quest_reward_qst_characterepisode_aftered_lou_c31110_17': {'package_path': '/Game/Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31110',
                                                             'source_file': 'Quest/Epilogue_AfterEnding/CharacterEpisode_AfterEnding/lou/QST_CharacterEpisode_AfterED_Lou_c31110.uasset',
                                                             'export_index': 17,
                                                             'export_name': 'QSTask_GainItem_0',
                                                             'actor_class': 'QSTask_GainItem_0',
                                                             'display_name': 'Quest Lou, reward #17',
                                                             'vanilla_article_path': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_Halberd_Zenon_AED_1',
                                                             'package_name_value': '/Game/Blueprint/Article/Weapon/AfterEnd/DA_Halberd_Zenon_AED',
                                                             'package_name_number': 2,
                                                             'asset_name_value': 'DA_Halberd_Zenon_AED',
                                                             'asset_name_number': 2,
                                                             'bulk_index': 0,
                                                             'reward_index': 0,
                                                             'count': 1,
                                                             'notification': True,
                                                             'sub_path_string': '',
                                                             'articles_count': 1},
 'quest_reward_qst_mainepisode_holly_e30375_37': {'package_path': '/Game/Quest/MainEpisode_Holly/QST_MainEpisode_Holly_e30375',
                                                  'source_file': 'Quest/MainEpisode_Holly/QST_MainEpisode_Holly_e30375.uasset',
                                                  'export_index': 37,
                                                  'export_name': 'QSTask_GainItem_0',
                                                  'actor_class': 'QSTask_GainItem_0',
                                                  'display_name': 'Quest Holly, reward #37',
                                                  'vanilla_article_path': '/Game/Blueprint/Article/Weapon/GreatSword/DA_GreatSword_TrueAncestorServant_1',
                                                  'package_name_value': '/Game/Blueprint/Article/Weapon/GreatSword/DA_GreatSword_TrueAncestorServant',
                                                  'package_name_number': 2,
                                                  'asset_name_value': 'DA_GreatSword_TrueAncestorServant',
                                                  'asset_name_number': 2,
                                                  'bulk_index': 0,
                                                  'reward_index': 0,
                                                  'count': 1,
                                                  'notification': True,
                                                  'sub_path_string': '',
                                                  'articles_count': 1},
 'quest_reward_qst_mainepisode_josee_e20240_43': {'package_path': '/Game/Quest/MainEpisode_Josee/QST_MainEpisode_Josee_e20240',
                                                  'source_file': 'Quest/MainEpisode_Josee/QST_MainEpisode_Josee_e20240.uasset',
                                                  'export_index': 43,
                                                  'export_name': 'QSTask_GainItem_1',
                                                  'actor_class': 'QSTask_GainItem_1',
                                                  'display_name': 'Quest Josee, reward #43',
                                                  'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV35_DodgeStaminaDown',
                                                  'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/SupportOther/DA_GB_CV35_DodgeStaminaDown',
                                                  'package_name_number': 0,
                                                  'asset_name_value': 'DA_GB_CV35_DodgeStaminaDown',
                                                  'asset_name_number': 0,
                                                  'bulk_index': 0,
                                                  'reward_index': 0,
                                                  'count': 1,
                                                  'notification': True,
                                                  'sub_path_string': '',
                                                  'articles_count': 1},
 'quest_reward_qst_mainepisode_josee_e20520_50': {'package_path': '/Game/Quest/MainEpisode_Josee/QST_MainEpisode_Josee_e20520',
                                                  'source_file': 'Quest/MainEpisode_Josee/QST_MainEpisode_Josee_e20520.uasset',
                                                  'export_index': 50,
                                                  'export_name': 'QSTask_GainItem_0',
                                                  'actor_class': 'QSTask_GainItem_0',
                                                  'display_name': 'Quest Josee, reward #50',
                                                  'vanilla_article_path': '/Game/Blueprint/Article/Weapon/RuneBlade/DA_RuneBlade_OnihimeRuneBlade_1',
                                                  'package_name_value': '/Game/Blueprint/Article/Weapon/RuneBlade/DA_RuneBlade_OnihimeRuneBlade',
                                                  'package_name_number': 2,
                                                  'asset_name_value': 'DA_RuneBlade_OnihimeRuneBlade',
                                                  'asset_name_number': 2,
                                                  'bulk_index': 0,
                                                  'reward_index': 0,
                                                  'count': 1,
                                                  'notification': True,
                                                  'sub_path_string': '',
                                                  'articles_count': 1},
 'quest_reward_qst_mainepisode_lyle_e00118_cave_45': {'package_path': '/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00118_Cave',
                                                      'source_file': 'Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00118_Cave.uasset',
                                                      'export_index': 45,
                                                      'export_name': 'QSTask_GainItem_0',
                                                      'actor_class': 'QSTask_GainItem_0',
                                                      'display_name': 'Quest Lyle, reward #45 (Cave)',
                                                      'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Halberd/DA_Halberd_QueenSlayer_1',
                                                      'package_name_value': '/Game/Blueprint/Article/Weapon/Halberd/DA_Halberd_QueenSlayer',
                                                      'package_name_number': 2,
                                                      'asset_name_value': 'DA_Halberd_QueenSlayer',
                                                      'asset_name_number': 2,
                                                      'bulk_index': 0,
                                                      'reward_index': 0,
                                                      'count': 1,
                                                      'notification': True,
                                                      'sub_path_string': '',
                                                      'articles_count': 1},
 'quest_reward_qst_mainepisode_lyle_e00118_valley_45': {'package_path': '/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00118_Valley',
                                                        'source_file': 'Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00118_Valley.uasset',
                                                        'export_index': 45,
                                                        'export_name': 'QSTask_GainItem_0',
                                                        'actor_class': 'QSTask_GainItem_0',
                                                        'display_name': 'Quest Lyle, reward #45 (Valley)',
                                                        'vanilla_article_path': '/Game/Blueprint/Article/Weapon/Halberd/DA_Halberd_QueenSlayer_1',
                                                        'package_name_value': '/Game/Blueprint/Article/Weapon/Halberd/DA_Halberd_QueenSlayer',
                                                        'package_name_number': 2,
                                                        'asset_name_value': 'DA_Halberd_QueenSlayer',
                                                        'asset_name_number': 2,
                                                        'bulk_index': 0,
                                                        'reward_index': 0,
                                                        'count': 1,
                                                        'notification': True,
                                                        'sub_path_string': '',
                                                        'articles_count': 1},
 'quest_reward_qst_mainepisode_lyle_e00290_11': {'package_path': '/Game/Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00290',
                                                 'source_file': 'Quest/MainEpisode_Lyle/QST_MainEpisode_Lyle_e00290.uasset',
                                                 'export_index': 11,
                                                 'export_name': 'QSTask_GainItem_0',
                                                 'actor_class': 'QSTask_GainItem_0',
                                                 'display_name': 'Quest Lyle, reward #11',
                                                 'vanilla_article_path': '/Game/Blueprint/Article/Gadget/WeaponAddOn/WeaponAction/DA_Vampire_LyleSlash',
                                                 'package_name_value': '/Game/Blueprint/Article/Gadget/WeaponAddOn/WeaponAction/DA_Vampire_LyleSlash',
                                                 'package_name_number': 0,
                                                 'asset_name_value': 'DA_Vampire_LyleSlash',
                                                 'asset_name_number': 0,
                                                 'bulk_index': 0,
                                                 'reward_index': 0,
                                                 'count': 1,
                                                 'notification': True,
                                                 'sub_path_string': '',
                                                 'articles_count': 1},
 'quest_reward_qst_subepisode_agedvamp_a40240_12': {'package_path': '/Game/Quest/SubEpisode/SubEpisode_AgedVamp/QST_SubEpisode_AgedVamp_a40240',
                                                    'source_file': 'Quest/SubEpisode/SubEpisode_AgedVamp/QST_SubEpisode_AgedVamp_a40240.uasset',
                                                    'export_index': 12,
                                                    'export_name': 'QSTask_GainItem_0',
                                                    'actor_class': 'QSTask_GainItem_0',
                                                    'display_name': 'Subquest AgedVamp, reward #12',
                                                    'vanilla_article_path': '/Game/Blueprint/Article/Item/Growth/DA_Item_ExpA_01',
                                                    'package_name_value': '/Game/Blueprint/Article/Item/Growth/DA_Item_ExpA_01',
                                                    'package_name_number': 0,
                                                    'asset_name_value': 'DA_Item_ExpA_01',
                                                    'asset_name_number': 0,
                                                    'bulk_index': 0,
                                                    'reward_index': 0,
                                                    'count': 1,
                                                    'notification': True,
                                                    'sub_path_string': '',
                                                    'articles_count': 1},
 'quest_reward_qst_subepisode_blindvamp_a40100_9': {'package_path': '/Game/Quest/SubEpisode/SubEpisode_BlindVamp/QST_SubEpisode_BlindVamp_a40100',
                                                    'source_file': 'Quest/SubEpisode/SubEpisode_BlindVamp/QST_SubEpisode_BlindVamp_a40100.uasset',
                                                    'export_index': 9,
                                                    'export_name': 'QSTask_GainItem_0',
                                                    'actor_class': 'QSTask_GainItem_0',
                                                    'display_name': 'Subquest BlindVamp, reward #9',
                                                    'vanilla_article_path': '/Game/Blueprint/Article/Item/Support/DA_Item_ResUpMoon_1',
                                                    'package_name_value': '/Game/Blueprint/Article/Item/Support/DA_Item_ResUpMoon',
                                                    'package_name_number': 2,
                                                    'asset_name_value': 'DA_Item_ResUpMoon',
                                                    'asset_name_number': 2,
                                                    'bulk_index': 0,
                                                    'reward_index': 0,
                                                    'count': 1,
                                                    'notification': True,
                                                    'sub_path_string': '',
                                                    'articles_count': 2},
 'quest_reward_qst_subepisode_piercevamp_a40050_7': {'package_path': '/Game/Quest/SubEpisode/SubEpisode_PierceVamp/QST_SubEpisode_PierceVamp_a40050',
                                                     'source_file': 'Quest/SubEpisode/SubEpisode_PierceVamp/QST_SubEpisode_PierceVamp_a40050.uasset',
                                                     'export_index': 7,
                                                     'export_name': 'QSTask_GainItem_0',
                                                     'actor_class': 'QSTask_GainItem_0',
                                                     'display_name': 'Subquest PierceVamp, reward #7',
                                                     'vanilla_article_path': '/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_CarmillaThorn_Alt2',
                                                     'package_name_value': '/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_CarmillaThorn_Alt2',
                                                     'package_name_number': 0,
                                                     'asset_name_value': 'DA_SingleSword_CarmillaThorn_Alt2',
                                                     'asset_name_number': 0,
                                                     'bulk_index': 0,
                                                     'reward_index': 0,
                                                     'count': 1,
                                                     'notification': True,
                                                     'sub_path_string': '',
                                                     'articles_count': 1}}
)

# Physical quest-task exports for the same logical Noah/Lyle reward can appear
# in mutually exclusive quest-tree variants. Keep one pool source and route its
# randomized payload to every equivalent task export.
_QUEST_ARTICLE_GRANT_MIRRORS = {
    "quest_reward_qst_characterepisode_noah_c01030_24": "quest_reward_qst_characterepisode_noah_c01020_19",
    "quest_reward_qst_characterepisode_noah_c01040_17": "quest_reward_qst_characterepisode_noah_c01020_19",
    "quest_reward_qst_characterepisode_noah_c01030_25": "quest_reward_qst_characterepisode_noah_c01020_20",
    "quest_reward_qst_characterepisode_noah_c01030_26": "quest_reward_qst_characterepisode_noah_c01020_20",
    "quest_reward_qst_characterepisode_noah_c01040_18": "quest_reward_qst_characterepisode_noah_c01020_20",
    "quest_reward_qst_characterepisode_noah_c01030_27": "quest_reward_qst_characterepisode_noah_c01020_21",
    "quest_reward_qst_characterepisode_noah_c01030_28": "quest_reward_qst_characterepisode_noah_c01020_21",
    "quest_reward_qst_characterepisode_noah_c01040_19": "quest_reward_qst_characterepisode_noah_c01020_21",
    "quest_reward_qst_mainepisode_lyle_e00118_valley_45": "quest_reward_qst_mainepisode_lyle_e00118_cave_45",
}
for _mirror_destination_id, _canonical_destination_id in _QUEST_ARTICLE_GRANT_MIRRORS.items():
    _mirror_spec = _QUEST_ARTICLE_GRANT_SPECS[_mirror_destination_id]
    _canonical_spec = _QUEST_ARTICLE_GRANT_SPECS[_canonical_destination_id]
    if _mirror_spec["vanilla_article_path"] != _canonical_spec["vanilla_article_path"]:
        raise ValueError("Quest reward mirror does not match its canonical Article identity.")
    _mirror_spec["mirror_of_package_path"] = _canonical_spec["package_path"]
    _mirror_spec["mirror_of_export_index"] = _canonical_spec["export_index"]
del _mirror_destination_id, _canonical_destination_id, _mirror_spec, _canonical_spec

SUPPORTED_RAW_ARTICLE_GRANT_DESTINATIONS = frozenset(_RAW_ARTICLE_GRANT_SPECS)
SUPPORTED_QUEST_ARTICLE_GRANT_DESTINATIONS = frozenset(_QUEST_ARTICLE_GRANT_SPECS)
SUPPORTED_DIRECT_ARTICLE_GRANT_DESTINATIONS = (
    SUPPORTED_RAW_ARTICLE_GRANT_DESTINATIONS | SUPPORTED_QUEST_ARTICLE_GRANT_DESTINATIONS
)


@dataclass(frozen=True, slots=True)
class MaterialLocation:
    """One renewable gathering actor and its complete resource/reward identity."""

    package_path: str
    source_file: str
    export_index: int
    export_name: str
    actor_class: str
    resource_class_package: str
    resource_class_name: str
    reward: RewardPayload
    save_id: str
    protected: bool
    protection_reason: str | None
    where_content_class: str = "UNKNOWN"
    resource_content_class: str = "UNKNOWN"
    reward_content_class: str = "UNKNOWN"
    mirror_of_package_path: str | None = None
    mirror_of_export_index: int | None = None

    # Read actor class package
    @property
    def actor_class_package(self) -> str:
        """Package of the Blueprint class that the map export actually uses."""
        if (
            not self.actor_class.endswith("_C")
            or self.actor_class != self.resource_class_name
            or self.resource_class_package.rsplit("/", 1)[-1] + "_C" != self.actor_class
        ):
            return ""
        return self.resource_class_package


@dataclass(frozen=True, slots=True)
class ShopProduct:
    """One destination slot and the source payload that may be shuffled."""

    family: str
    shop_row: str
    product_index: int
    original_product_path: str
    item_object: str
    price: int
    uses_stock: bool
    stock: int
    protected: bool
    protection_reason: str | None
    what_content_class: str = "UNKNOWN"
    article_path_number: int | None = None
    item_object_number: int | None = None

    # Read where content class
    @property
    def where_content_class(self) -> str:
        return "DLC" if self.family.upper() in {"DLC", "TRADE_DLC"} else "BASE"


@dataclass(frozen=True, slots=True)
class RuntimeCatalogs:
    """Loaded catalog data bound to one explicit revision."""

    revision: str
    world_loot: tuple[WorldLootLocation, ...]
    materials: tuple[MaterialLocation, ...]
    materials_available: bool
    shops: tuple[ShopProduct, ...]
    unsafe_packages: frozenset[str]
    pickup_types: tuple[dict[str, Any], ...]
    features: dict[str, bool]
    important_item_exclusions: ImportantItemExclusions | None = None
    important_item_audit: dict[str, int] | None = None
    important_item_classifications: tuple[dict[str, str], ...] = ()
    world_loot_unsupported_cdo_sources: tuple[WorldLootCdoSourceReference, ...] = ()
    renewable_ingredient_count: int = 0
    healing_upgrade_count: int = 0

    # Require features
    def require_features(self, *, world_loot: bool, materials: bool, shops: bool) -> None:
        """Reject generation requests whose selected data is incomplete."""
        requested = {
            "world_loot": world_loot,
            "materials": materials,
            "shops": shops,
        }
        for feature, enabled in requested.items():
            if enabled and not self.features.get(feature, False):
                if feature == "materials":
                    raise CatalogError(
                        "Material randomization is unavailable: the catalog has no eligible "
                        "renewable material instances."
                    )
                raise CatalogError(f"The {feature.replace('_', ' ')} catalog is unavailable.")
        if materials and not self.materials_available:
            raise CatalogError(
                "Material randomization is unavailable: no complete material catalog was supplied."
            )


class CatalogStore:
    """Load structured catalogs and verify their manifest hashes and counts."""

    # Initialize this object
    def __init__(self, catalogs_dir: Path | None = None, *, reward_pools_dir: Path | None = None) -> None:
        self.catalogs_dir = catalogs_dir or application_data_path("catalogs")
        self.reward_pools_dir = reward_pools_dir or self.catalogs_dir.parent / "reward_pools"

    # Load and validate catalog data
    def load(self, revision: str) -> RuntimeCatalogs:
        """Load one revision after checking every generated JSON signature."""
        if not isinstance(revision, str) or not _CATALOG_REVISION.fullmatch(revision):
            raise CatalogError("Catalog revision must be a simple identifier.")
        directory = self.catalogs_dir / revision
        manifest = self._read_json(directory / "manifest.json", "catalog manifest")
        if manifest.get("catalog_revision") != revision:
            raise CatalogError("Catalog manifest revision does not match the requested revision.")
        file_metadata = _object(manifest.get("files"), "manifest files")

        documents: dict[str, dict[str, Any]] = {}
        for filename, (collection_name, _) in _REQUIRED_FILES.items():
            path = directory / filename
            try:
                raw_bytes = path.read_bytes()
            except OSError as error:
                raise CatalogError(f"Could not read catalog file {path.name}: {error}") from error
            metadata = _object(file_metadata.get(filename), f"manifest entry {filename}")
            expected_hash = _string(metadata.get("sha256"), f"{filename} sha256").casefold()
            actual_hash = hashlib.sha256(raw_bytes).hexdigest()
            if actual_hash != expected_hash:
                raise CatalogError(f"Catalog signature mismatch: {filename}.")
            try:
                document: Any = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as error:
                raise CatalogError(f"Catalog file {filename} is not valid UTF-8 JSON.") from error
            document = _object(document, filename)
            if document.get("catalog_revision") != revision:
                raise CatalogError(f"Catalog file revision mismatch: {filename}.")
            entries = document.get(collection_name)
            if not isinstance(entries, list):
                raise CatalogError(f"{filename}.{collection_name} must be an array.")
            expected_count = _integer(metadata.get("count"), f"{filename} count")
            if len(entries) != expected_count:
                raise CatalogError(f"Catalog count mismatch: {filename}.")
            documents[filename] = document

        features_raw = _object(manifest.get("features"), "manifest features")
        features = {
            name: _boolean(features_raw.get(name), f"feature {name}")
            for name in ("world_loot", "materials", "shops")
        }
        important_exclusions: ImportantItemExclusions | None = None
        important_policy_dir = self.reward_pools_dir / revision
        if (important_policy_dir / "important_item_exclusions.json").is_file():
            try:
                important_exclusions = ImportantItemExclusionStore(important_policy_dir).load(revision)
            except (ImportantItemExclusionError, OSError, ValueError) as error:
                raise CatalogError(f"Important Item exclusion validation failed: {error}") from error
        exclusions = documents["exclusions.json"]["packages"]
        unsafe_packages = frozenset(
            _string(value, "excluded package").casefold() for value in exclusions
        )
        world = tuple(
            self._parse_world_location(entry, unsafe_packages, important_exclusions)
            for entry in documents["world_loot.json"]["locations"]
        )
        world_by_slot = {world_loot_slot_key(item.package_path, item.export_index): item for item in world}
        if len(world_by_slot) != len(world):
            raise CatalogError("World Loot catalog contains duplicate physical package/export slots.")
        mirror_targets: set[tuple[str, int]] = set()
        for location in world:
            if location.mirror_of_package_path is None:
                continue
            target_key = world_loot_slot_key(
                location.mirror_of_package_path, location.mirror_of_export_index or 0,
            )
            target = world_by_slot.get(target_key)
            quest_mirror = location.destination_kind == "QUEST_ARTICLE_GRANT"
            location_class = re.sub(r"_\d+$", "", location.actor_class)
            target_class = re.sub(r"_\d+$", "", target.actor_class) if target is not None else ""
            if (
                target is None
                or (not quest_mirror and target_key in mirror_targets)
                or target.mirror_of_package_path is not None
                or (
                    (target.save_id is None or location.save_id is None
                     or target.save_id.casefold() != location.save_id.casefold())
                    if not quest_mirror else
                    (target.save_id is not None or location.save_id is not None
                     or not target.package_path.startswith("/Game/Quest/")
                     or not location.package_path.startswith("/Game/Quest/")
                     or target.destination_kind != "QUEST_ARTICLE_GRANT")
                )
                or target_class.casefold() != location_class.casefold()
                or target.actor_category != location.actor_category
                or target.destination_kind != location.destination_kind
                or target.location_type != location.location_type
                or target.scripted_reward_mode != location.scripted_reward_mode
                or target.protected != location.protected
                or target.protection_reasons != location.protection_reasons
                or target.rewards != location.rewards
            ):
                raise CatalogError(
                    "World Loot mirror must point to a compatible canonical slot with matching "
                    "class, protection, serializer, and reward payload."
                )
            if not quest_mirror:
                mirror_targets.add(target_key)
        unsupported_cdo_sources = tuple(
            self._parse_world_loot_cdo_source_reference(entry)
            for entry in documents["world_loot.json"].get("unsupported_cdo_source_references", [])
        )
        materials_doc = documents["materials.json"]
        materials_available = _boolean(
            materials_doc.get("available"), "materials.available"
        )
        materials = tuple(
            self._parse_material(entry)
            for entry in materials_doc["locations"]
        )
        if features["materials"] != materials_available:
            raise CatalogError("Material availability does not match the manifest feature flags.")
        if materials_available != bool(materials):
            raise CatalogError("Material availability does not match the catalog contents.")
        source_rows = _integer(materials_doc.get("source_rows"), "materials.source_rows")
        excluded_rows = _integer(materials_doc.get("excluded_rows"), "materials.excluded_rows")
        if excluded_rows > source_rows or source_rows - excluded_rows != len(materials):
            raise CatalogError("Material source/exclusion counts do not match the eligible locations.")
        material_save_ids = [material.save_id.casefold() for material in materials]
        if len(material_save_ids) != len(set(material_save_ids)):
            raise CatalogError("Material catalog contains duplicate UniqueSaveID values.")
        shops = tuple(
            self._parse_shop_product(entry, important_exclusions)
            for entry in documents["shops.json"]["products"]
        )
        important_audit, important_classifications = self._audit_important_items(
            world, shops, important_exclusions,
        )
        pickup_types = tuple(
            _object(entry, "pickup type map") for entry in documents["pickup_types.json"]["maps"]
        )
        return RuntimeCatalogs(
            revision=revision,
            world_loot=world,
            materials=materials,
            materials_available=materials_available,
            shops=shops,
            unsafe_packages=unsafe_packages,
            pickup_types=pickup_types,
            features=features,
            important_item_exclusions=important_exclusions,
            important_item_audit=important_audit,
            important_item_classifications=important_classifications,
            world_loot_unsupported_cdo_sources=unsupported_cdo_sources,
            renewable_ingredient_count=sum(item.location_type == "RENEWABLE_INGREDIENT" for item in world),
            healing_upgrade_count=sum(
                item.location_type in {
                    "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
                }
                for item in world
            ),
        )

    # Read JSON
    @staticmethod
    def _read_json(path: Path, label: str) -> dict[str, Any]:
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CatalogError(f"Could not read {label}: {error}") from error
        return _object(value, label)

    # Parse reward
    @staticmethod
    def _parse_reward(value: Any) -> RewardPayload:
        reward = _object(value, "reward")
        article_path = _string(reward.get("article_path"), "article path")
        if not article_path.startswith("/Game/"):
            raise CatalogError("Reward article path must be a game virtual path.")
        content_class = _string(reward.get("content_class", "UNKNOWN"), "reward content class").upper()
        if content_class not in {"BASE", "DLC", "AFTER_ENDING", "UNKNOWN"}:
            raise CatalogError("Reward has an unknown content class.")
        identity_raw = reward.get("soft_object_path_identity")
        identity = None
        if identity_raw is not None:
            identity_obj = _object(identity_raw, "soft object path identity")
            package_raw = _object(identity_obj.get("package_name"), "package FName identity")
            asset_raw = _object(identity_obj.get("asset_name"), "asset FName identity")
            try:
                identity = SoftObjectPathIdentity(
                    FNameIdentity(
                        _string(package_raw.get("value"), "package FName literal"),
                        _integer(package_raw.get("number"), "package FName number"),
                    ),
                    FNameIdentity(
                        _string(asset_raw.get("value"), "asset FName literal"),
                        _integer(asset_raw.get("number"), "asset FName number"),
                    ),
                )
            except ValueError as error:
                raise CatalogError(f"Invalid soft object path FName identity: {error}") from error
            if (
                identity.resolved_path != article_path
                or identity.resolved_asset_name != article_path.rsplit("/", 1)[-1]
            ):
                raise CatalogError("Soft object path FName identity does not resolve to its Article path.")
        return RewardPayload(
            article_path=article_path,
            count=_integer(reward.get("count"), "reward count", minimum=1),
            notification=_boolean(reward.get("notification"), "reward notification"),
            content_class=content_class,
            soft_object_path_identity=identity,
        )

    # Parse world loot CDO source reference
    @staticmethod
    def _parse_world_loot_cdo_source_reference(value: Any) -> WorldLootCdoSourceReference:
        entry = _object(value, "unsupported World Loot CDO source reference")
        package = _string(entry.get("blueprint_package"), "unsupported CDO Blueprint package")
        class_name = _string(entry.get("blueprint_class"), "unsupported CDO Blueprint class")
        export_name = _string(entry.get("cdo_export_name"), "unsupported CDO export name")
        if (
            not package.startswith("/Game/Environment/Item/")
            or not class_name.endswith("_C")
            or export_name != "Default__" + class_name
        ):
            raise CatalogError("Unsupported CDO source reference has an inconsistent Blueprint/CDO identity.")
        export_index = _integer(entry.get("cdo_export_index"), "unsupported CDO export index", minimum=1)
        physical_count = _integer(entry.get("physical_instance_count"), "unsupported CDO physical instance count", minimum=1)
        unsupported_count = _integer(entry.get("unsupported_instance_count"), "unsupported CDO opaque instance count", minimum=1)
        reason = _string(entry.get("reason"), "unsupported CDO reason")
        if unsupported_count != physical_count or not reason.strip():
            raise CatalogError("Unsupported CDO source reference must explain every opaque physical instance.")
        reward = CatalogStore._parse_reward(entry.get("reward"))
        return WorldLootCdoSourceReference(
            package, class_name, export_index, export_name, physical_count,
            unsupported_count, reason, reward,
        )

    # Parse world location
    @staticmethod
    def _parse_world_location(
        value: Any,
        unsafe_packages: frozenset[str],
        important_exclusions: ImportantItemExclusions | None = None,
    ) -> WorldLootLocation:
        entry = _object(value, "world-loot location")
        package_path = _string(entry.get("package_path"), "world package path")
        source_file = _string(entry.get("source_file"), "world source file")
        export_index = _integer(entry.get("export_index"), "world export index")
        export_name = _string(entry.get("export_name"), "world export name")
        actor_class = _string(entry.get("actor_class"), "world actor class")
        actor_category = _string(entry.get("actor_category"), "world actor category")
        content_class = _string(entry.get("content_class", "UNKNOWN"), "world content class").upper()
        if content_class not in {"BASE", "DLC", "AFTER_ENDING", "UNKNOWN"}:
            raise CatalogError("World location has an unknown content class.")
        protected = _boolean(entry.get("protected"), "world protected")
        reasons = _string_array(entry.get("protection_reasons"), "world protection reasons")
        rewards_raw = entry.get("rewards")
        if not isinstance(rewards_raw, list) or not rewards_raw:
            raise CatalogError("Every world-loot location must have at least one reward.")
        rewards = tuple(CatalogStore._parse_reward(reward) for reward in rewards_raw)
        save_id = entry.get("save_id")
        if save_id is not None:
            save_id = _string(save_id, "world save ID")
        destination_kind = _string(entry.get("destination_kind", "ARTICLE_BULK"), "world destination kind")
        scripted_reward_mode_raw = entry.get("scripted_reward_mode")
        scripted_reward_mode = (
            _string(scripted_reward_mode_raw, "scripted reward mode")
            if scripted_reward_mode_raw is not None else None
        )
        if destination_kind not in {
            "RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT", "BLUEPRINT_CDO_ARTICLE_BULK",
        }:
            world_map_package_for_source(source_file, package_path)
        destination_id: str | None = None
        display_name: str | None = None
        location_type = _string(entry.get("location_type", "NORMAL_WORLD_LOOT"), "world location type")
        structural_class_package = entry.get("structural_class_package")
        if structural_class_package is not None:
            structural_class_package = _string(structural_class_package, "structural class package")
            if not structural_class_package.startswith("/Game/"):
                raise CatalogError("Structural class package must use a /Game path.")
        instance_override_source: WorldLootInstanceOverrideSource | None = None
        raw_article_layout: RawArticleGrantLayout | None = None
        quest_article_layout: QuestArticleGrantLayout | None = None
        quest_article_layouts: tuple[QuestArticleGrantLayout, ...] = ()
        physical_instance_count = _integer(
            entry.get("physical_instance_count", 1), "world physical instance count", minimum=1,
        )
        mirror_package_raw = entry.get("mirror_of_package_path")
        mirror_index_raw = entry.get("mirror_of_export_index")
        mirror_of_package_path: str | None = None
        mirror_of_export_index: int | None = None
        if mirror_package_raw is not None or mirror_index_raw is not None:
            mirror_of_package_path = _string(mirror_package_raw, "mirror source package")
            mirror_of_export_index = _integer(mirror_index_raw, "mirror source export index", minimum=1)
            map_mirror = (
                is_world_map_package(mirror_of_package_path)
                and is_world_map_package(package_path)
                and destination_kind not in {
                    "RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT", "BLUEPRINT_CDO_ARTICLE_BULK",
                }
            )
            quest_mirror = (
                destination_kind == "QUEST_ARTICLE_GRANT"
                and mirror_of_package_path.startswith("/Game/Quest/")
                and package_path.startswith("/Game/Quest/")
            )
            if (
                not (map_mirror or quest_mirror)
                or (mirror_of_package_path.casefold(), mirror_of_export_index)
                == (package_path.casefold(), export_index)
            ):
                raise CatalogError("World Loot mirror metadata does not identify a compatible physical slot.")
        parent_blueprint_package = entry.get("parent_blueprint_package")
        if parent_blueprint_package is not None:
            parent_blueprint_package = _string(parent_blueprint_package, "parent Blueprint package")
            if not parent_blueprint_package.startswith("/Game/"):
                raise CatalogError("Parent Blueprint package must use a /Game path.")
        parent_blueprint_packages = _string_array(
            entry.get("parent_blueprint_packages", [parent_blueprint_package] if parent_blueprint_package else []),
            "parent Blueprint package chain",
        )
        if any(not package.startswith("/Game/") for package in parent_blueprint_packages):
            raise CatalogError("Parent Blueprint package chain must use /Game paths.")
        if len({package.casefold() for package in parent_blueprint_packages}) != len(parent_blueprint_packages):
            raise CatalogError("Parent Blueprint package chain contains duplicates.")
        if destination_kind == "RAW_ARTICLE_GRANT":
            destination_id = _string(entry.get("destination_id"), "direct grant destination id")
            display_name = _string(entry.get("display_name"), "direct grant display name")
            raw_article_layout = CatalogStore._parse_raw_article_layout(entry.get("raw_article_layout"), destination_id)
            spec = _RAW_ARTICLE_GRANT_SPECS.get(destination_id)
            if (
                spec is None
                or package_path != spec["package_path"]
                or source_file != spec["source_file"]
                or export_index != 2
                or export_name != spec["export_name"]
                or actor_class != spec["actor_class"]
                or actor_category != "DIRECT_ARTICLE_GRANT"
                or display_name != spec["display_name"]
                or protected or reasons or save_id is not None
                or not _same_reward_payloads(rewards, (RewardPayload(
                    spec["vanilla_article_path"], 1, True,
                ),))
            ):
                raise CatalogError("Direct Article grant does not match a confirmed, fingerprint-bound destination identity.")
        elif destination_kind == "QUEST_ARTICLE_GRANT":
            destination_id = _string(entry.get("destination_id"), "quest grant destination id")
            display_name = _string(entry.get("display_name"), "quest grant display name")
            raw_layouts = entry.get("quest_article_layouts")
            raw_layout = entry.get("quest_article_layout")
            if raw_layout is not None and raw_layouts is not None:
                raise CatalogError("Structured quest Article destination cannot mix singleton and bundled layouts.")
            if raw_layouts is not None:
                if not isinstance(raw_layouts, list) or not raw_layouts:
                    raise CatalogError("Structured quest Article layouts must be a non-empty array.")
                quest_article_layouts = tuple(
                    CatalogStore._parse_quest_article_layout(item, destination_id)
                    for item in raw_layouts
                )
                if len(quest_article_layouts) < 2:
                    raise CatalogError("Bundled quest Article layouts must contain at least two reward slots.")
            else:
                quest_article_layout = CatalogStore._parse_quest_article_layout(raw_layout, destination_id)
            spec = _QUEST_ARTICLE_GRANT_SPECS.get(destination_id)
            spec_layouts = spec.get("reward_layouts") if spec is not None else None
            if spec_layouts is None:
                expected_rewards = (RewardPayload(
                    spec["vanilla_article_path"], int(spec["count"]), bool(spec["notification"]),
                ),) if spec is not None else ()
            else:
                expected_rewards = tuple(RewardPayload(
                    item["vanilla_article_path"], int(item["count"]), bool(item["notification"]),
                ) for item in spec_layouts)
            layouts_for_destination = quest_article_layouts or (
                (quest_article_layout,) if quest_article_layout is not None else ()
            )
            if (
                spec is None
                or package_path != spec["package_path"]
                or source_file != spec["source_file"]
                or export_index != spec["export_index"]
                or export_name != spec["export_name"]
                or actor_class != spec["actor_class"]
                or actor_category != "DIRECT_ARTICLE_GRANT"
                or display_name != spec["display_name"]
                or protected or reasons or save_id is not None
                or mirror_of_package_path != spec.get("mirror_of_package_path")
                or mirror_of_export_index != spec.get("mirror_of_export_index")
                or not _same_reward_payloads(rewards, expected_rewards)
                or len(layouts_for_destination) != len(expected_rewards)
                or (bool(quest_article_layouts)
                    and tuple(layout.reward_index for layout in layouts_for_destination) != tuple(range(len(expected_rewards))))
                or tuple(layout.expected_article_path for layout in layouts_for_destination)
                != tuple(reward.article_path for reward in expected_rewards)
            ):
                raise CatalogError("Structured quest Article grant differs from its researched source identity.")
        if raw_article_layout is not None or quest_article_layout is not None or quest_article_layouts:
            if raw_article_layout is not None:
                expected_identity = SoftObjectPathIdentity(
                    FNameIdentity(
                        raw_article_layout.expected_path_name_map_value,
                        raw_article_layout.expected_vanilla_number,
                    ),
                    FNameIdentity(
                        raw_article_layout.expected_object_name_map_value,
                        raw_article_layout.expected_vanilla_number,
                    ),
                )
                identity_layouts = ((0, expected_identity),)
            else:
                quest_layout_values = quest_article_layouts or (
                    (quest_article_layout,) if quest_article_layout is not None else ()
                )
                identity_layouts = tuple((
                    layout.reward_index if quest_article_layouts else reward_index,
                    SoftObjectPathIdentity(
                    FNameIdentity(layout.expected_package_name_value, layout.expected_package_name_number),
                    FNameIdentity(layout.expected_asset_name_value, layout.expected_asset_name_number),
                    ),
                ) for reward_index, layout in enumerate(quest_layout_values))
            if len(rewards) != len(identity_layouts):
                raise CatalogError("Direct Article grant FName metadata does not cover every reward slot.")
            identified_rewards = list(rewards)
            for reward_index, expected_identity in identity_layouts:
                reward = rewards[reward_index]
                if (
                    expected_identity.resolved_path != reward.article_path
                    or expected_identity.resolved_asset_name != reward.article_path.rsplit("/", 1)[-1]
                    or (reward.soft_object_path_identity is not None
                        and reward.soft_object_path_identity != expected_identity)
                ):
                    raise CatalogError("Direct Article reward differs from its exact serialized FName identity.")
                identified_rewards[reward_index] = RewardPayload(
                    reward.article_path, reward.count, reward.notification, reward.content_class, expected_identity,
                )
            rewards = tuple(identified_rewards)
        elif destination_kind == "BLUEPRINT_CDO_ARTICLE_BULK":
            destination_id = _string(entry.get("destination_id"), "Blueprint CDO destination id")
            display_name = _string(entry.get("display_name"), "Blueprint CDO display name")
            if (
                not package_path.startswith("/Game/Environment/Item/")
                or source_file != package_path.removeprefix("/Game/") + ".uasset"
                or not export_name.startswith("Default__")
                or destination_id != "world_loot_cdo:" + package_path.casefold()
                or actor_category != "GROUND_PICKUP"
                or protected or reasons or save_id is not None
                or raw_article_layout is not None or quest_article_layout is not None
                or parent_blueprint_package is None
                or not parent_blueprint_packages
                or parent_blueprint_packages[0].casefold() != parent_blueprint_package.casefold()
            ):
                raise CatalogError("Blueprint CDO destination does not match a supported pickup class identity.")
        elif destination_kind == "RENEWABLE_INGREDIENT":
            destination_id = _string(entry.get("destination_id"), "renewable location id")
            display_name = _string(entry.get("display_name"), "renewable location display name")
            if (
                location_type != "RENEWABLE_INGREDIENT"
                or actor_category != "GROUND_PICKUP"
                or not protected or reasons != ("RENEWABLE_INGREDIENT_DISABLED",)
                or not is_world_map_package(package_path)
                or structural_class_package is None
                or len(rewards) != 1
            ):
                raise CatalogError("Renewable ingredient location is missing its structural lock metadata.")
        elif destination_kind == "INSTANCE_ARTICLE_BULK_OVERRIDE":
            destination_id = _string(entry.get("destination_id"), "instance override location id")
            display_name = _string(entry.get("display_name"), "instance override display name")
            source = _object(entry.get("instance_override_source"), "instance override source")
            cdo_package = _string(source.get("cdo_package"), "instance override CDO package")
            cdo_export_index = _integer(source.get("cdo_export_index"), "instance override CDO export index", minimum=1)
            cdo_export_name = _string(source.get("cdo_export_name"), "instance override CDO export name")
            class_package = _string(source.get("class_package"), "instance override class package")
            parents = _string_array(source.get("parent_blueprint_packages"), "instance override Blueprint parents")
            healing_location = location_type in {
                "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
            }
            expected_healing_type = _HEALING_UPGRADE_ARTICLES.get(rewards[0].article_path)
            if (
                location_type not in {
                    "NORMAL_WORLD_LOOT",
                    "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
                }
                or (healing_location and location_type != expected_healing_type)
                or len(rewards) != 1 or protected or reasons
                or actor_category not in {"GROUND_PICKUP", "TREASURE_BOX"}
                or (not healing_location and location_type != "NORMAL_WORLD_LOOT")
                or not is_world_map_package(package_path)
                or not cdo_package.startswith("/Game/Environment/Item/")
                or not cdo_export_name.startswith("Default__")
                or not class_package.startswith("/Game/Environment/Item/")
                or cdo_package.casefold() != class_package.casefold()
                or actor_class != class_package.rsplit("/", 1)[-1] + "_C"
                or cdo_export_name != "Default__" + actor_class
                or destination_id != f"{package_path}#{export_index}"
                or physical_instance_count != 1
                or not parents or any(not parent.startswith("/Game/") for parent in parents)
                or structural_class_package != class_package
            ):
                raise CatalogError("Per-instance ArticleBulk override does not match a supported physical pickup and CDO identity.")
            instance_override_source = WorldLootInstanceOverrideSource(
                cdo_package, cdo_export_index, cdo_export_name, class_package, parents,
            )
        elif destination_kind != "ARTICLE_BULK":
            raise CatalogError(f"Unknown World Loot destination serializer: {destination_kind}.")
        if destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"} and destination_id is not None:
            package_key = package_path.casefold()
            expected_class = (
                "DLC" if "/quest/dlc/" in package_key
                else "AFTER_ENDING" if (
                    "/epilogue_afterending/" in package_key
                    or destination_id.startswith("harmony_moon_")
                )
                else "BASE"
            )
            if content_class != expected_class:
                raise CatalogError(f"World direct-reward class differs from its confirmed source: {destination_id}.")
        # Generated MainWorld maps can hold DLC slots. Preserve their explicit
        # version-bound catalog class, and keep missing classification UNKNOWN.
        important_paths = tuple(
            reward.article_path for reward in rewards
            if "/item/important/" in reward.article_path.casefold()
        )
        all_paths = {reward.article_path.casefold() for reward in rewards}
        if important_paths and important_exclusions is None:
            raise CatalogError("Important Item exclusion metadata is required when Important rewards are catalogued.")
        key_reward = bool(important_exclusions and all_paths & important_exclusions.key_identities)
        quest_flag_reward = bool(important_exclusions and all_paths & important_exclusions.quest_flag_identities)
        if key_reward and (not protected or "KEY_EXCLUDED" not in {reason.upper() for reason in reasons}):
            raise CatalogError("A World location containing an excluded Key must stay locked with KEY_EXCLUDED.")
        if quest_flag_reward and (not protected or "QUEST_FLAG_EXCLUDED" not in {reason.upper() for reason in reasons}):
            raise CatalogError("A World location containing a quest/flag Important Item must stay locked with QUEST_FLAG_EXCLUDED.")
        if any(reason.casefold() == "important item" or reason.casefold().startswith("keyword:") for reason in reasons):
            raise CatalogError("World catalog still contains a non-authoritative keyword protection reason.")
        scripted = "withflagchange" in actor_class.casefold() or "withsmallscenario" in actor_class.casefold()
        unresolved = actor_category not in {"GROUND_PICKUP", "TREASURE_BOX"}
        excluded = source_file.casefold() in unsafe_packages
        direct_article = destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}
        blueprint_cdo = destination_kind == "BLUEPRINT_CDO_ARTICLE_BULK"
        special_location = destination_kind in {"RENEWABLE_INGREDIENT", "INSTANCE_ARTICLE_BULK_OVERRIDE"}
        if protected and not reasons:
            raise CatalogError("Protected world locations must include a protection reason.")
        location = WorldLootLocation(
            package_path=package_path,
            source_file=source_file,
            export_index=export_index,
            export_name=export_name,
            actor_class=actor_class,
            actor_category=actor_category,
            save_id=save_id,
            protected=protected,
            protection_reasons=reasons,
            rewards=rewards,
            destination_kind=destination_kind,
            destination_id=destination_id,
            display_name=display_name,
            raw_article_layout=raw_article_layout,
            quest_article_layout=quest_article_layout,
            quest_article_layouts=quest_article_layouts,
            content_class=content_class,
            physical_instance_count=physical_instance_count,
            parent_blueprint_package=parent_blueprint_package,
            parent_blueprint_packages=parent_blueprint_packages,
            location_type=location_type,
            structural_class_package=structural_class_package,
            instance_override_source=instance_override_source,
            mirror_of_package_path=mirror_of_package_path,
            mirror_of_export_index=mirror_of_export_index,
            scripted_reward_mode=scripted_reward_mode,
        )
        scripted_reward_allowed = (
            is_valid_scripted_reward_only_location(location)
            if scripted_reward_mode is not None else False
        )
        if scripted_reward_mode is not None and not scripted_reward_allowed:
            raise CatalogError("Scripted reward-only randomization does not match the researched Stinger pickup identity.")
        if ((scripted and not direct_article and not special_location and not scripted_reward_allowed)
                or (unresolved and not direct_article and not blueprint_cdo and not special_location)
                or excluded) and not protected:
            raise CatalogError(f"Protected world location is marked randomizable: {source_file}:{export_index}.")
        return location

    # Parse raw article layout
    @staticmethod
    def _parse_raw_article_layout(value: Any, destination_id: str) -> RawArticleGrantLayout:
        entry = _object(value, "direct raw Article layout")
        spec = _RAW_ARTICLE_GRANT_SPECS.get(destination_id)
        if spec is None:
            raise CatalogError("Direct raw Article layout has no explicitly supported destination.")
        layout = RawArticleGrantLayout(
            export_name=_string(entry.get("export_name"), "raw grant export name"),
            serial_size=_integer(entry.get("serial_size"), "raw grant serial size", minimum=1),
            path_name_index_offset=_integer(entry.get("path_name_index_offset"), "path NameMap index offset"),
            path_number_offset=_integer(entry.get("path_number_offset"), "path FName Number offset"),
            object_name_index_offset=_integer(entry.get("object_name_index_offset"), "object NameMap index offset"),
            object_number_offset=_integer(entry.get("object_number_offset"), "object FName Number offset"),
            expected_path_name_index=_integer(entry.get("expected_path_name_index"), "expected path NameMap index"),
            expected_object_name_index=_integer(entry.get("expected_object_name_index"), "expected object NameMap index"),
            expected_path_name_map_value=_string(entry.get("expected_path_name_map_value"), "vanilla path NameMap value"),
            expected_object_name_map_value=_string(entry.get("expected_object_name_map_value"), "vanilla object NameMap value"),
            expected_vanilla_article_path=_string(entry.get("expected_vanilla_article_path"), "vanilla Article path"),
            expected_vanilla_number=_integer(entry.get("expected_vanilla_number"), "vanilla FName Number"),
            source_uasset_sha256=_string(entry.get("source_uasset_sha256"), "source .uasset SHA-256"),
            source_uexp_sha256=_string(entry.get("source_uexp_sha256"), "source .uexp SHA-256"),
        )
        expected = RawArticleGrantLayout(
            str(spec["export_name"]), 55, 0x16, 0x1A, 0x1E, 0x22,
            0, 27,
            str(spec["path_name_map_value"]), str(spec["object_name_map_value"]),
            str(spec["vanilla_article_path"]), int(spec["vanilla_number"]),
            str(spec["source_uasset_sha256"]), str(spec["source_uexp_sha256"]),
        )
        if layout != expected or not _SHA256.fullmatch(layout.source_uasset_sha256) or not _SHA256.fullmatch(layout.source_uexp_sha256):
            raise CatalogError("Direct raw Article layout differs from its researched, fingerprint-bound slot definition.")
        return layout

    # Parse quest article layout
    @staticmethod
    def _parse_quest_article_layout(value: Any, destination_id: str) -> QuestArticleGrantLayout:
        entry = _object(value, "structured quest Article layout")
        spec = _QUEST_ARTICLE_GRANT_SPECS.get(destination_id)
        if spec is None:
            raise CatalogError("Structured quest Article layout has no explicitly supported destination.")
        reward_index = _integer(entry.get("reward_index"), "quest Articles index")
        reward_layouts = spec.get("reward_layouts")
        if reward_layouts is not None:
            if not isinstance(reward_layouts, list):
                raise CatalogError("Registered quest reward layouts must be an array.")
            expected_spec = next((item for item in reward_layouts if item.get("reward_index") == reward_index), None)
            if expected_spec is None:
                raise CatalogError("Structured quest Article layout names an unregistered reward index.")
        else:
            expected_spec = spec
        sub_path = entry.get("expected_sub_path_string", "")
        if not isinstance(sub_path, str):
            raise CatalogError("quest SubPathString expectation must be a string.")
        raw_articles_count = entry.get("articles_count")
        articles_count = (
            None if raw_articles_count is None
            else _integer(raw_articles_count, "quest Articles count", minimum=1)
        )
        raw_preserved_export_index = entry.get("preserved_export_index")
        raw_preserved_export_name = entry.get("preserved_export_name")
        if (raw_preserved_export_index is None) != (raw_preserved_export_name is None):
            raise CatalogError("preserved quest export identity must include both its index and name.")
        preserved_export_index = (
            None if raw_preserved_export_index is None
            else _integer(raw_preserved_export_index, "preserved quest export index", minimum=1)
        )
        preserved_export_name = (
            None if raw_preserved_export_name is None
            else _string(raw_preserved_export_name, "preserved quest export name")
        )
        raw_mirrored_exports = entry.get("mirrored_exports", [])
        if not isinstance(raw_mirrored_exports, list):
            raise CatalogError("mirrored quest exports must be an array.")
        mirrored_exports = tuple(
            QuestArticleGrantMirror(
                export_index=_integer(_object(item, "mirrored quest export").get("export_index"), "mirrored quest export index", minimum=1),
                export_name=_string(_object(item, "mirrored quest export").get("export_name"), "mirrored quest export name"),
            )
            for item in raw_mirrored_exports
        )
        if len({mirror.export_index for mirror in mirrored_exports}) != len(mirrored_exports):
            raise CatalogError("mirrored quest export indexes must be unique.")
        raw_strict_snapshot = entry.get("require_strict_export_snapshots", False)
        strict_snapshot = _boolean(raw_strict_snapshot, "strict quest export snapshots")
        layout = QuestArticleGrantLayout(
            export_name=_string(entry.get("export_name"), "quest grant export name"),
            bulk_index=_integer(entry.get("bulk_index"), "quest ArticleBulkInfo index"),
            reward_index=reward_index,
            expected_article_path=_string(entry.get("expected_article_path"), "vanilla quest Article path"),
            expected_package_name_value=_string(entry.get("expected_package_name_value"), "vanilla package FName value"),
            expected_package_name_number=_integer(entry.get("expected_package_name_number"), "vanilla package FName Number"),
            expected_asset_name_value=_string(entry.get("expected_asset_name_value"), "vanilla asset FName value"),
            expected_asset_name_number=_integer(entry.get("expected_asset_name_number"), "vanilla asset FName Number"),
            count=_integer(entry.get("count"), "quest reward Count", minimum=1),
            notification=_boolean(entry.get("notification"), "quest reward notification"),
            expected_sub_path_string=sub_path,
            expected_articles_count=articles_count,
            preserved_export_index=preserved_export_index,
            preserved_export_name=preserved_export_name,
            mirrored_exports=mirrored_exports,
            require_strict_export_snapshots=strict_snapshot,
            source_uasset_sha256=(
                None if entry.get("source_uasset_sha256") is None
                else _string(entry.get("source_uasset_sha256"), "quest source .uasset SHA-256")
            ),
            source_uexp_sha256=(
                None if entry.get("source_uexp_sha256") is None
                else _string(entry.get("source_uexp_sha256"), "quest source .uexp SHA-256")
            ),
        )
        expected = QuestArticleGrantLayout(
            str(spec["export_name"]), int(expected_spec["bulk_index"]), int(expected_spec["reward_index"]),
            str(expected_spec["vanilla_article_path"]), str(expected_spec["package_name_value"]), int(expected_spec["package_name_number"]),
            str(expected_spec["asset_name_value"]), int(expected_spec["asset_name_number"]),
            int(expected_spec["count"]), bool(expected_spec["notification"]), str(expected_spec["sub_path_string"]),
            expected_spec.get("articles_count"), expected_spec.get("preserved_export_index"), expected_spec.get("preserved_export_name"),
            tuple(QuestArticleGrantMirror(int(item["export_index"]), str(item["export_name"])) for item in expected_spec.get("mirrored_exports", [])),
            bool(expected_spec.get("require_strict_export_snapshots", False)),
            expected_spec.get("source_uasset_sha256"), expected_spec.get("source_uexp_sha256"),
        )
        if layout != expected:
            raise CatalogError("Structured quest Article layout differs from its researched source identity.")
        return layout

    # Parse material
    @staticmethod
    def _parse_material(value: Any) -> MaterialLocation:
        entry = _object(value, "material location")
        source_file = _string(entry.get("source_file"), "material source file")
        if (
            not source_file.casefold().endswith(".umap")
            or Path(source_file).name != source_file
            or "/" in source_file
            or "\\" in source_file
        ):
            raise CatalogError("Material source file must be a map filename.")
        package_path = _string(entry.get("package_path"), "material package path")
        expected_package = "/Game/Map/MainWorld/MainWorld/_Generated_/" + Path(source_file).stem
        if package_path != expected_package:
            raise CatalogError("Material package path does not match its source map.")
        export_index = _integer(entry.get("export_index"), "material export index")
        export_name = _string(entry.get("export_name"), "material export name")
        instance_name_class = _string(entry.get("actor_class"), "material actor instance-name prefix")
        if not export_name.startswith(instance_name_class + "_UAID_"):
            raise CatalogError("Material actor export name does not match its instance-name prefix.")
        resource_class_package = _string(
            entry.get("resource_class_package"), "material resource class package"
        )
        if not resource_class_package.startswith("/Game/"):
            raise CatalogError("Material resource class package must be a game virtual path.")
        resource_class_name = _string(
            entry.get("resource_class_name"), "material resource class name"
        )
        if resource_class_name != resource_class_package.rsplit("/", 1)[-1] + "_C":
            raise CatalogError("Material resource class package and name do not identify the same Blueprint.")
        save_id = _string(entry.get("save_id"), "material UniqueSaveID")
        protected = _boolean(entry.get("protected"), "material protected")
        reason = entry.get("protection_reason")
        if reason is not None:
            reason = _string(reason, "material protection reason")
        if protected or reason is not None:
            raise CatalogError("Excluded or protected material records must not enter the pool.")
        reward = CatalogStore._parse_reward(entry.get("reward"))
        if reward.count != 1 or not reward.notification:
            raise CatalogError("Renewable material rewards must keep Count=1 and bNotification=True.")
        return MaterialLocation(
            package_path=package_path,
            source_file=source_file,
            export_index=export_index,
            export_name=export_name,
            # The export-name prefix can differ from the ClassIndex import. The
            # structural resource package is the validated Blueprint class the
            # map actor actually uses.
            actor_class=resource_class_name,
            resource_class_package=resource_class_package,
            resource_class_name=resource_class_name,
            reward=reward,
            save_id=save_id,
            protected=protected,
            protection_reason=reason,
            where_content_class=_content_class(entry.get("where_content_class", "UNKNOWN"), "material WHERE"),
            resource_content_class=_content_class(entry.get("resource_content_class", "UNKNOWN"), "material resource WHAT"),
            reward_content_class=_content_class(entry.get("reward_content_class", "UNKNOWN"), "material reward WHAT"),
        )

    # Parse shop product
    @staticmethod
    def _parse_shop_product(
        value: Any,
        important_exclusions: ImportantItemExclusions | None = None,
    ) -> ShopProduct:
        entry = _object(value, "shop product")
        family = _string(entry.get("family"), "shop family").upper()
        if family not in _SHOP_FAMILIES:
            raise CatalogError(f"Unknown shop family: {family}.")
        path = _string(entry.get("original_product_path"), "shop article path")
        protected = _boolean(entry.get("protected"), "shop protected")
        reason = entry.get("protection_reason")
        if reason is not None:
            reason = _string(reason, "shop protection reason")
        important = "/item/important/" in path.casefold()
        if important and important_exclusions is None:
            raise CatalogError("Important Item exclusion metadata is required when Important Shop products are catalogued.")
        path_key = path.casefold()
        if important_exclusions is not None and path_key in important_exclusions.key_identities:
            if not protected or reason is None or reason.upper() != "KEY_EXCLUDED":
                raise CatalogError(f"Excluded Key Shop product is not locked with KEY_EXCLUDED: {path}.")
        elif important_exclusions is not None and path_key in important_exclusions.quest_flag_identities:
            if not protected or reason is None or reason.upper() != "QUEST_FLAG_EXCLUDED":
                raise CatalogError(f"Quest/flag Important Shop product is not locked with QUEST_FLAG_EXCLUDED: {path}.")
        elif reason is not None and (reason.casefold() == "important item" or reason.casefold().startswith("keyword:important")):
            raise CatalogError("Shop catalog still contains the obsolete blanket Important Item protection reason.")
        if protected and reason is None:
            raise CatalogError("Protected shop products must include a protection reason.")
        return ShopProduct(
            family=family,
            shop_row=_string(entry.get("shop_row"), "shop row"),
            product_index=_integer(entry.get("product_index"), "shop product index"),
            original_product_path=path,
            item_object=_string(entry.get("item_object"), "shop item object", allow_empty=True),
            price=_integer(entry.get("price"), "shop price"),
            uses_stock=_boolean(entry.get("uses_stock"), "shop uses_stock"),
            stock=_integer(entry.get("stock"), "shop stock"),
            protected=protected,
            protection_reason=reason,
            what_content_class=_content_class(
                entry.get("what_content_class", "UNKNOWN"),
                "shop WHAT",
            ),
        )

    # Audit protected key items
    @staticmethod
    def _audit_important_items(
        world: tuple[WorldLootLocation, ...],
        shops: tuple[ShopProduct, ...],
        exclusions: ImportantItemExclusions | None,
    ) -> tuple[dict[str, int], tuple[dict[str, str], ...]]:
        """Classify each actual Important reward occurrence into one policy state."""
        if exclusions is None:
            return {
                "eligible_articles": 0,
                "eligible_occurrences": 0,
                "key_excluded_occurrences": 0,
                "quest_flag_excluded_occurrences": 0,
                "other_safety_locked_occurrences": 0,
                "important_world_locations_unlocked": 0,
                "important_shop_products_unlocked": 0,
                "important_world_locations_other_locked": 0,
                "important_shop_products_other_locked": 0,
            }, ()
        eligible_paths: set[str] = set()
        eligible_occurrences = 0
        key_occurrences = 0
        quest_occurrences = 0
        safety_occurrences = 0
        world_unlocked: set[tuple[str, int]] = set()
        shop_unlocked = 0
        world_other_locked: set[tuple[str, int]] = set()
        shop_other_locked = 0
        classifications: list[dict[str, str]] = []

        # Classify the reward source
        def classify(path: str, protected: bool) -> str:
            normalized = path.casefold()
            if normalized in exclusions.key_identities:
                return "KEY_EXCLUDED"
            if normalized in exclusions.quest_flag_identities:
                return "QUEST_FLAG_EXCLUDED"
            return "OTHER_EXISTING_SAFETY_LOCK" if protected else "ELIGIBLE"

        for location in world:
            important = [reward.article_path for reward in location.rewards
                         if "/item/important/" in reward.article_path.casefold()]
            if not important:
                continue
            state_counts = {classify(path, location.protected) for path in important}
            for path in important:
                state = classify(path, location.protected)
                classifications.append({
                    "article_path": path,
                    "source": "WORLD",
                    "destination": f"{location.package_path}#{location.export_index}",
                    "state": state,
                })
                if state == "ELIGIBLE":
                    eligible_occurrences += 1
                    eligible_paths.add(path.casefold())
                elif state == "KEY_EXCLUDED":
                    key_occurrences += 1
                elif state == "QUEST_FLAG_EXCLUDED":
                    quest_occurrences += 1
                else:
                    safety_occurrences += 1
            if "ELIGIBLE" in state_counts:
                world_unlocked.add((location.package_path.casefold(), location.export_index))
            if "OTHER_EXISTING_SAFETY_LOCK" in state_counts:
                world_other_locked.add((location.package_path.casefold(), location.export_index))
        for product in shops:
            path = product.original_product_path
            if "/item/important/" not in path.casefold():
                continue
            state = classify(path, product.protected)
            classifications.append({
                "article_path": path,
                "source": "SHOP",
                "destination": f"{product.family}:{product.shop_row}:{product.product_index}",
                "state": state,
            })
            if state == "ELIGIBLE":
                eligible_occurrences += 1
                eligible_paths.add(path.casefold())
                shop_unlocked += 1
            elif state == "KEY_EXCLUDED":
                key_occurrences += 1
            elif state == "QUEST_FLAG_EXCLUDED":
                quest_occurrences += 1
            else:
                safety_occurrences += 1
                shop_other_locked += 1
        classifications.sort(key=lambda item: (
            item["article_path"].casefold(), item["source"], item["destination"].casefold(),
        ))
        return {
            "eligible_articles": len(eligible_paths),
            "eligible_occurrences": eligible_occurrences,
            "key_excluded_occurrences": key_occurrences,
            "quest_flag_excluded_occurrences": quest_occurrences,
            "other_safety_locked_occurrences": safety_occurrences,
            "important_world_locations_unlocked": len(world_unlocked),
            "important_shop_products_unlocked": shop_unlocked,
            "important_world_locations_other_locked": len(world_other_locked),
            "important_shop_products_other_locked": shop_other_locked,
        }, tuple(classifications)
