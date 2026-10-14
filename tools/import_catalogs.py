"""Convert development CSV research into deterministic runtime JSON catalogs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cv2randomizer.game.important_items import (  # noqa: E402
    ImportantItemExclusionError,
    ImportantItemExclusionStore,
    ImportantItemExclusions,
)
from cv2randomizer.game.catalogs import (  # noqa: E402
    FLAG_PICKUP_ARTICLE_ONLY_ALLOWLIST,
    SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS,
    SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO,
    _SCRIPTED_SCENARIO_REWARD_SLOT_SPECS,
    RewardPayload,
    WorldLootLocation,
    is_valid_scripted_reward_only_location,
    is_world_map_package,
    world_map_package_for_source,
)
from cv2randomizer.game.fname_identity import FNameIdentity, SoftObjectPathIdentity  # noqa: E402
from cv2randomizer.game.content_classification import (  # noqa: E402
    article_class_counts,
    build_explicit_article_class_index,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data" / "catalogs" / "current"
DEFAULT_EXCLUSIONS = PROJECT_ROOT / "data" / "exclusions" / "unsafe_packages.txt"
DEFAULT_OUTPUT_DIR = DEFAULT_SOURCE_DIR
GENERATED_MAP_PREFIX = "/Game/Map/MainWorld/MainWorld/_Generated_/"
SHOP_FAMILIES = {
    "NORMAL": "NORMAL",
    "LUXURY": "LUXURY",
    "TRADE": "TRADE",
    "DLC": "DLC",
    "TRADE_DLC": "TRADE_DLC",
}
PICKUP_COLUMNS = (
    "GROUND_PICKUP",
    "FLAG_PICKUP",
    "SCENARIO_PICKUP",
    "TREASURE_BOX",
    "RENEWABLE_MATERIAL",
    "DROP_ITEM",
)
MATERIAL_MAP_PREFIX = "/Game/Map/MainWorld/MainWorld/_Generated_/"
MATERIAL_COUNTS = {"locations": 734, "resource_classes": 38, "rewards": 38}
CONTENT_CLASSES = frozenset({"BASE", "DLC", "AFTER_ENDING", "UNKNOWN"})
# Confirmed by the installed Blueprint parent chain: this concrete item actor
# derives from /Game/Blueprint/FieldGimmick/PickupItem/BP_PickupItem. Older
# research rows sometimes mislabeled these map instances as unresolved.
KNOWN_PICKUP_ACTOR_CATEGORIES = {
    "BP_it00_001_A_0_C": "GROUND_PICKUP",
}


class CatalogImportError(ValueError):
    """Raised when development research cannot safely become a catalog."""


# Find soft object path identity
def _soft_object_path_identity_from_dict(value: dict[str, Any] | None) -> SoftObjectPathIdentity | None:
    if not isinstance(value, dict):
        return None
    package = value.get("package_name")
    asset = value.get("asset_name")
    if not isinstance(package, dict) or not isinstance(asset, dict):
        return None
    try:
        return SoftObjectPathIdentity(
            FNameIdentity(str(package["value"]), int(package["number"])),
            FNameIdentity(str(asset["value"]), int(asset["number"])),
        )
    except (KeyError, TypeError, ValueError):
        return None


# Read csv
def read_csv(path: Path, required_headers: set[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            headers = set(reader.fieldnames or ())
            missing = required_headers - headers
            if missing:
                raise CatalogImportError(f"{path.name} is missing columns: {', '.join(sorted(missing))}")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        raise CatalogImportError(f"Could not read {path}: {error}") from error
    if not rows:
        raise CatalogImportError(f"{path.name} contains no data rows.")
    return rows


# Parse int
def parse_int(value: str, field: str, *, minimum: int = 0) -> int:
    try:
        result = int(value, 10)
    except (TypeError, ValueError) as error:
        raise CatalogImportError(f"{field} must be an integer; got {value!r}.") from error
    if result < minimum:
        raise CatalogImportError(f"{field} must be at least {minimum}; got {result}.")
    return result


# Parse bool
def parse_bool(value: str, field: str) -> bool:
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise CatalogImportError(f"{field} must be True or False; got {value!r}.")


# Parse content class
def parse_content_class(value: str | None, field: str) -> str:
    """Preserve explicit research labels; missing labels remain UNKNOWN."""
    normalized = (value or "").strip().upper() or "UNKNOWN"
    if normalized not in CONTENT_CLASSES:
        raise CatalogImportError(
            f"{field} must be one of {', '.join(sorted(CONTENT_CLASSES))}; got {value!r}."
        )
    return normalized


# Apply explicit article classes
def apply_explicit_article_classes(
    world: dict[str, Any],
    materials: dict[str, Any],
    shops: dict[str, Any],
    article_index: Any,
) -> dict[str, int]:
    """Fill UNKNOWN WHAT labels only when an exact reviewed Article identity proves them."""
    used_classes = {
        path.casefold()
        for path in (
            *(reward.get("article_path", "") for location in world.get("locations", [])
              for reward in location.get("rewards", [])),
            *(location.get("reward", {}).get("article_path", "") for location in materials.get("locations", [])),
            *(product.get("original_product_path", "") for product in shops.get("products", [])),
        )
    }
    conflicts = {
        path.casefold(): evidence
        for path, evidence in article_index.conflicts
    }
    conflicting_used = sorted(used_classes & conflicts.keys())
    if conflicting_used:
        raise CatalogImportError(
            "Conflicting explicit Article classifications exist for: "
            + ", ".join(article_index.conflicts[[path.casefold() for path, _ in article_index.conflicts].index(key)][0]
                        for key in conflicting_used)
        )

    applied = {"world_what": 0, "material_reward_what": 0, "material_article_what": 0, "shop_what": 0}

    # Check exact class
    def exact_class(article_path: str) -> str | None:
        entry = article_index.get(article_path)
        return entry.content_class if entry is not None else None

    for location in world.get("locations", []):
        for reward in location.get("rewards", []):
            if reward.get("content_class", "UNKNOWN").upper() == "UNKNOWN":
                inferred = exact_class(reward.get("article_path", ""))
                if inferred:
                    reward["content_class"] = inferred
                    applied["world_what"] += 1
    for location in materials.get("locations", []):
        article_path = location.get("reward", {}).get("article_path", "")
        inferred = exact_class(article_path)
        if location.get("reward_content_class", "UNKNOWN").upper() == "UNKNOWN" and inferred:
            location["reward_content_class"] = inferred
            applied["material_reward_what"] += 1
        if location.get("reward", {}).get("content_class", "UNKNOWN").upper() == "UNKNOWN" and inferred:
            location["reward"]["content_class"] = inferred
            applied["material_article_what"] += 1
    for product in shops.get("products", []):
        if product.get("what_content_class", "UNKNOWN").upper() == "UNKNOWN":
            inferred = exact_class(product.get("original_product_path", ""))
            if inferred:
                product["what_content_class"] = inferred
                applied["shop_what"] += 1
    return applied


# Read unsafe packages
def read_unsafe_packages(path: Path) -> list[str]:
    try:
        entries = [
            line.strip()
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeError) as error:
        raise CatalogImportError(f"Could not read unsafe-package exclusions: {error}") from error
    if len(entries) != len(set(entries)):
        raise CatalogImportError("unsafe_packages.txt contains duplicate package names.")
    return sorted(entries, key=str.casefold)


# Read explicit world source protections
def read_explicit_world_source_protections(path: Path) -> dict[tuple[str, int], tuple[str, ...]]:
    """Load curated lock reasons for unresolved World Loot sources."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CatalogImportError(f"Could not read curated World Loot protections: {error}") from error
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise CatalogImportError("Curated World Loot protections must contain an items array.")
    protected: dict[tuple[str, int], tuple[str, ...]] = {}
    for item in items:
        source = item.get("vanilla_source") if isinstance(item, dict) else None
        if not isinstance(source, dict) or source.get("type") != "GROUND_PICKUP":
            continue
        if source.get("protected") is not True:
            continue
        package_path = source.get("package_path")
        export_index = source.get("export_index")
        reasons = source.get("protection_reasons")
        if (
            not isinstance(package_path, str) or not package_path.startswith("/Game/")
            or type(export_index) is not int or export_index < 0
            or not isinstance(reasons, list) or not reasons
            or any(not isinstance(reason, str) or not reason.strip() for reason in reasons)
        ):
            raise CatalogImportError("A protected curated World Loot source has invalid identity/reason metadata.")
        key = (package_path.casefold(), export_index)
        if key in protected:
            raise CatalogImportError("Curated World Loot source protections contain duplicate slots.")
        protected[key] = tuple(reason.strip() for reason in reasons)
    return protected


# Read explicit scripted reward modes
def read_explicit_scripted_reward_modes(path: Path) -> dict[tuple[str, int], str]:
    """Load the one researched Jail exception that changes an Article reward only."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CatalogImportError(f"Could not read curated Jail source policies: {error}") from error
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise CatalogImportError("Curated Jail source policies must contain an items array.")
    modes: dict[tuple[str, int], str] = {}
    for item in items:
        source = item.get("vanilla_source") if isinstance(item, dict) else None
        if not isinstance(source, dict):
            continue
        mode = source.get("scripted_reward_mode")
        if mode is None:
            continue
        if (
            mode != SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS
            or item.get("object") != "DA_Jail_Stinger"
            or item.get("asset_path") != "/Game/Blueprint/Article/Jail/DA_Jail_Stinger"
            or source.get("type") != "GROUND_PICKUP"
            or source.get("movable_world_loot") is not True
            or source.get("protected") is not False
            or source.get("protection_reasons") != []
            or source.get("package_path") != "/Game/Map/MainWorld/MainWorld/_Generated_/61Y5LAV87NWVD213F35X2VK87"
            or source.get("source_file") != "61Y5LAV87NWVD213F35X2VK87.umap"
            or source.get("export_index") != 272
            or source.get("export_name") != "BP_PickupItem_WithFlagChange_C_UAID_A8A159A4A14B9D8902_1822678819_3eb3acb6c4c82637"
            or source.get("reward_index") != 0
            or source.get("count") != 1
            or source.get("notification") is not True
            or source.get("save_id") != "5500DB0E-438B-2632-00A8-A5B994AAFAA2"
        ):
            raise CatalogImportError("Scripted reward-only Jail policy does not match the researched Sunken City Stinger source.")
        key = (source["package_path"].casefold(), source["export_index"])
        if key in modes:
            raise CatalogImportError("Curated Jail scripted reward policies contain duplicate source slots.")
        modes[key] = mode
        mirror_key = (
            "/Game/Map/MainWorld/DlcWorld/_Generated_/3Z2KPIGCUSLGYDYC5DQHT6AFA".casefold(),
            42,
        )
        if mirror_key in modes:
            raise CatalogImportError("Curated Jail scripted reward policies contain duplicate mirror slots.")
        modes[mirror_key] = mode
    for (package_path, export_index) in _SCRIPTED_SCENARIO_REWARD_SLOT_SPECS:
        key = (package_path.casefold(), export_index)
        if key in modes:
            raise CatalogImportError("Curated scripted reward policies contain duplicate source slots.")
        modes[key] = SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO
    return modes


# Import world loot
def import_world_loot(
    source: Path,
    unsafe_packages: set[str],
    important_exclusions: ImportantItemExclusions | None = None,
    protected_source_reasons: dict[tuple[str, int], tuple[str, ...]] | None = None,
    scripted_reward_modes: dict[tuple[str, int], str] | None = None,
) -> dict[str, Any]:
    required = {
        "map_file", "export_index", "export_name", "location_type", "reward_index",
        "item_path", "count", "bNotification", "unique_save_id", "progression_risk",
        "progression_reason", "scanner_flag", "scanner_scenario",
        "article_package_literal_fname", "article_package_fname_number",
        "article_asset_literal_fname", "article_asset_fname_number",
    }
    rows = read_csv(source, required)
    scripted_reward_modes = scripted_reward_modes or {}
    seen_scripted_reward_slots: set[tuple[str, int]] = set()
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        map_file = row["map_file"].strip()
        if not map_file.casefold().endswith(".umap") or Path(map_file).name != map_file:
            raise CatalogImportError(f"Invalid generated-map filename: {map_file!r}.")
        try:
            package_path = world_map_package_for_source(
                map_file, (row.get("map_package") or "").strip() or None,
            )
        except ValueError as error:
            raise CatalogImportError(f"Invalid world map package for {map_file}: {error}") from error
        export_index = parse_int(row["export_index"], "export_index")
        reward_index = parse_int(row["reward_index"], "reward_index")
        export_name = row["export_name"].strip()
        article_path = row["item_path"].strip()
        if not export_name or not article_path.startswith("/Game/"):
            raise CatalogImportError(f"Invalid export or reward identity in {map_file}:{export_index}.")
        package_literal = row["article_package_literal_fname"].strip()
        package_number_text = row["article_package_fname_number"].strip()
        asset_literal = row["article_asset_literal_fname"].strip()
        asset_number_text = row["article_asset_fname_number"].strip()
        identity_values = (package_literal, package_number_text, asset_literal, asset_number_text)
        soft_object_path_identity = None
        if any(identity_values):
            if not all(identity_values):
                raise CatalogImportError(
                    f"Incomplete Article FName identity in {map_file}:{export_index}:{reward_index}."
                )
            package_number = parse_int(package_number_text, "article_package_fname_number")
            asset_number = parse_int(asset_number_text, "article_asset_fname_number")
            resolved_package = _resolve_fname(package_literal, package_number)
            resolved_asset = _resolve_fname(asset_literal, asset_number)
            if resolved_package != article_path or resolved_asset != article_path.rsplit("/", 1)[-1]:
                raise CatalogImportError(
                    f"Article FName identity does not resolve exactly to {article_path} "
                    f"in {map_file}:{export_index}:{reward_index}."
                )
            soft_object_path_identity = {
                "package_name": {"value": package_literal, "number": package_number},
                "asset_name": {"value": asset_literal, "number": asset_number},
            }
        grouped[(map_file, export_index)].append(
            {
                "reward_index": reward_index,
                "package_path": package_path,
                "mirror_of_package_path": (row.get("mirror_of_package_path") or "").strip() or None,
                "mirror_of_export_index": (
                    parse_int(row["mirror_of_export_index"], "mirror_of_export_index", minimum=1)
                    if (row.get("mirror_of_export_index") or "").strip() else None
                ),
                "article_path": article_path,
                "count": parse_int(row["count"], "count", minimum=1),
                "notification": parse_bool(row["bNotification"], "bNotification"),
                "soft_object_path_identity": soft_object_path_identity,
                "location_type": row["location_type"].strip(),
                "export_name": export_name,
                "unique_save_id": row["unique_save_id"].strip() or None,
                "progression_risk": row["progression_risk"].strip().upper(),
                "progression_reason": row["progression_reason"].strip(),
                "scanner_flag": row["scanner_flag"].strip(),
                "scanner_scenario": row["scanner_scenario"].strip(),
                "where_content_class": parse_content_class(
                    row.get("where_content_class"), "world WHERE content class",
                ),
                "what_content_class": parse_content_class(
                    row.get("what_content_class"), "world WHAT content class",
                ),
                "actor_class_override": (row.get("actor_class") or "").strip() or None,
                "destination_kind": (row.get("destination_kind") or "ARTICLE_BULK").strip() or "ARTICLE_BULK",
                "override_cdo_package": (row.get("override_cdo_package") or "").strip() or None,
                "override_cdo_export_index": (row.get("override_cdo_export_index") or "").strip() or None,
                "override_cdo_export_name": (row.get("override_cdo_export_name") or "").strip() or None,
                "override_class_package": (row.get("override_class_package") or "").strip() or None,
                "override_parent_blueprint_packages": (row.get("override_parent_blueprint_packages") or "").strip() or None,
            }
        )

    locations: list[dict[str, Any]] = []
    for (map_file, export_index), rewards in sorted(grouped.items()):
        rewards.sort(key=lambda entry: entry["reward_index"])
        reward_indices = [reward["reward_index"] for reward in rewards]
        if len(reward_indices) != len(set(reward_indices)):
            raise CatalogImportError(f"Duplicate reward index for {map_file}:{export_index}.")
        export_names = {reward["export_name"] for reward in rewards}
        save_ids = {reward["unique_save_id"] for reward in rewards}
        location_types = {reward["location_type"] for reward in rewards}
        where_classes = {reward["where_content_class"] for reward in rewards}
        package_paths = {reward["package_path"] for reward in rewards}
        mirror_packages = {reward["mirror_of_package_path"] for reward in rewards}
        mirror_indices = {reward["mirror_of_export_index"] for reward in rewards}
        if (len(export_names) != 1 or len(save_ids) != 1 or len(location_types) != 1
                or len(where_classes) != 1 or len(package_paths) != 1
                or len(mirror_packages) != 1 or len(mirror_indices) != 1):
            raise CatalogImportError(f"Inconsistent grouped location identity for {map_file}:{export_index}.")
        if (None in mirror_packages) != (None in mirror_indices):
            raise CatalogImportError(f"Incomplete World Loot mirror identity for {map_file}:{export_index}.")

        export_name = rewards[0]["export_name"]
        location_type = rewards[0]["location_type"]
        actor_class = export_name.split("_UAID_", maxsplit=1)[0]
        package_path = next(iter(package_paths))
        actor_class_overrides = {reward["actor_class_override"] for reward in rewards}
        destination_kinds = {reward["destination_kind"] for reward in rewards}
        override_fields = (
            "override_cdo_package", "override_cdo_export_index", "override_cdo_export_name",
            "override_class_package", "override_parent_blueprint_packages",
        )
        override_values_by_field = {
            field: {reward[field] for reward in rewards} for field in override_fields
        }
        if (len(actor_class_overrides) != 1 or len(destination_kinds) != 1
                or any(len(values) != 1 for values in override_values_by_field.values())):
            raise CatalogImportError(f"Inconsistent World Loot serializer metadata for {map_file}:{export_index}.")
        actor_override = next(iter(actor_class_overrides))
        if actor_override is not None:
            actor_class = actor_override
        destination_kind = next(iter(destination_kinds))
        override_values = {field: next(iter(values)) for field, values in override_values_by_field.items()}
        override_is_populated = [value is not None for value in override_values.values()]
        instance_override_source = None
        if destination_kind == "INSTANCE_ARTICLE_BULK_OVERRIDE":
            if (len(rewards) != 1 or rewards[0]["reward_index"] != 0 or not all(override_is_populated)):
                raise CatalogImportError(
                    f"Per-instance World Loot override metadata is incomplete for {map_file}:{export_index}."
                )
            parent_packages = [
                value.strip()
                for value in override_values["override_parent_blueprint_packages"].split("|")
                if value.strip()
            ]
            cdo_package = override_values["override_cdo_package"]
            class_package = override_values["override_class_package"]
            class_name = class_package.rsplit("/", 1)[-1] + "_C" if class_package else ""
            cdo_name = override_values["override_cdo_export_name"]
            cdo_index = parse_int(
                override_values["override_cdo_export_index"], "override CDO export index", minimum=1,
            )
            if (location_type not in {"GROUND_PICKUP", "TREASURE_BOX"}
                    or not package_path.startswith("/Game/Map/") or not class_package
                    or cdo_package != class_package or actor_class != class_name
                    or cdo_name != "Default__" + class_name or not parent_packages
                    or any(not value.startswith("/Game/") for value in parent_packages)):
                raise CatalogImportError(
                    f"Invalid physical actor/CDO identity for World Loot override {map_file}:{export_index}."
                )
            instance_override_source = {
                "cdo_package": cdo_package,
                "cdo_export_index": cdo_index,
                "cdo_export_name": cdo_name,
                "class_package": class_package,
                "parent_blueprint_packages": parent_packages,
            }
        elif destination_kind != "ARTICLE_BULK" or any(override_is_populated):
            raise CatalogImportError(
                f"Unsupported or incomplete World Loot destination serializer for {map_file}:{export_index}."
            )
        explicit_protection = (protected_source_reasons or {}).get(
            (package_path.casefold(), export_index),
        )
        if (
            location_type == "WORLD_LOOT_UNRESOLVED"
            and actor_class in KNOWN_PICKUP_ACTOR_CATEGORIES
            and not any(reward["scanner_flag"] or reward["scanner_scenario"] for reward in rewards)
        ):
            location_type = KNOWN_PICKUP_ACTOR_CATEGORIES[actor_class]
        exception_key = (package_path.casefold(), export_index)
        explicit_exception_mode = scripted_reward_modes.get(exception_key)
        exception_mode = explicit_exception_mode
        if (
            exception_mode is None
            and len(rewards) == 1
            and rewards[0]["article_path"].casefold()
            in {value.casefold() for value in FLAG_PICKUP_ARTICLE_ONLY_ALLOWLIST}
            and rewards[0]["progression_reason"].casefold() == "flag_pickup"
            and rewards[0]["scanner_flag"]
            and not rewards[0]["scanner_scenario"]
            and location_type in {"GROUND_PICKUP", "TREASURE_BOX"}
            and destination_kind == "ARTICLE_BULK"
            and actor_class in {
                "BP_PickupItem_C",
                "BP_PickupItem_WithFlagChange_C",
                "BP_it00_001_A_0_C",
                "BP_it00_002_A_0_C",
            }
            and rewards[0]["soft_object_path_identity"] is not None
            and explicit_protection is None
            and map_file.casefold() not in {item.casefold() for item in unsafe_packages}
            and not (
                important_exclusions is not None
                and rewards[0]["article_path"].casefold()
                in important_exclusions.key_identities | important_exclusions.quest_flag_identities
            )
        ):
            exception_mode = SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS
        scripted_reward_mode = None
        if exception_mode is not None:
            provisional = WorldLootLocation(
                package_path=package_path,
                source_file=map_file,
                export_index=export_index,
                export_name=export_name,
                actor_class=actor_class,
                actor_category=location_type,
                save_id=rewards[0]["unique_save_id"],
                protected=False,
                protection_reasons=(),
                rewards=tuple(RewardPayload(
                    reward["article_path"], reward["count"], reward["notification"],
                    soft_object_path_identity=_soft_object_path_identity_from_dict(
                        reward["soft_object_path_identity"],
                    ),
                ) for reward in rewards),
                destination_kind=destination_kind,
                mirror_of_package_path=next(iter(mirror_packages)),
                mirror_of_export_index=next(iter(mirror_indices)),
                scripted_reward_mode=exception_mode,
            )
            if not is_valid_scripted_reward_only_location(provisional):
                raise CatalogImportError(
                    f"Scripted reward-only exception does not match an exact audited pickup: "
                    f"{map_file}:{export_index}."
                )
            if explicit_exception_mode == SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS and (
                len(rewards) != 1
                or rewards[0]["reward_index"] != 0
                or rewards[0]["soft_object_path_identity"] != {
                    "package_name": {
                        "value": "/Game/Blueprint/Article/Jail/DA_Jail_Stinger",
                        "number": 0,
                    },
                    "asset_name": {"value": "DA_Jail_Stinger", "number": 0},
                }
                or rewards[0]["progression_reason"].casefold() != "flag_pickup"
                or rewards[0]["scanner_scenario"]
            ):
                raise CatalogImportError("Scripted Jail exception does not match the audited Stinger pickup and flag-only metadata.")
            if explicit_exception_mode == SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO and (
                len(rewards) != 1
                or rewards[0]["reward_index"] != 0
                or rewards[0]["scanner_flag"]
                or rewards[0]["scanner_scenario"] != "BP_PickupItem_WithSmallScenario"
                or rewards[0]["progression_reason"].casefold() != "scenario_pickup"
            ):
                raise CatalogImportError("Scripted scenario exception does not match the audited Report pickup metadata.")
            scripted_reward_mode = exception_mode
            if explicit_exception_mode is not None:
                seen_scripted_reward_slots.add(exception_key)
        reasons: set[str] = set()
        if location_type not in {"GROUND_PICKUP", "TREASURE_BOX"}:
            reasons.add("unresolved location type")
        if map_file.casefold() in {item.casefold() for item in unsafe_packages}:
            reasons.add("unsafe or unparsed package")
        if "withflagchange" in export_name.casefold() and scripted_reward_mode is None:
            reasons.add("flag-changing pickup")
        if (
            "withsmallscenario" in export_name.casefold()
            and scripted_reward_mode != SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO
        ):
            reasons.add("scenario pickup")
        if any(reward["scanner_flag"] for reward in rewards) and scripted_reward_mode is None:
            reasons.add("flag-associated pickup")
        if (
            any(reward["scanner_scenario"] for reward in rewards)
            and scripted_reward_mode != SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO
        ):
            reasons.add("scenario-associated pickup")
        if explicit_protection:
            reasons.update(explicit_protection)
        reward_paths = {reward["article_path"].casefold() for reward in rewards}
        if important_exclusions is not None:
            if reward_paths & important_exclusions.key_identities:
                reasons.add("KEY_EXCLUDED")
            if reward_paths & important_exclusions.quest_flag_identities:
                reasons.add("QUEST_FLAG_EXCLUDED")
        for reward in rewards:
            if reward["progression_risk"] in {"HIGH", "REVIEW"}:
                progression_reason = reward["progression_reason"]
                if scripted_reward_mode is not None and progression_reason.casefold() == "flag_pickup":
                    continue
                if (
                    scripted_reward_mode == SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_SCENARIO
                    and progression_reason.casefold() == "scenario_pickup"
                ):
                    continue
                # Keyword labels are discovery hints, not proof that a physical
                # pickup is progression-critical. Protection must come from an
                # exact key/quest exclusion or a structural/scripted signal.
                if not progression_reason.casefold().startswith("keyword:"):
                    reasons.add(progression_reason or f"progression risk {reward['progression_risk'].casefold()}")

        if any(reward["soft_object_path_identity"] is None for reward in rewards) and not reasons:
            raise CatalogImportError(
                f"World Loot reward without serialized FName identity must be explicitly protected: "
                f"{map_file}:{export_index}."
            )

        locations.append(
            {
                "package_path": package_path,
                "source_file": map_file,
                "export_index": export_index,
                "export_name": export_name,
                "actor_class": actor_class,
                "actor_category": location_type,
                "save_id": rewards[0]["unique_save_id"],
                "content_class": rewards[0]["where_content_class"],
                "protected": bool(reasons),
                "protection_reasons": sorted(reasons, key=str.casefold),
                "destination_kind": destination_kind,
                **({"scripted_reward_mode": scripted_reward_mode} if scripted_reward_mode is not None else {}),
                "destination_id": (
                    f"{package_path}#{export_index}" if instance_override_source is not None else None
                ),
                "display_name": (
                    f"World pickup — {export_name}" if instance_override_source is not None else None
                ),
                "structural_class_package": (
                    instance_override_source["class_package"] if instance_override_source else None
                ),
                "instance_override_source": instance_override_source,
                **({"location_type": "NORMAL_WORLD_LOOT"} if instance_override_source is not None else {}),
                "rewards": [
                    {
                        "article_path": reward["article_path"],
                        "count": reward["count"],
                        "notification": reward["notification"],
                        "content_class": reward["what_content_class"],
                        "soft_object_path_identity": reward["soft_object_path_identity"],
                    }
                    for reward in rewards
                ],
                **({
                    "mirror_of_package_path": next(iter(mirror_packages)),
                    "mirror_of_export_index": next(iter(mirror_indices)),
                } if None not in mirror_packages else {}),
            }
        )

    # A mirrored DLC copy is the same logical pickup. If map-specific safety or
    # curated protection applies to either physical export, preserve the more
    # restrictive policy on both copies; never let a mirror weaken protection.
    locations_by_slot = {
        (str(entry["package_path"]).casefold(), int(entry["export_index"])): entry
        for entry in locations
    }
    for mirror in locations:
        mirror_package = mirror.get("mirror_of_package_path")
        mirror_index = mirror.get("mirror_of_export_index")
        if not isinstance(mirror_package, str) or type(mirror_index) is not int:
            continue
        target = locations_by_slot.get((mirror_package.casefold(), mirror_index))
        if target is None:
            continue
        reasons = sorted(
            set(target["protection_reasons"]) | set(mirror["protection_reasons"]),
            key=str.casefold,
        )
        target["protection_reasons"] = reasons
        mirror["protection_reasons"] = reasons
        target["protected"] = mirror["protected"] = bool(reasons)

    if seen_scripted_reward_slots != set(scripted_reward_modes):
        missing = sorted(set(scripted_reward_modes) - seen_scripted_reward_slots)
        unexpected = sorted(seen_scripted_reward_slots - set(scripted_reward_modes))
        raise CatalogImportError(
            f"Curated scripted reward policies did not match source slots (missing={missing[:3]!r}, unexpected={unexpected[:3]!r})."
        )

    return {"locations": locations}


# Resolve the NameMap FName
def _resolve_fname(value: str, number: int) -> str:
    """Resolve Unreal's serialized FName literal/number without suffix guessing."""
    return value if number == 0 else f"{value}_{number - 1}"


# Import pickup types
def import_pickup_types(source: Path) -> dict[str, Any]:
    rows = read_csv(source, {"file", *PICKUP_COLUMNS})
    maps: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for row in rows:
        file_name = row["file"].strip()
        if not file_name.casefold().endswith(".umap") or Path(file_name).name != file_name:
            raise CatalogImportError(f"Invalid map filename in pickup_types.csv: {file_name!r}.")
        if file_name.casefold() in seen_files:
            raise CatalogImportError(f"Duplicate map type record: {file_name}.")
        seen_files.add(file_name.casefold())
        maps.append(
            {"file": file_name, "actor_types": {column.casefold(): row[column].strip() or None for column in PICKUP_COLUMNS}}
        )
    maps.sort(key=lambda entry: entry["file"].casefold())
    return {"maps": maps}


# Import materials
def import_materials(source: Path) -> dict[str, Any]:
    """Import only resolved, complete material instances from the safe CSV."""
    required = {
        "map_file", "export_index", "actor_name", "resource_class_package",
        "resource_class_name", "unique_save_id", "item_path", "count",
        "bNotification", "class_asset_status",
    }
    rows = read_csv(source, required)
    locations: list[dict[str, Any]] = []
    save_ids: set[str] = set()
    skipped_rows = 0

    for row_number, row in enumerate(rows, start=2):
        # Only explicitly resolved rows enter the pool. Excluded, unresolved, and
        # other scan statuses are ignored without consulting another materials source.
        if row["class_asset_status"].strip().casefold() != "resolved":
            skipped_rows += 1
            continue

        map_file = row["map_file"].strip()
        if (
            not map_file.casefold().endswith(".umap")
            or Path(map_file).name != map_file
            or "/" in map_file
            or "\\" in map_file
        ):
            raise CatalogImportError(f"Invalid generated-map filename in materials row {row_number}: {map_file!r}.")

        export_index = parse_int(row["export_index"], "material export_index")
        actor_name = row["actor_name"].strip()
        if not actor_name or "_UAID_" not in actor_name:
            raise CatalogImportError(f"Invalid material actor identity in row {row_number}.")
        actor_class = actor_name.split("_UAID_", maxsplit=1)[0]

        class_package = row["resource_class_package"].strip()
        class_name = row["resource_class_name"].strip()
        if not class_package.startswith("/Game/") or not class_name.endswith("_C"):
            raise CatalogImportError(f"Invalid renewable resource class in materials row {row_number}.")

        save_id = row["unique_save_id"].strip()
        if not save_id:
            raise CatalogImportError(f"Material UniqueSaveID is missing in row {row_number}.")
        if save_id.casefold() in save_ids:
            raise CatalogImportError(f"Duplicate material UniqueSaveID in row {row_number}: {save_id}.")
        save_ids.add(save_id.casefold())

        reward_path = row["item_path"].strip()
        if not reward_path.startswith("/Game/"):
            raise CatalogImportError(f"Invalid material reward path in row {row_number}: {reward_path!r}.")
        try:
            count = Decimal(row["count"].strip())
        except (InvalidOperation, AttributeError) as error:
            raise CatalogImportError(f"Material count must equal 1 in row {row_number}.") from error
        if count != Decimal(1):
            raise CatalogImportError(f"Material count must equal 1 in row {row_number}.")
        notification = parse_bool(row["bNotification"], "material bNotification")
        if not notification:
            raise CatalogImportError(f"Material bNotification must be True in row {row_number}.")

        locations.append(
            {
                "package_path": MATERIAL_MAP_PREFIX + Path(map_file).stem,
                "source_file": map_file,
                "export_index": export_index,
                "export_name": actor_name,
                "actor_class": actor_class,
                "resource_class_package": class_package,
                "resource_class_name": class_name,
                "where_content_class": parse_content_class(
                    row.get("where_content_class"), "material WHERE content class",
                ),
                "resource_content_class": parse_content_class(
                    row.get("resource_content_class"), "material resource content class",
                ),
                "reward_content_class": parse_content_class(
                    row.get("reward_content_class"), "material reward content class",
                ),
                "save_id": save_id,
                "protected": False,
                "protection_reason": None,
                "reward": {
                    "article_path": reward_path,
                    "count": int(count),
                    "notification": notification,
                    "content_class": parse_content_class(
                        row.get("article_content_class"), "material Article content class",
                    ),
                },
            }
        )

    counts = {
        "locations": len(locations),
        "resource_classes": len(
            {(entry["resource_class_package"], entry["resource_class_name"]) for entry in locations}
        ),
        "rewards": len({entry["reward"]["article_path"] for entry in locations}),
    }
    if counts != MATERIAL_COUNTS:
        raise CatalogImportError(
            "Material catalog counts do not match the reviewed safe set: "
            f"expected {MATERIAL_COUNTS}, got {counts}."
        )

    locations.sort(key=lambda entry: (entry["source_file"].casefold(), entry["export_index"]))
    return {
        "available": True,
        "locations": locations,
        "source_rows": len(rows),
        "excluded_rows": skipped_rows,
    }


# Import shops
def import_shops(
    source: Path,
    important_exclusions: ImportantItemExclusions | None = None,
) -> dict[str, Any]:
    required = {
        "table", "shop_row", "item_path", "item_object", "price", "uses_stock",
        "stock", "is_important_locked",
    }
    rows = read_csv(source, required)
    products: list[dict[str, Any]] = []
    next_index: dict[tuple[str, str], int] = defaultdict(int)
    seen_slots: set[tuple[str, str, int]] = set()
    for row in rows:
        source_family = row["table"].strip().upper()
        if source_family not in SHOP_FAMILIES:
            raise CatalogImportError(f"Unknown shop table family: {source_family!r}.")
        family = SHOP_FAMILIES[source_family]
        shop_row = row["shop_row"].strip()
        article_path = row["item_path"].strip()
        if not shop_row or not article_path.startswith("/Game/"):
            raise CatalogImportError("Shop row and item_path must be present and valid.")
        index_key = (family, shop_row)
        product_index = next_index[index_key]
        next_index[index_key] += 1
        slot_key = (family, shop_row, product_index)
        if slot_key in seen_slots:
            raise CatalogImportError(f"Duplicate shop slot: {slot_key}.")
        seen_slots.add(slot_key)
        is_important = parse_bool(row["is_important_locked"], "is_important_locked")
        important_path = "/item/important/" in article_path.casefold()
        if important_path and not is_important:
            raise CatalogImportError(f"Important shop item is not locked: {article_path}.")
        normalized_path = article_path.casefold()
        if family == "NORMAL" and shop_row == "Shop_Jadwiga_Item":
            # The Magmell Tool Shop is an explicit gameplay-tested safety boundary.
            # It stays protected independently of its serialized stock fields.
            protected = True
            protection_reason = "MAGMELL_TOOL_SHOP"
        elif important_exclusions is not None and normalized_path in important_exclusions.key_identities:
            protected = True
            protection_reason = "KEY_EXCLUDED"
        elif important_exclusions is not None and normalized_path in important_exclusions.quest_flag_identities:
            protected = True
            protection_reason = "QUEST_FLAG_EXCLUDED"
        elif is_important and not important_path:
            # Preserve any source lock that is not the legacy Important blanket.
            protected = True
            protection_reason = "catalogued shop safety lock"
        else:
            # is_important_locked in the research CSV records the retired blanket
            # policy for Important paths. Actual product identity is the reward.
            protected = False
            protection_reason = None
        products.append(
            {
                "family": family,
                "shop_row": shop_row,
                "product_index": product_index,
                "original_product_path": article_path,
                "item_object": row["item_object"].strip(),
                "price": parse_int(row["price"], "price"),
                "uses_stock": parse_bool(row["uses_stock"], "uses_stock"),
                "stock": parse_int(row["stock"], "stock"),
                "protected": protected,
                "protection_reason": protection_reason,
                "what_content_class": parse_content_class(
                    row.get("what_content_class"), "shop WHAT content class",
                ),
            }
        )
    return {"products": products}


# Validate shop summary
def validate_shop_summary(
    source: Path,
    products: list[dict[str, Any]],
    source_catalog: Path | None = None,
) -> None:
    """Cross-check imported slot counts against the supplied table summary."""
    summary = read_csv(
        source,
        {
            "table", "rows", "product_slots", "unique_items",
            "important_locked_slots", "randomizable_slots_v1", "row_names",
        },
    )
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in products:
        by_family[product["family"]].append(product)

    source_locked_by_family: dict[str, int] = defaultdict(int)
    if source_catalog is not None:
        source_rows = read_csv(source_catalog, {"table", "is_important_locked"})
        for row in source_rows:
            source_family = row["table"].strip().upper()
            if source_family not in SHOP_FAMILIES:
                raise CatalogImportError(f"Unknown shop table family: {source_family!r}.")
            if parse_bool(row["is_important_locked"], "is_important_locked"):
                source_locked_by_family[SHOP_FAMILIES[source_family]] += 1

    seen_families: set[str] = set()
    for summary_row in summary:
        source_family = summary_row["table"].strip().upper()
        if source_family not in SHOP_FAMILIES:
            raise CatalogImportError(f"Unknown family in shop summary: {source_family!r}.")
        family = SHOP_FAMILIES[source_family]
        if family in seen_families:
            raise CatalogImportError(f"Duplicate family in shop summary: {family}.")
        seen_families.add(family)
        family_products = by_family.get(family, [])
        # The supplied summary describes the legacy research classification;
        # runtime eligibility is recomputed from exact exclusions above.
        locked = source_locked_by_family[family] if source_catalog is not None else sum(
            product["protected"] for product in family_products
        )
        actual = {
            "rows": len({product["shop_row"] for product in family_products}),
            "product_slots": len(family_products),
            "unique_items": len({product["original_product_path"] for product in family_products}),
            "important_locked_slots": locked,
            "randomizable_slots_v1": len(family_products) - locked,
        }
        for field, value in actual.items():
            expected = parse_int(summary_row[field], f"shop summary {family}.{field}")
            if value != expected:
                raise CatalogImportError(
                    f"Shop summary mismatch for {family}.{field}: expected {expected}, got {value}."
                )
        expected_rows = {
            name.strip() for name in summary_row["row_names"].split(" | ") if name.strip()
        }
        actual_rows = {product["shop_row"] for product in family_products}
        if expected_rows != actual_rows:
            raise CatalogImportError(f"Shop row names do not match the summary for {family}.")
    if seen_families != set(by_family):
        raise CatalogImportError("Shop catalog and summary contain different table families.")


# Count stable JSON bytes
def stable_json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


# Import catalogs
def import_catalogs(
    source_dir: Path,
    exclusions_path: Path,
    output_dir: Path,
    *,
    important_exclusions_path: Path | None = None,
) -> dict[str, Any]:
    unsafe_packages = read_unsafe_packages(exclusions_path)
    important_exclusions_path = important_exclusions_path or (
        PROJECT_ROOT / "data" / "reward_pools" / output_dir.name / "important_item_exclusions.json"
    )
    try:
        important_exclusions = ImportantItemExclusionStore(important_exclusions_path.parent).load(output_dir.name)
    except (ImportantItemExclusionError, OSError, ValueError) as error:
        raise CatalogImportError(f"Important Item exclusions could not be validated: {error}") from error
    shop_source = source_dir / "shop_catalog_all_tables.csv"
    shop_catalog = import_shops(shop_source, important_exclusions)
    validate_shop_summary(source_dir / "shop_tables_summary.csv", shop_catalog["products"], shop_source)
    protected_source_reasons = read_explicit_world_source_protections(
        PROJECT_ROOT / "data" / "jails" / output_dir.name / "jails.json",
    )
    scripted_reward_modes = read_explicit_scripted_reward_modes(
        PROJECT_ROOT / "data" / "jails" / output_dir.name / "jails.json",
    )
    imported_world = import_world_loot(
        source_dir / "world_loot_catalog.csv", set(unsafe_packages), important_exclusions,
        protected_source_reasons, scripted_reward_modes,
    )
    # The confirmed direct Article grants are curated records outside
    # world_loot_catalog.csv. Preserve them through the same importer instead of
    # losing supported destinations on a catalog rebuild.
    previous_world_path = output_dir / "world_loot.json"
    if previous_world_path.is_file():
        try:
            previous_world = json.loads(previous_world_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CatalogImportError(f"Could not preserve existing direct World destinations: {error}") from error
        direct_entries = [
            entry for entry in previous_world.get("locations", [])
            if isinstance(entry, dict) and entry.get("destination_kind") in {
                "RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT",
            }
        ] if isinstance(previous_world, dict) else []
        existing_slots = {
            (entry["package_path"].casefold(), entry["export_index"])
            for entry in imported_world["locations"]
        }
        for entry in direct_entries:
            entry.setdefault("content_class", "UNKNOWN")
            raw_rewards = entry.get("rewards")
            if isinstance(raw_rewards, list):
                for reward in raw_rewards:
                    if isinstance(reward, dict):
                        reward.setdefault("content_class", "UNKNOWN")
            slot = (str(entry.get("package_path", "")).casefold(), entry.get("export_index"))
            if not slot[0] or slot in existing_slots:
                raise CatalogImportError(f"Preserved direct World destination collides with imported slot: {slot!r}.")
            existing_slots.add(slot)
            imported_world["locations"].append(entry)
    cdo_defaults = import_world_loot_cdo_defaults(source_dir / "world_loot_cdo_catalog.csv")
    renewable_locations = import_renewable_ingredient_locations(
        source_dir / "world_loot_renewable_ingredients.csv",
    )
    healing_locations = import_healing_upgrade_locations(
        source_dir / "world_loot_healing_overrides.csv",
    )
    replaced_cdo_owners = {
        location["instance_override_source"]["cdo_package"].casefold()
        for location in healing_locations
    }
    replaced_cdo_owners.update(
        row["reward_owner_package"].casefold()
        for row in read_csv(
            source_dir / "world_loot_renewable_ingredients.csv",
            {"reward_owner_package"},
        )
    )
    imported_world["locations"].extend(
        location for location in cdo_defaults
        if location["package_path"].casefold() not in replaced_cdo_owners
    )
    imported_world["locations"].extend(renewable_locations)
    imported_world["locations"].extend(healing_locations)
    imported_world["unsupported_cdo_source_references"] = import_world_loot_unsupported_cdo_sources(
        source_dir / "world_loot_unsupported_cdo_sources.csv",
    )
    imported_world["locations"].sort(key=lambda entry: (
        entry["package_path"].casefold(), entry["export_index"],
    ))
    catalogs = {
        "world_loot.json": imported_world,
        "pickup_types.json": import_pickup_types(source_dir / "pickup_types.csv"),
        "shops.json": shop_catalog,
        "materials.json": import_materials(source_dir / "materials_catalog.csv"),
        "exclusions.json": {"packages": unsafe_packages},
    }
    article_index = build_explicit_article_class_index(PROJECT_ROOT / "data")
    if article_index.conflicts:
        conflict_summary = "; ".join(
            f"{path}: {', '.join(content_class for content_class, _sources in classes)}"
            for path, classes in article_index.conflicts
        )
        raise CatalogImportError(f"Explicit Article classification sources conflict: {conflict_summary}")
    applied_classifications = apply_explicit_article_classes(
        catalogs["world_loot.json"],
        catalogs["materials.json"],
        catalogs["shops.json"],
        article_index,
    )
    catalog_revision = output_dir.name
    metadata: dict[str, Any] = {}
    for filename, document in catalogs.items():
        payload = stable_json_bytes(document)
        count_key = {
            "world_loot.json": "locations",
            "pickup_types.json": "maps",
            "shops.json": "products",
            "materials.json": "locations",
            "exclusions.json": "packages",
        }[filename]
        count = len(document[count_key])
        metadata[filename] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "count": count,
        }
        document["catalog_revision"] = catalog_revision
        catalogs[filename] = document
        payload = stable_json_bytes(document)
        metadata[filename]["sha256"] = hashlib.sha256(payload).hexdigest()

    source_names = (
        "world_loot_catalog.csv",
        "world_loot_cdo_catalog.csv",
        "world_loot_unsupported_cdo_sources.csv",
        "world_loot_renewable_ingredients.csv",
        "world_loot_healing_overrides.csv",
        "pickup_types.csv",
        "materials_catalog.csv",
        "shop_catalog_all_tables.csv",
        "shop_tables_summary.csv",
    )
    source_hashes = {
        filename: hashlib.sha256((source_dir / filename).read_bytes()).hexdigest()
        for filename in source_names
    }
    source_hashes[exclusions_path.name] = hashlib.sha256(exclusions_path.read_bytes()).hexdigest()
    source_hashes[important_exclusions_path.name] = hashlib.sha256(important_exclusions_path.read_bytes()).hexdigest()

    manifest = {
        "catalog_revision": catalog_revision,
        "files": metadata,
        "source_sha256": source_hashes,
        "content_classification": {
            "explicit_article_identities": len(article_index.entries),
            "explicit_article_classes": article_class_counts(article_index),
            "applied_to_runtime_catalogs": applied_classifications,
            "conflicting_explicit_article_identities": len(article_index.conflicts),
            "unmatched_unknowns_are_metadata_only": True,
        },
        "features": {
            "world_loot": True,
            "materials": bool(catalogs["materials.json"]["locations"]),
            "shops": True,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, document in catalogs.items():
        write_json(output_dir / filename, document)
    write_json(output_dir / "manifest.json", manifest)
    return manifest


# Import renewable ingredient locations
def import_renewable_ingredient_locations(source: Path) -> list[dict[str, Any]]:
    """Create one locked catalog record per structurally renewable physical actor."""
    required = {
        "location_id", "generated_package", "export_index", "actor_name", "actor_class",
        "blueprint_class_package", "effective_reset_on_yadorigi_rest", "reward_owner_package",
        "article_path", "count", "bNotification", "classification", "exclusion_reason",
        "parent_blueprint_chain",
    }
    locations: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for row_number, row in enumerate(read_csv(source, required), start=2):
        package = row["generated_package"].strip()
        location_id = row["location_id"].strip()
        try:
            export_index = parse_int(location_id.rsplit("#", 1)[1], "renewable physical export index", minimum=1)
        except (IndexError, CatalogImportError) as error:
            raise CatalogImportError(f"Renewable location id is not a physical actor identity on row {row_number}.") from error
        class_package = row["blueprint_class_package"].strip()
        article_path = row["article_path"].strip()
        if (
            not location_id or not is_world_map_package(package)
            or location_id != f"{package}#{export_index}"
            or not class_package.startswith("/Game/Environment/Item/")
            or not article_path.startswith("/Game/")
            or row["effective_reset_on_yadorigi_rest"].strip().casefold() != "true"
            or row["classification"].strip() != "RENEWABLE_INGREDIENT"
            or row["exclusion_reason"].strip() != "RENEWABLE_INGREDIENT_DISABLED"
        ):
            raise CatalogImportError(f"Renewable ingredient evidence is incomplete on row {row_number}.")
        key = (package.casefold(), export_index)
        if key in seen:
            raise CatalogImportError(f"Duplicate renewable physical actor: {location_id}.")
        seen.add(key)
        chain = json.loads(row["parent_blueprint_chain"])
        if not isinstance(chain, list) or not chain or any(not isinstance(item, str) for item in chain):
            raise CatalogImportError(f"Renewable Blueprint ancestry is invalid on row {row_number}.")
        package_literal = (row.get("article_package_literal_fname") or "").strip()
        asset_literal = (row.get("article_asset_literal_fname") or "").strip()
        package_number_text = (row.get("article_package_fname_number") or "").strip()
        asset_number_text = (row.get("article_asset_fname_number") or "").strip()
        fname_values = (package_literal, package_number_text, asset_literal, asset_number_text)
        soft_object_path_identity = None
        if any(fname_values):
            if not all(fname_values):
                raise CatalogImportError(f"Renewable Article FName identity is incomplete on row {row_number}.")
            package_number = parse_int(package_number_text, "renewable Article package FName number")
            asset_number = parse_int(asset_number_text, "renewable Article asset FName number")
            if (
                _resolve_fname(package_literal, package_number) != article_path
                or _resolve_fname(asset_literal, asset_number) != article_path.rsplit("/", 1)[-1]
            ):
                raise CatalogImportError(f"Renewable Article FName identity does not resolve on row {row_number}.")
            soft_object_path_identity = {
                "package_name": {"value": package_literal, "number": package_number},
                "asset_name": {"value": asset_literal, "number": asset_number},
            }
        mirror_package = (row.get("mirror_of_package_path") or "").strip()
        mirror_index_text = (row.get("mirror_of_export_index") or "").strip()
        if bool(mirror_package) != bool(mirror_index_text):
            raise CatalogImportError(f"Renewable mirror identity is incomplete on row {row_number}.")
        mirror = ({
            "mirror_of_package_path": mirror_package,
            "mirror_of_export_index": parse_int(mirror_index_text, "renewable mirror export index", minimum=1),
        } if mirror_package else {})
        locations.append({
            "package_path": package,
            "source_file": package.removeprefix("/Game/") + ".umap",
            "export_index": export_index,
            "export_name": row["actor_name"].strip(),
            "actor_class": row["actor_class"].strip(),
            "actor_category": "GROUND_PICKUP",
            "save_id": (row.get("unique_save_id") or "").strip() or None,
            "protected": True,
            "protection_reasons": ["RENEWABLE_INGREDIENT_DISABLED"],
            "rewards": [{
                "article_path": article_path,
                "count": parse_int(row["count"], "renewable count", minimum=1),
                "notification": parse_bool(row["bNotification"], "renewable bNotification"),
                "soft_object_path_identity": soft_object_path_identity,
            }],
            "destination_kind": "RENEWABLE_INGREDIENT",
            "destination_id": location_id,
            "display_name": f"Renewable ingredient — {row['actor_name'].strip()}",
            "content_class": "BASE",
            "physical_instance_count": 1,
            "parent_blueprint_package": chain[0],
            "parent_blueprint_packages": chain,
            "location_type": "RENEWABLE_INGREDIENT",
            "structural_class_package": class_package,
            "effective_reset_on_yadorigi_rest": True,
            "reward_owner_package": row["reward_owner_package"].strip(),
            **mirror,
        })
    return locations


# Import healing upgrade locations
def import_healing_upgrade_locations(source: Path) -> list[dict[str, Any]]:
    """Import Golden Seed/Bead physical actors with exact shared-CDO provenance."""
    required = {
        "location_id", "generated_package", "export_index", "actor_name", "actor_class",
        "blueprint_class_package", "parent_blueprint_packages", "shared_cdo_package",
        "shared_cdo_export_index", "shared_cdo_export_name", "article_path",
        "article_package_literal", "article_package_number", "article_asset_literal",
        "article_asset_number", "count", "bNotification", "location_type",
    }
    locations: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for row_number, row in enumerate(read_csv(source, required), start=2):
        package = row["generated_package"].strip()
        location_id = row["location_id"].strip()
        export_index = parse_int(row["export_index"], "healing upgrade export_index", minimum=1)
        try:
            physical_export_index = parse_int(location_id.rsplit("#", 1)[1], "healing physical export index", minimum=1)
        except (IndexError, CatalogImportError) as error:
            raise CatalogImportError(f"Healing location id is not a physical actor identity on row {row_number}.") from error
        if physical_export_index != export_index:
            raise CatalogImportError(f"Healing physical export index disagrees with its location id on row {row_number}.")
        article_path = row["article_path"].strip()
        package_literal = row["article_package_literal"].strip()
        asset_literal = row["article_asset_literal"].strip()
        package_number = parse_int(row["article_package_number"], "Article package FName number")
        asset_number = parse_int(row["article_asset_number"], "Article asset FName number")
        resolve = lambda value, number: value if number == 0 else f"{value}_{number - 1}"
        if (
            not location_id or not is_world_map_package(package)
            or location_id != f"{package}#{export_index}"
            or not row["shared_cdo_package"].strip().startswith("/Game/Environment/Item/")
            or not row["blueprint_class_package"].strip().startswith("/Game/Environment/Item/")
            or resolve(package_literal, package_number) != article_path
            or resolve(asset_literal, asset_number) != article_path.rsplit("/", 1)[-1]
        ):
            raise CatalogImportError(f"Healing upgrade identity is invalid on row {row_number}.")
        expected_type = {
            "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryCountUp_1": "HEALING_UPGRADE_GOLDEN_SEED",
            "/Game/Blueprint/Article/Item/Material/DA_Item_MatBasicRecoveryAmountUp_1": "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
        }.get(article_path)
        if expected_type != row["location_type"].strip():
            raise CatalogImportError(f"Unexpected healing Article or location type on row {row_number}.")
        key = (package.casefold(), export_index)
        if key in seen:
            raise CatalogImportError(f"Duplicate healing physical actor: {location_id}.")
        seen.add(key)
        parent_package = row["parent_blueprint_packages"].strip()
        parents = [parent_package] if parent_package.startswith("/Game/") else []
        if not parents:
            raise CatalogImportError(f"Healing Blueprint ancestry is invalid on row {row_number}.")
        mirror_package = (row.get("mirror_of_package_path") or "").strip()
        mirror_index_text = (row.get("mirror_of_export_index") or "").strip()
        if bool(mirror_package) != bool(mirror_index_text):
            raise CatalogImportError(f"Healing mirror identity is incomplete on row {row_number}.")
        mirror = ({
            "mirror_of_package_path": mirror_package,
            "mirror_of_export_index": parse_int(mirror_index_text, "healing mirror export index", minimum=1),
        } if mirror_package else {})
        locations.append({
            "package_path": package,
            "source_file": package.removeprefix("/Game/") + ".umap",
            "export_index": export_index,
            "export_name": row["actor_name"].strip(),
            "actor_class": row["actor_class"].strip(),
            "actor_category": "GROUND_PICKUP",
            "save_id": (row.get("unique_save_id") or "").strip() or None,
            "protected": False,
            "protection_reasons": [],
            "rewards": [{
                "article_path": article_path,
                "content_class": "BASE",
                "count": parse_int(row["count"], "healing upgrade count", minimum=1),
                "notification": parse_bool(row["bNotification"], "healing upgrade bNotification"),
                "soft_object_path_identity": {
                    "package_name": {"value": package_literal, "number": package_number},
                    "asset_name": {"value": asset_literal, "number": asset_number},
                },
            }],
            "destination_kind": "INSTANCE_ARTICLE_BULK_OVERRIDE",
            "destination_id": location_id,
            "display_name": ("Golden Seed" if expected_type.endswith("SEED") else "Golden Blood Bead") + f" — {location_id}",
            "content_class": "BASE",
            "physical_instance_count": 1,
            "parent_blueprint_package": parents[0],
            "parent_blueprint_packages": parents,
            "location_type": expected_type,
            "structural_class_package": row["blueprint_class_package"].strip(),
            "instance_override_source": {
                "cdo_package": row["shared_cdo_package"].strip(),
                "cdo_export_index": parse_int(row["shared_cdo_export_index"], "healing CDO export index", minimum=1),
                "cdo_export_name": row["shared_cdo_export_name"].strip(),
                "class_package": row["blueprint_class_package"].strip(),
                "parent_blueprint_packages": parents,
            },
            **mirror,
        })
    return locations


# Import world loot CDO defaults
def import_world_loot_cdo_defaults(source: Path) -> list[dict[str, Any]]:
    """Import one patchable Blueprint CDO per reward owner, with exact FName identity."""
    required = {
        "blueprint_package", "blueprint_class", "cdo_export_index", "cdo_export_name",
        "parent_blueprint_package", "article_path", "article_package_literal",
        "article_package_number", "article_asset_literal", "article_asset_number",
        "parent_blueprint_packages", "count", "bNotification", "physical_instance_count",
        "unsupported_instance_count",
    }
    rows = read_csv(source, required)
    locations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        package = row["blueprint_package"].strip()
        if not package.startswith("/Game/Environment/Item/") or package.casefold() in seen:
            raise CatalogImportError(f"Invalid or duplicate World Loot CDO package on row {row_number}: {package!r}.")
        seen.add(package.casefold())
        class_name = row["blueprint_class"].strip()
        cdo_name = row["cdo_export_name"].strip()
        if not class_name.endswith("_C") or cdo_name != "Default__" + class_name:
            raise CatalogImportError(f"World Loot CDO export identity is inconsistent on row {row_number}.")
        export_index = parse_int(row["cdo_export_index"], "cdo_export_index", minimum=1)
        parent = row["parent_blueprint_package"].strip()
        if not parent.startswith("/Game/"):
            raise CatalogImportError(f"Invalid parent Blueprint package on World Loot CDO row {row_number}.")
        parent_packages = tuple(value.strip() for value in row["parent_blueprint_packages"].split("|") if value.strip())
        if (
            not parent_packages
            or parent_packages[0].casefold() != parent.casefold()
            or any(not value.startswith("/Game/") for value in parent_packages)
            or len({value.casefold() for value in parent_packages}) != len(parent_packages)
        ):
            raise CatalogImportError(f"Invalid World Loot CDO parent chain on row {row_number}.")
        article_path = row["article_path"].strip()
        package_literal = row["article_package_literal"].strip()
        asset_literal = row["article_asset_literal"].strip()
        package_number = parse_int(row["article_package_number"], "article_package_number")
        asset_number = parse_int(row["article_asset_number"], "article_asset_number")
        if not article_path.startswith("/Game/") or not package_literal.startswith("/Game/") or not asset_literal:
            raise CatalogImportError(f"Invalid World Loot CDO Article identity on row {row_number}.")
        resolved_package = package_literal if package_number == 0 else f"{package_literal}_{package_number - 1}"
        resolved_asset = asset_literal if asset_number == 0 else f"{asset_literal}_{asset_number - 1}"
        if resolved_package != article_path or resolved_asset != article_path.rsplit("/", 1)[-1]:
            raise CatalogImportError(f"World Loot CDO FName Number does not resolve to Article path on row {row_number}.")
        if parse_int(row["unsupported_instance_count"], "unsupported_instance_count") != 0:
            raise CatalogImportError(f"World Loot CDO catalog cannot include opaque physical instances on row {row_number}.")
        physical_count = parse_int(row["physical_instance_count"], "physical_instance_count", minimum=1)
        content_class = "DLC" if asset_literal.startswith("DA_Item_DLC_") else "BASE"
        locations.append({
            "package_path": package,
            "source_file": package.removeprefix("/Game/") + ".uasset",
            "export_index": export_index,
            "export_name": cdo_name,
            "actor_class": class_name,
            "actor_category": "GROUND_PICKUP",
            "save_id": None,
            "content_class": "BASE",
            "protected": False,
            "protection_reasons": [],
            "rewards": [{
                "article_path": article_path,
                "count": parse_int(row["count"], "count", minimum=1),
                "notification": parse_bool(row["bNotification"], "bNotification"),
                "content_class": content_class,
                "soft_object_path_identity": {
                    "package_name": {"value": package_literal, "number": package_number},
                    "asset_name": {"value": asset_literal, "number": asset_number},
                },
            }],
            "destination_kind": "BLUEPRINT_CDO_ARTICLE_BULK",
            "destination_id": "world_loot_cdo:" + package.casefold(),
            "display_name": f"World Loot Blueprint CDO - {class_name} ({physical_count} physical instances)",
            "physical_instance_count": physical_count,
            "parent_blueprint_package": parent,
            "parent_blueprint_packages": list(parent_packages),
        })
    return locations


# Import world loot unsupported CDO sources
def import_world_loot_unsupported_cdo_sources(source: Path) -> list[dict[str, Any]]:
    """Track Article references without making opaque CDO groups writable slots."""
    required = {
        "blueprint_package", "blueprint_class", "cdo_export_index", "cdo_export_name",
        "article_path", "article_package_literal", "article_package_number",
        "article_asset_literal", "article_asset_number", "count", "bNotification",
        "physical_instance_count", "unsupported_instance_count", "unsupported_reason",
    }
    rows = read_csv(source, required)
    references: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        package = row["blueprint_package"].strip()
        class_name = row["blueprint_class"].strip()
        cdo_name = row["cdo_export_name"].strip()
        if (
            not package.startswith("/Game/Environment/Item/")
            or package.casefold() in seen
            or not class_name.endswith("_C")
            or cdo_name != "Default__" + class_name
        ):
            raise CatalogImportError(f"Invalid or duplicate unsupported World Loot CDO identity on row {row_number}.")
        seen.add(package.casefold())
        export_index = parse_int(row["cdo_export_index"], "cdo_export_index", minimum=1)
        article_path = row["article_path"].strip()
        package_literal = row["article_package_literal"].strip()
        asset_literal = row["article_asset_literal"].strip()
        package_number = parse_int(row["article_package_number"], "article_package_number")
        asset_number = parse_int(row["article_asset_number"], "article_asset_number")
        resolved_package = package_literal if package_number == 0 else f"{package_literal}_{package_number - 1}"
        resolved_asset = asset_literal if asset_number == 0 else f"{asset_literal}_{asset_number - 1}"
        if (
            not article_path.startswith("/Game/")
            or not package_literal.startswith("/Game/")
            or not asset_literal
            or resolved_package != article_path
            or resolved_asset != article_path.rsplit("/", 1)[-1]
        ):
            raise CatalogImportError(f"Unsupported World Loot CDO FName identity does not resolve on row {row_number}.")
        physical_count = parse_int(row["physical_instance_count"], "physical_instance_count", minimum=1)
        unsupported_count = parse_int(row["unsupported_instance_count"], "unsupported_instance_count", minimum=1)
        reason = row["unsupported_reason"].strip()
        if unsupported_count != physical_count or not reason:
            raise CatalogImportError(f"Unsupported World Loot CDO does not explain every opaque instance on row {row_number}.")
        asset_class = "DLC" if asset_literal.startswith("DA_Item_DLC_") else "BASE"
        references.append({
            "blueprint_package": package,
            "blueprint_class": class_name,
            "cdo_export_index": export_index,
            "cdo_export_name": cdo_name,
            "physical_instance_count": physical_count,
            "unsupported_instance_count": unsupported_count,
            "reason": reason,
            "reward": {
                "article_path": article_path,
                "content_class": asset_class,
                "count": parse_int(row["count"], "count", minimum=1),
                "notification": parse_bool(row["bNotification"], "bNotification"),
                "soft_object_path_identity": {
                    "package_name": {"value": package_literal, "number": package_number},
                    "asset_name": {"value": asset_literal, "number": asset_number},
                },
            },
        })
    return references


# Write JSON
def write_json(path: Path, document: dict[str, Any]) -> None:
    path.write_bytes(stable_json_bytes(document))


# Start the application
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--exclusions", type=Path, default=DEFAULT_EXCLUSIONS)
    parser.add_argument(
        "--important-item-exclusions",
        type=Path,
        default=PROJECT_ROOT / "data" / "reward_pools" / "current" / "important_item_exclusions.json",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    arguments = parser.parse_args()
    try:
        manifest = import_catalogs(
            arguments.source_dir,
            arguments.exclusions,
            arguments.output_dir,
            important_exclusions_path=arguments.important_item_exclusions,
        )
    except (CatalogImportError, OSError) as error:
        parser.error(str(error))
    print(f"Wrote runtime catalogs for revision {manifest['catalog_revision']}:")
    for filename, metadata in manifest["files"].items():
        print(f"  {filename}: {metadata['count']} records, sha256 {metadata['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
