"""Deterministic inventory export built from the runtime randomizer catalogs.

This module is diagnostic-only. It reads the same checksum-validated catalogs
and planner eligibility helpers used by Generate; it never extracts or patches
game assets.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.bequeathed_formae_catalog import BequeathedFormaCatalogStore
from cv2randomizer.game.article_reward_catalog import CanonicalArticleRewardCatalogStore
from cv2randomizer.game.blood_code_catalog import BloodCodeCatalogStore
from cv2randomizer.game.booster_catalog import BoosterCatalogStore
from cv2randomizer.game.catalogs import CatalogStore, MaterialLocation, RewardPayload, RuntimeCatalogs, WorldLootLocation
from cv2randomizer.game.content_availability import PackageInventory, package_path_from_object_path
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalogStore
from cv2randomizer.game.enemy_drop_catalog import RANDOMIZED_CLASSIFICATIONS, EnemyDropCatalogStore
from cv2randomizer.game.extractor_catalog import ExtractorCatalogStore
from cv2randomizer.game.field_enemy_catalog import FieldEnemyCatalogStore
from cv2randomizer.game.fname_identity import SoftObjectPathIdentity
from cv2randomizer.game.enemy_drop_generation import ENEMY_DROP_TABLES
from cv2randomizer.game.jail_catalog import JailCatalogStore
from cv2randomizer.game.renewable_ingredients import renewable_material_locations
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES
from cv2randomizer.randomizer.initial_inventory import INITIAL_INVENTORY_PACKAGE
from cv2randomizer.randomizer.reward_reservations import RewardReservationSet
from cv2randomizer.randomizer.shops import is_npc_trader_row, shop_catalog_slot_lock_reason
from cv2randomizer.randomizer.world_loot import (
    WorldLootRandomizationPlan,
    build_world_loot_randomization_plan,
    filter_enemy_reward_world_locations,
)
from cv2randomizer.randomizer.article_rewards import canonical_article_world_candidates
from cv2randomizer.randomizer.defensive_gadgets import defensive_gadget_world_candidates
from cv2randomizer.randomizer.jails import jail_world_candidates
from cv2randomizer.randomizer.linked_jail_rewards import is_stinger_shop_slot


INVENTORY_COLUMNS = (
    "category", "subcategory", "system_owner", "status", "included_in_randomizer",
    "intrinsically_eligible", "enabled_by_current_config", "exclusion_reason", "exclusion_detail",
    "location_id", "source_type", "display_name", "article_name", "article_literal_fname",
    "article_fname_number", "resolved_article", "content_class", "base_dlc_after_ending_unknown",
    "map", "area", "package", "object_path", "actor", "actor_class", "reward_owner",
    "reward_index", "count", "stock", "drop_chance", "unlock_condition", "is_key_item",
    "is_quest_item", "is_progression_item", "is_scripted", "is_renewable", "is_one_time",
    "available_in_installed_game", "available_in_cache", "eligible_as_source",
    "eligible_as_destination", "randomizer_pool", "controlled_by_option", "notes",
    "quest_grant_audit",
)

STATUSES = ("INCLUDED", "EXCLUDED", "PROTECTED", "DISABLED", "UNSUPPORTED", "UNAVAILABLE")
_WORLD_TYPES = {"HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD"}
_ENEMY_LOCATION_PREFIXES = ("one_time_enemy_", "harmony_moon_", "qst_fieldenemy_")
_STARTING_FIXED_ITEMS = (
    ("DA_BodyLight", "/Game/Blueprint/Article/Item/DA_BodyLight"),
    ("DA_Item_BasicRecovery", "/Game/Blueprint/Article/Item/Recover/DA_Item_BasicRecovery_1"),
    ("DA_BloodCode_Lou_01", "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Lou_01"),
)
_STARTING_SLOTS = (
    ("starting_weapon", "Starting Weapon", "weapon", "INITIAL_INVENTORY_WEAPON"),
    ("starting_defensive_gadget", "Starting Defensive Gadget", "defensive_gadget", "INITIAL_INVENTORY_DEFENSIVE_GADGET"),
    ("starting_jail", "Starting Jail", "jail", "INITIAL_INVENTORY_JAIL"),
)


@dataclass(frozen=True, slots=True)
class InventoryExportResult:
    output_dir: Path
    files: tuple[Path, ...]
    total_rows: int
    status_counts: dict[str, int]
    category_counts: dict[str, dict[str, int]]
    validation: dict[str, bool]


# Export randomizer inventory
def export_randomizer_inventory(
    output_dir: Path,
    *,
    options: GenerationOptions | None = None,
    seed: str | int | None = None,
    catalog_revision: str = "current",
    cache_inventory: PackageInventory | None = None,
    unavailable_optional_sources: Iterable[str] = (),
) -> InventoryExportResult:
    """Write one row per known reward/source and a unique Article summary.

    A supplied ``cache_inventory`` represents the selected installation's
    validated extracted cache. When omitted, availability fields stay blank
    rather than implying that the installed game was inspected.
    """
    selected_options = options or GenerationOptions()
    normalized_seed: str | int = seed if seed is not None else 0
    catalogs = CatalogStore().load(catalog_revision)
    # Generate expands the reviewed legacy material subset to the full audited
    # renewable set. Keep the diagnostic inventory aligned with that live pool.
    catalogs = replace(catalogs, materials=renewable_material_locations(catalogs.world_loot))
    blood_codes = BloodCodeCatalogStore().load(catalog_revision)
    boosters = BoosterCatalogStore().load(catalog_revision)
    extractor = ExtractorCatalogStore().load(catalog_revision)
    canonical_articles = CanonicalArticleRewardCatalogStore().load(catalog_revision)
    enemy_catalog = EnemyDropCatalogStore().load(catalog_revision)
    field_enemy = FieldEnemyCatalogStore().load(
        catalog_revision, important_item_exclusions=catalogs.important_item_exclusions,
    )
    gadgets = DefensiveGadgetCatalogStore().load(catalog_revision)
    jails = JailCatalogStore().load()
    formae = BequeathedFormaCatalogStore().load()
    inventory = cache_inventory
    unavailable = {value.casefold() for value in unavailable_optional_sources}

    rows: list[dict[str, Any]] = []
    world_slot_rows: dict[tuple[str, int], dict[str, Any]] = {}
    material_by_slot = {
        (item.package_path.casefold(), item.export_index): item
        for item in catalogs.materials
    }
    one_time_context = _read_one_time_enemy_context()

    # These are ownership transfers, not Article-level reservations: Generate
    # replaces the source occurrence when a reward moves to another system.
    # Reserving the path here wrongly hides every matching location and made
    # the inventory summary disagree with the real planner.
    reservations = RewardReservationSet()

    available_catalog_world = tuple(
        location for location in catalogs.world_loot
        if inventory is None or _world_destination_available(location, inventory)
    )
    available_field_locations = (
        field_enemy.locations if inventory is None
        else field_enemy.eligible_locations(inventory)
    )
    field_group_available = len(available_field_locations) == len(field_enemy.locations)
    canonical_articles = _available_canonical_articles(canonical_articles, inventory)
    available_gadgets = _available_gadgets(gadgets, inventory)
    available_jails = _available_jails(jails, inventory)
    gadgets_for_world = replace(
        available_gadgets,
        items=tuple(
            item for item in available_gadgets.items
            if not reservations.contains("defensive_gadget", item.asset_path)
        ),
    )
    jails_for_world = replace(
        available_jails,
        items=tuple(item for item in available_jails.items if not reservations.contains("jail", item.asset_path)),
    )
    world_candidates = (
        *defensive_gadget_world_candidates(gadgets_for_world),
        *jail_world_candidates(jails_for_world, world_loot=catalogs.world_loot),
        *canonical_article_world_candidates(canonical_articles, include_boosters=False),
    )
    all_available_world = (*available_catalog_world, *available_field_locations)
    active_world_locations = filter_enemy_reward_world_locations(
        available_catalog_world,
        enabled=selected_options.randomize_enemy_drops,
    )
    if selected_options.randomize_enemy_drops:
        active_world_locations = (*active_world_locations, *available_field_locations)
    world_plan = _build_world_plan(
        active_world_locations, normalized_seed, selected_options, catalogs, field_enemy, inventory,
        world_candidates, reservations, include_healing=selected_options.randomize_healing,
    )
    eligibility_plan = _build_world_plan(
        all_available_world, normalized_seed, selected_options, catalogs, field_enemy, inventory,
        world_candidates, reservations, include_healing=True,
    )
    world_assignable = _assigned_world_slots(world_plan)
    world_sources = _world_source_slots(world_plan)
    locked_world = {
        _slot_key(item.location.package_path, item.location.export_index): item.reason
        for item in world_plan.locked_locations
    }
    world_unmatched = {
        _slot_key(item.package_path, item.export_index)
        for item in world_plan.unmatched_locations
    }
    eligibility_assignable = _assigned_world_slots(eligibility_plan)
    eligibility_locked = {
        _slot_key(item.location.package_path, item.location.export_index): item.reason
        for item in eligibility_plan.locked_locations
    }
    eligibility_unmatched = {
        _slot_key(item.package_path, item.export_index)
        for item in eligibility_plan.unmatched_locations
    }
    field_by_slot = {
        _slot_key(item.package_path, item.export_index): item
        for item in field_enemy.locations
    }
    direct_enemy_by_id = {
        str(row.get("location_id", "")).casefold(): row
        for row in one_time_context
        if row.get("location_id")
    }

    unavailable_what = {
        _slot_key(location.package_path, location.export_index)
        for location in world_plan.unavailable_what_locations
    }
    world_locations = (*catalogs.world_loot, *field_enemy.locations)
    for location in sorted(world_locations, key=_world_sort_key):
        is_field_enemy = _slot_key(location.package_path, location.export_index) in field_by_slot
        key = _slot_key(location.package_path, location.export_index)
        material = material_by_slot.get(key)
        metadata = direct_enemy_by_id.get((location.destination_id or "").casefold())
        category, subcategory = _world_category(location, is_field_enemy=is_field_enemy)
        control = _world_control(location, is_field_enemy=is_field_enemy)
        option_enabled = _option_enabled(selected_options, control)
        intrinsically_eligible = key in eligibility_assignable
        planner_reason = eligibility_locked.get(key) or locked_world.get(key)
        unmatched = key in eligibility_unmatched or key in world_unmatched
        status = "INCLUDED" if key in world_assignable else _world_exclusion_status(
            location, planner_reason, unmatched, catalogs,
        )
        reason, detail = _world_exclusion_reason(location, planner_reason, unmatched, catalogs)
        if is_field_enemy and inventory is not None and not field_group_available:
            status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The FieldEnemy direct-reward group requires all 24 Quest packages, and the validated extracted cache is incomplete."
            intrinsically_eligible = False
        elif inventory is not None and not _world_destination_available(location, inventory):
            status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The destination package or required structural dependency is absent from the validated extracted cache."
            intrinsically_eligible = False
        elif intrinsically_eligible and not option_enabled:
            status, reason, detail = "DISABLED", "OPTION_DISABLED", f"Feature control {control} is off in the selected configuration."
        elif status == "INCLUDED" and key not in world_assignable:
            status, reason, detail = "UNSUPPORTED", "UNSUPPORTED_REWARD_STRUCTURE", "The active production planner did not assign this location."
        if status == "INCLUDED":
            # The eligibility pass may find a lock/unmatched reason that does
            # not apply to the active pool. Never export an INCLUDED reward
            # with a stale exclusion reason from that separate diagnostic pass.
            reason, detail = "", ""
        for reward_index, reward in enumerate(location.rewards):
            row_location_id = location.destination_id or f"{location.package_path}#{location.export_index}"
            if len(location.rewards) > 1:
                row_location_id = f"{row_location_id}:reward:{reward_index}"
            quest_layout = (
                location.quest_article_layout if location.quest_article_layout is not None
                else next((layout for layout in location.quest_grant_layouts if layout.reward_index == reward_index), None)
            )
            row = _new_row(
                category=category,
                subcategory=subcategory,
                system_owner="WORLD_LOOT" if not is_field_enemy else "ENEMY_DROPS",
                status=status,
                intrinsically_eligible=intrinsically_eligible,
                enabled_by_current_config=option_enabled,
                exclusion_reason=reason,
                exclusion_detail=detail,
                location_id=row_location_id,
                source_type=location.location_type if not is_field_enemy else "FIELD_ENEMY_DIRECT_REWARD",
                display_name=location.display_name or "",
                article_name=reward.article_path.rsplit("/", 1)[-1],
                content_class=reward.content_class,
                map=location.package_path,
                area=(metadata.get("dungeon_area", "") if metadata else ""),
                package=location.package_path,
                object_path=f"{location.package_path}.{location.export_name}",
                actor=location.export_name,
                actor_class=location.actor_class,
                reward_owner=location.destination_id or "WORLD_LOOT",
                reward_index=quest_layout.reward_index if quest_layout is not None else reward_index,
                count=reward.count,
                unlock_condition="",
                is_scripted=bool(
                    location.scripted_reward_mode
                    or location.destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}
                    or "withflagchange" in location.actor_class.casefold()
                    or "withsmallscenario" in location.actor_class.casefold()
                ),
                is_renewable=_is_renewable_location(location),
                is_one_time=category == "ONE_TIME_ENEMY_REWARD",
                eligible_as_source=key in world_sources and key not in unavailable_what,
                eligible_as_destination=key in world_assignable,
                randomizer_pool=_world_pool(selected_options, location, is_field_enemy=is_field_enemy),
                controlled_by_option=control,
                notes=_world_notes(location, material, metadata, is_field_enemy),
            )
            _set_article_protection(row, reward.article_path, catalogs)
            if metadata:
                row["is_key_item"] = row["is_key_item"] or _csv_bool(metadata.get("key_item"))
                row["is_quest_item"] = row["is_quest_item"] or _csv_bool(metadata.get("quest_item"))
                row["is_progression_item"] = row["is_progression_item"] or row["is_key_item"] or row["is_quest_item"]
                if row["is_key_item"] or row["is_quest_item"]:
                    row["status"] = "PROTECTED"
                    row["included_in_randomizer"] = False
                    row["eligible_as_source"] = False
                    row["eligible_as_destination"] = False
                    row["exclusion_reason"] = "KEY_ITEM" if row["is_key_item"] else "QUEST_ITEM"
                    row["exclusion_detail"] = str(metadata.get("protection_reason") or "This exact reward identity is marked protected by the one-time enemy audit.")
            _set_reward_identity(row, reward.soft_object_path_identity)
            _set_availability(row, inventory, location.package_path, reward.article_path, unavailable)
            if key in unavailable_what and row["status"] == "INCLUDED":
                row["notes"] = _join_notes(row["notes"], "Vanilla WHAT Article is absent from the cache; this WHERE may still receive a reward from another available source.")
            rows.append(row)
            world_slot_rows.setdefault(key, row)

    _append_unsupported_cdo_rows(rows, catalogs, inventory, unavailable)
    _append_shop_rows(rows, catalogs, selected_options, reservations, inventory, unavailable)
    _append_blood_code_rows(rows, blood_codes, selected_options, inventory, unavailable)
    _append_booster_rows(rows, boosters, selected_options, inventory, unavailable)
    _append_extractor_rows(rows, extractor, selected_options, inventory, unavailable)
    _append_enemy_drop_rows(rows, enemy_catalog, selected_options, inventory, unavailable, catalogs)
    _append_material_catalog_metadata(
        rows, catalogs.materials, material_by_slot, world_slot_rows,
        selected_options, inventory, unavailable,
    )
    _append_reward_candidates(rows, gadgets, jails, formae, selected_options, inventory, unavailable, catalogs)
    _append_field_enemy_protected_keys(rows, field_enemy, inventory, unavailable)
    _append_unsafe_package_rows(rows, catalogs)
    _append_initial_inventory_rows(rows, selected_options, reservations, inventory, unavailable)
    quest_grant_audit = _append_quest_article_grant_audit(
        rows, catalogs, inventory, unavailable, formae,
    )

    # Some feature-specific passes adjust a row's status after _new_row has
    # initialized it. Keep the exported summary flag consistent with that final
    # status for every system (notably renewable Ingredients).
    for row in rows:
        row["included_in_randomizer"] = row["status"] == "INCLUDED"

    rows.sort(key=_row_sort_key)
    article_rows = _build_article_summary(rows)
    category_summary = _build_category_summary(rows)
    status_counts = {status: sum(row["status"] == status for row in rows) for status in STATUSES}
    validation = _validate_known_cases(rows, catalogs, world_slot_rows, selected_options)

    target = output_dir.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    payloads = {
        "randomizer_item_inventory.csv": _csv_bytes(rows, INVENTORY_COLUMNS),
        "randomizer_item_inventory.json": _json_bytes({
            "schema": "cv2-randomizer-item-inventory-v1",
            "catalog_revision": catalog_revision,
            "seed": normalized_seed,
            "generation_options": selected_options.to_dict(),
            "cache_inspected": inventory is not None,
            "cache_package_count": len(inventory.package_paths) if inventory is not None else None,
            "source_of_truth": "Checksum-validated runtime catalogs and production planner eligibility helpers; cross-system transfers preserve physical occurrence counts.",
            "availability_note": "An extracted cache proves installed-game presence for extracted packages; absent cache entries are marked installed=false only when the cache snapshot explicitly listed them as unavailable optional sources.",
            "quest_grant_audit": quest_grant_audit,
            "rows": rows,
            "category_summary": category_summary,
            "validation": validation,
        }),
        "randomizer_article_summary.csv": _csv_bytes(article_rows, (
            "article_name", "display_name", "content_class", "total_locations", "included_locations",
            "excluded_locations", "protected_locations", "disabled_locations", "unsupported_locations",
            "unavailable_locations", "categories", "pools", "can_appear_as_source",
            "can_appear_as_destination", "exclusion_reasons",
        )),
        "randomizer_inventory_summary.csv": _csv_bytes(category_summary, (
            "scope", "category", "total", "included", "excluded", "protected", "disabled", "unsupported", "unavailable",
        )),
    }
    _atomic_write_set(target, payloads)
    return InventoryExportResult(
        target,
        tuple(target / name for name in payloads),
        len(rows),
        status_counts,
        {
            item["category"]: item for item in category_summary
            if item["scope"] == "CATEGORY"
        },
        validation,
    )


# Build world plan
def _build_world_plan(
    locations: Iterable[WorldLootLocation], seed: str | int, options: GenerationOptions,
    catalogs: RuntimeCatalogs, field_enemy: Any, inventory: PackageInventory | None,
    candidates: tuple[Any, ...], reservations: RewardReservationSet, *, include_healing: bool,
) -> WorldLootRandomizationPlan:
    return build_world_loot_randomization_plan(
        locations,
        seed,
        enabled=options.randomize_world_loot,
        include_healing=include_healing,
        unsafe_packages=catalogs.unsafe_packages,
        available_reward_packages=inventory.package_paths if inventory is not None else None,
        catalog_candidates=candidates,
        reserved_reward_identities=reservations.asset_paths,
        supported_quest_grant_destinations=field_enemy.destination_ids,
    )


# Find available canonical articles
def _available_canonical_articles(catalog: Any, inventory: PackageInventory | None) -> Any:
    if inventory is None:
        return catalog
    return replace(catalog, items=tuple(item for item in catalog.items if inventory.contains(item.asset_path)))


# Find available gadgets
def _available_gadgets(catalog: Any, inventory: PackageInventory | None) -> Any:
    if inventory is None:
        return catalog
    return replace(
        catalog,
        items=tuple(item for item in catalog.items if inventory.contains(item.asset_path)),
        grants=tuple(
            grant for grant in catalog.grants
            if inventory.contains(grant.package_path) and inventory.contains(grant.original_asset_path)
        ),
    )


# Find available jails
def _available_jails(catalog: Any, inventory: PackageInventory | None) -> Any:
    if inventory is None:
        return catalog
    return replace(catalog, items=tuple(item for item in catalog.items if inventory.contains(item.asset_path)))


# Check world destination available
def _world_destination_available(location: WorldLootLocation, inventory: PackageInventory) -> bool:
    if not inventory.contains(location.package_path):
        return False
    if location.destination_kind == "BLUEPRINT_CDO_ARTICLE_BULK" and not all(
        inventory.contains(parent) for parent in location.parent_blueprint_packages
    ):
        return False
    if location.instance_override_source is not None:
        source = location.instance_override_source
        if (
            not inventory.contains(source.cdo_package)
            or not all(inventory.contains(parent) for parent in source.parent_blueprint_packages)
        ):
            return False
    if location.location_type in {
        "RENEWABLE_INGREDIENT", "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
    } and (location.structural_class_package is None or not inventory.contains(location.structural_class_package)):
        return False
    return True


# List world source slots
def _world_source_slots(plan: WorldLootRandomizationPlan) -> set[tuple[str, int]]:
    result: set[tuple[str, int]] = set()
    for assignment in plan.assignments:
        if assignment.source is not None:
            result.add(_slot_key(assignment.source.package_path, assignment.source.export_index))
        candidate = assignment.source_candidate
        if candidate is not None:
            source_locations = candidate.source_locations or (
                (candidate.source_location,) if candidate.source_location is not None else ()
            )
            result.update(
                _slot_key(source.package_path, source.export_index)
                for source in source_locations
            )
    return result


# List append shop rows
def _append_shop_rows(
    rows: list[dict[str, Any]], catalogs: RuntimeCatalogs, options: GenerationOptions,
    reservations: RewardReservationSet, inventory: PackageInventory | None,
    unavailable: set[str],
) -> None:
    for product in sorted(catalogs.shops, key=lambda item: (item.family, item.shop_row.casefold(), item.product_index)):
        trader = is_npc_trader_row(product.family, product.shop_row)
        category = "NPC_TRADER" if trader else "SHOP"
        stinger_alternate = is_stinger_shop_slot(product)
        linked_stinger_alternate = stinger_alternate and options.randomize_world_loot
        control = "randomize_shops"
        enabled = options.randomize_shops
        reason = shop_catalog_slot_lock_reason(product)
        reserved = any(
            item.asset_path.casefold() == product.original_product_path.casefold()
            for item in reservations.identities
        )
        if linked_stinger_alternate:
            status, exclusion, detail = (
                "INCLUDED", "", "Mirrors Josee's Outpost Stinger reward; this is an alternate route, not a second source occurrence."
            )
        elif stinger_alternate:
            status, exclusion, detail = (
                "PROTECTED", "LINKED_ALTERNATE_ROUTE",
                "Josee's shop route shares the unique Stinger reward and is never shuffled separately; World Loot controls its linked result.",
            )
        elif reserved:
            status, exclusion, detail = "EXCLUDED", "RESERVED_BY_OTHER_SYSTEM", "This Article is allocated to an earlier enabled randomizer system."
        elif reason == "INFINITE_SHOP_STOCK":
            status, exclusion, detail = "EXCLUDED", reason, "Infinite-stock slots do not participate in Shop shuffling."
        elif reason == "UNKNOWN_SHOP_STOCK_SEMANTICS":
            status, exclusion, detail = "UNSUPPORTED", reason, "Finite stock cannot be proven from the catalog fields."
        elif reason:
            status = "PROTECTED" if _is_protection_reason(reason) else "EXCLUDED"
            exclusion, detail = reason, (product.protection_reason or "The catalog marks this Shop destination protected.")
        else:
            status, exclusion, detail = "INCLUDED", "", ""
        if status == "INCLUDED" and not enabled and not linked_stinger_alternate:
            status, exclusion, detail = "DISABLED", "OPTION_DISABLED", "Randomize Shops and NPC Traders is off in the selected configuration."
        package = f"/Game/Blueprint/GameSystem/Shop/{SHOP_TABLE_NAMES[product.family.upper()]}"
        article = product.original_product_path
        table_available = inventory is None or inventory.contains(package)
        article_available = inventory is None or inventory.contains(article)
        if inventory is not None and not table_available:
            status, exclusion, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The Shop DataTable is absent from the validated extracted cache."
        if stinger_alternate:
            control = "randomize_world_loot"
            enabled = options.randomize_world_loot
        row = _new_row(
            category=category,
            subcategory=product.family,
            system_owner="NPC_TRADERS" if trader else "SHOPS",
            status=status,
            intrinsically_eligible=not stinger_alternate and reason is None and not reserved,
            enabled_by_current_config=enabled,
            exclusion_reason=exclusion,
            exclusion_detail=detail,
            location_id=f"{product.family}:{product.shop_row}:{product.product_index}",
            source_type="NPC_TRADER_SLOT" if trader else "SHOP_SLOT",
            article_name=product.item_object,
            resolved_article=article,
            content_class=product.what_content_class,
            map="",
            area="",
            package=package,
            object_path=f"{package}.{product.shop_row}[{product.product_index}]",
            actor=product.shop_row,
            actor_class="",
            reward_owner=product.shop_row,
            count=None,
            stock=product.stock,
            is_scripted=False,
            eligible_as_source=(not stinger_alternate and reason is None and not reserved and article_available and table_available),
            eligible_as_destination=(not stinger_alternate and reason is None and not reserved and table_available),
            randomizer_pool=(
                "WORLD_LINKED_ALTERNATE" if stinger_alternate else
                "SHOP_WORLD_SHARED" if options.mix_shops_with_world else "SHOP"
            ),
            controlled_by_option=control,
            notes=(
                f"price={product.price}; uses_stock={str(product.uses_stock).lower()}; "
                + ("mirrors the Josee's Outpost Stinger Article assignment; excluded from pool/source counts" if stinger_alternate else "")
            ),
        )
        _set_article_protection(row, article, catalogs)
        _set_availability(row, inventory, package, article, unavailable)
        rows.append(row)


# Add Blood Code rows to report
def _append_blood_code_rows(rows: list[dict[str, Any]], catalog: Any, options: GenerationOptions,
                            inventory: PackageInventory | None, unavailable: set[str]) -> None:
    for slot in catalog.slots:
        control, enabled = "randomize_blood_codes", options.randomize_blood_codes
        status, reason, detail = _option_status(enabled, intrinsically_eligible=True, control=control)
        source_packages = slot.physical_source_packages
        missing = inventory is not None and any(not inventory.contains(path) for path in source_packages)
        missing = missing or (inventory is not None and not inventory.contains(slot.original_blood_code_package))
        if missing:
            status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "A physical grant/reference package or the Blood Code Article is absent from the validated extracted cache."
        row = _new_row(
            category="BLOOD_CODE", subcategory=slot.location_kind.upper(), system_owner="BLOOD_CODES",
            status=status, intrinsically_eligible=True, enabled_by_current_config=enabled,
            exclusion_reason=reason, exclusion_detail=detail,
            location_id=slot.slot_id, source_type=slot.location_kind,
            article_name=slot.original_blood_code_asset, resolved_article=slot.original_blood_code_package,
            content_class=slot.content_class, package=slot.virtual_package,
            object_path=f"{slot.virtual_package}.{slot.export_name}", actor=slot.export_name,
            reward_owner=slot.virtual_package, reward_index=slot.reward_index, count=slot.count,
            is_scripted=slot.location_kind in {"scenario_reward", "gain_item"},
            eligible_as_source=not missing, eligible_as_destination=not missing,
            randomizer_pool="BLOOD_CODE_ONLY", controlled_by_option=control,
            unlock_condition="", notes=f"references={len(slot.physical_reference_targets)}; physical_packages={len(source_packages)}",
        )
        row["is_progression_item"] = True
        row["is_scripted"] = slot.location_kind in {"gain_item", "scenario_reward"}
        _set_availability(row, inventory, slot.virtual_package, slot.original_blood_code_package, unavailable)
        if missing:
            row["available_in_cache"] = False
            if any(path.casefold() in unavailable for path in (*source_packages, slot.original_blood_code_package)):
                row["available_in_installed_game"] = False
        rows.append(row)


# List append booster rows
def _append_booster_rows(rows: list[dict[str, Any]], catalog: Any, options: GenerationOptions,
                         inventory: PackageInventory | None, unavailable: set[str]) -> None:
    for slot in catalog.slots:
        enabled = options.randomize_boosters
        status, reason, detail = _option_status(enabled, intrinsically_eligible=True, control="randomize_boosters")
        booster_table_available = inventory is None or inventory.contains("/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/DT_GainBoosterSmallScenario")
        booster_article_available = inventory is None or inventory.contains(slot.booster_article_path)
        if inventory is not None and not booster_table_available:
            status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The Booster mastery DataTable is absent from the validated extracted cache."
        row = _new_row(
            category="BOOSTER", subcategory="MASTERY_REWARD", system_owner="BOOSTERS",
            status=status, intrinsically_eligible=True, enabled_by_current_config=enabled,
            exclusion_reason=reason, exclusion_detail=detail,
            location_id=f"DT_GainBoosterSmallScenario:{slot.row_name}", source_type="BOOSTER_MASTERY_ROW",
            article_name=slot.booster_article_asset, resolved_article=slot.booster_article_path,
            content_class=slot.content_class, package="/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/DT_GainBoosterSmallScenario",
            object_path=f"DT_GainBoosterSmallScenario.{slot.row_name}", actor=slot.row_name,
            actor_class="", reward_owner="BoosterMastery", reward_index=slot.row_index,
            count=slot.count, unlock_condition=slot.evaluation_formula_original,
            is_scripted=True, eligible_as_source=booster_table_available and booster_article_available,
            eligible_as_destination=booster_table_available,
            randomizer_pool="BOOSTER_ONLY", controlled_by_option="randomize_boosters",
            notes=f"story_flag={slot.booster_story_flag}; blood_code={slot.blood_code_article_asset}; mastery={slot.mastery_owner}:{slot.mastery_rank}; notification={str(slot.b_notification).lower()}",
        )
        _set_availability(
            row, inventory,
            "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/DT_GainBoosterSmallScenario",
            slot.booster_article_path, unavailable,
        )
        rows.append(row)


# List append extractor rows
def _append_extractor_rows(rows: list[dict[str, Any]], catalog: Any, options: GenerationOptions,
                           inventory: PackageInventory | None, unavailable: set[str]) -> None:
    for table in catalog.tables:
        for slot in table.rows:
            enabled = options.randomize_extractor_recipes
            status, reason, detail = _option_status(enabled, intrinsically_eligible=True, control="randomize_extractor_recipes")
            table_available = inventory is None or inventory.contains(slot.virtual_package)
            article_available = inventory is None or inventory.contains(slot.original_article_path)
            if inventory is not None and not table_available:
                status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The recipe DataTable is absent from the validated extracted cache."
            row = _new_row(
                category="BOOSTER_CRAFTING", subcategory=table.content_class, system_owner="BOOSTER_CRAFTING",
                status=status, intrinsically_eligible=True, enabled_by_current_config=enabled,
                exclusion_reason=reason, exclusion_detail=detail,
                location_id=f"{table.table_name}:{slot.row_name}", source_type="BOOSTER_RECIPE_ROW",
                article_name=slot.row_name, resolved_article=slot.original_article_path,
                content_class=table.content_class, package=slot.virtual_package,
                object_path=f"{slot.virtual_package}.{slot.row_name}", actor=slot.row_name,
                actor_class="", reward_owner=table.table_name, reward_index=slot.row_index,
                eligible_as_source=table_available and article_available,
                eligible_as_destination=table_available,
                randomizer_pool="BOOSTER_CRAFTING_ONLY", controlled_by_option="randomize_extractor_recipes",
                notes="Recipe output identity; ingredient fields are preserved by the recipe randomizer.",
            )
            _set_availability(row, inventory, slot.virtual_package, slot.original_article_path, unavailable)
            rows.append(row)


# List append enemy drop rows
def _append_enemy_drop_rows(rows: list[dict[str, Any]], catalog: Any, options: GenerationOptions,
                            inventory: PackageInventory | None, unavailable: set[str],
                            catalogs: RuntimeCatalogs) -> None:
    research_path = application_data_path("enemy_drops", "enemy_drops_reward_slots_research.csv")
    classifications = catalog.by_row()
    try:
        with research_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            for item in reader:
                table = (item.get("source_table") or "").strip().upper()
                row_name = (item.get("row_name") or "").strip()
                try:
                    slot_index = int(item.get("slot_index") or "0")
                    article_index = int(item.get("article_index") or "0")
                except ValueError as error:
                    raise ValueError(f"Invalid Enemy Drop research slot {table}:{row_name}.") from error
                classification = classifications.get((table, row_name.casefold()))
                if classification is None:
                    raise ValueError(f"Enemy Drop slot has no production classification: {table}:{row_name}.")
                source_package = (item.get("package_name") or "").strip()
                article_name = (item.get("asset_name") or "").strip()
                classification_name = classification.classification
                if classification_name in RANDOMIZED_CLASSIFICATIONS:
                    status, reason, detail = _option_status(options.randomize_enemy_drops, True, "randomize_enemy_drops")
                else:
                    status, reason, detail = "UNSUPPORTED", "AMBIGUOUS_ENEMY_RESPAWN_CLASSIFICATION", "The project has not proven whether this generic DropItem row is repeatable or a one-time reward."
                shared_row = classification_name == "SHARED_BOSS_NORMAL"
                package = dict(ENEMY_DROP_TABLES).get(table, "")
                table_available = inventory is None or inventory.contains(package)
                article_available = inventory is None or not source_package or inventory.contains(source_package)
                if inventory is not None and not table_available:
                    status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The enemy Drop DataTable is absent from the validated extracted cache."
                weight = (item.get("weight") or "").strip()
                notes = f"weight={weight}; notification={item.get('b_notification','')}" if weight else f"notification={item.get('b_notification','')}"
                row = _new_row(
                    category="ENEMY_DROP", subcategory=classification_name, system_owner="ENEMY_DROPS",
                    status=status, intrinsically_eligible=classification_name in RANDOMIZED_CLASSIFICATIONS,
                    enabled_by_current_config=options.randomize_enemy_drops,
                    exclusion_reason=reason, exclusion_detail=detail,
                    location_id=f"{table}:{row_name}:{slot_index}:{article_index}",
                    source_type="RESPAWNABLE_ENEMY_DROP_ROW", article_name=article_name,
                    resolved_article=source_package, content_class=table,
                    package=package, object_path=f"{package}.{row_name}[{slot_index}].Articles[{article_index}]",
                    actor=row_name, actor_class="", reward_owner=f"{table}:{row_name}",
                    reward_index=article_index,
                    count=_int_or_none(item.get("count")), drop_chance="",
                    is_key_item=False,
                    is_quest_item=False,
                    is_progression_item=False,
                    eligible_as_source=classification_name in RANDOMIZED_CLASSIFICATIONS and options.randomize_enemy_drops and table_available and article_available,
                    eligible_as_destination=classification_name in RANDOMIZED_CLASSIFICATIONS and options.randomize_enemy_drops and table_available,
                    randomizer_pool="ENEMY_DROP_ONLY", controlled_by_option="randomize_enemy_drops",
                    notes=(f"{notes}; shared_by_normal_and_boss_miniboss" if shared_row else notes),
                )
                _set_article_protection(row, source_package, catalogs)
                if row["is_key_item"] or row["is_quest_item"]:
                    row["status"] = "PROTECTED"
                    row["included_in_randomizer"] = False
                    row["exclusion_reason"] = "KEY_ITEM" if row["is_key_item"] else "QUEST_ITEM"
                    row["exclusion_detail"] = "The exact reward Article is listed in the protected key/quest policy."
                    row["eligible_as_source"] = False
                    row["eligible_as_destination"] = False
                _set_availability(row, inventory, package, source_package, unavailable)
                rows.append(row)
    except OSError as error:
        raise RuntimeError(f"Could not read the shipped Enemy Drop slot research catalog: {error}") from error


# Build append material catalog metadata
def _append_material_catalog_metadata(
    rows: list[dict[str, Any]],
    materials: tuple[MaterialLocation, ...],
    by_slot: dict[tuple[str, int], MaterialLocation],
    world_rows: dict[tuple[str, int], dict[str, Any]],
    options: GenerationOptions,
    inventory: PackageInventory | None,
    unavailable: set[str],
) -> None:
    for key, material in by_slot.items():
        row = world_rows.get(key)
        if row is None:
            # Catalog validation should make this unreachable: the Materials
            # inventory is a reviewed subset of physical renewable pickups.
            raise ValueError(f"Materials source has no matching World Loot physical location: {material.package_path}#{material.export_index}.")
        row["subcategory"] = "MATERIALS_CATALOGLED_RENEWABLE"
        row["system_owner"] = "MATERIALS"
        row["controlled_by_option"] = "randomize_materials"
        row["enabled_by_current_config"] = options.randomize_materials
        row["randomizer_pool"] = "INGREDIENTS"
        row["intrinsically_eligible"] = True
        row["eligible_as_source"] = True
        row["eligible_as_destination"] = True
        row["exclusion_reason"] = ""
        row["exclusion_detail"] = ""
        packages_present = inventory is None or all(
            inventory.contains(path)
            for path in (
                material.package_path,
                material.resource_class_package,
                package_path_from_object_path(material.reward.article_path),
            )
        )
        _set_availability(row, inventory, material.package_path, material.reward.article_path, unavailable)
        if inventory is not None and not inventory.contains(material.resource_class_package):
            row["available_in_cache"] = False
            row["available_in_installed_game"] = (
                False if material.resource_class_package.casefold() in unavailable else ""
            )
        if not packages_present:
            row["status"] = "UNAVAILABLE"
            row["exclusion_reason"] = "OPTIONAL_CONTENT_ABSENT"
            row["exclusion_detail"] = "The map, ingredient Blueprint, or ingredient Article is absent from the validated extracted cache."
            row["eligible_as_source"] = False
            row["eligible_as_destination"] = False
        elif options.randomize_materials:
            row["status"] = "INCLUDED"
        else:
            row["status"] = "DISABLED"
            row["exclusion_reason"] = "OPTION_DISABLED"
            row["exclusion_detail"] = "Feature control randomize_materials is off in the selected configuration."
        row["notes"] = _join_notes(
            row["notes"],
            f"resource_class={material.resource_class_name}; resource_class_package={material.resource_class_package}; material_reward={material.reward.article_path}; all pickups of each ingredient type share one global replacement type",
        )


# List append reward candidates
def _append_reward_candidates(rows: list[dict[str, Any]], gadgets: Any, jails: Any, formae: Any,
                              options: GenerationOptions, inventory: PackageInventory | None,
                              unavailable: set[str], catalogs: RuntimeCatalogs) -> None:
    for item in gadgets.items:
        enabled = options.randomize_world_loot and item.world_reward_eligible
        status, reason, detail = _option_status(enabled, item.world_reward_eligible, "ALWAYS_ON_WORLD_LOOT")
        if inventory is not None and not inventory.contains(item.asset_path):
            status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The canonical Defensive Gadget Article is absent from the validated extracted cache."
        row = _new_row(
            category="DEFENSIVE_GADGET", subcategory="WORLD_REWARD_CANDIDATE", system_owner="WORLD_LOOT",
            status=status, intrinsically_eligible=item.world_reward_eligible, enabled_by_current_config=enabled,
            exclusion_reason=reason, exclusion_detail=detail,
            location_id=f"candidate:defensive_gadget:{item.object}", source_type="CANONICAL_REWARD_CANDIDATE",
            article_name=item.object, display_name="", resolved_article=item.asset_path,
            content_class=item.content_class, eligible_as_source=item.world_reward_eligible and status != "UNAVAILABLE",
            eligible_as_destination=False, randomizer_pool="WORLD",
            controlled_by_option="ALWAYS_ON_WORLD_LOOT", notes=f"aliases={';'.join(item.aliases)}",
        )
        _set_article_protection(row, item.asset_path, catalogs)
        _set_availability(row, inventory, "", item.asset_path, unavailable)
        rows.append(row)

    for item in jails.items:
        source = item.vanilla_source
        enabled = options.randomize_world_loot and item.world_reward_eligible
        status, reason, detail = _option_status(enabled, item.world_reward_eligible, "ALWAYS_ON_WORLD_LOOT")
        if inventory is not None and not inventory.contains(item.asset_path):
            status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The canonical Jail Article is absent from the validated extracted cache."
        row = _new_row(
            category="JAIL", subcategory=source.source_type, system_owner="WORLD_LOOT",
            status=status, intrinsically_eligible=item.world_reward_eligible, enabled_by_current_config=enabled,
            exclusion_reason=reason, exclusion_detail=detail,
            location_id=f"candidate:jail:{item.object}", source_type="CANONICAL_REWARD_CANDIDATE",
            display_name=item.object, article_name=item.object, resolved_article=item.asset_path,
            content_class=item.content_class, package=source.package_path or "", actor=source.export_name or "",
            actor_class="", reward_owner="WORLD_LOOT", reward_index=source.reward_index,
            count=source.count, is_scripted=source.scripted_reward_mode is not None,
            eligible_as_source=item.world_reward_eligible and status != "UNAVAILABLE", eligible_as_destination=False,
            randomizer_pool="WORLD;INITIAL_INVENTORY_JAIL", controlled_by_option="ALWAYS_ON_WORLD_LOOT",
            notes=f"vanilla_source={source.source_type}; movable_world_loot={str(source.movable_world_loot).lower()}; {source.note or ''}",
        )
        _set_article_protection(row, item.asset_path, catalogs)
        _set_availability(row, inventory, "", item.asset_path, unavailable)
        rows.append(row)

    for item in formae.items:
        if item.source_class == "DIRECT_QUEST" and item.world_loot_destination_id:
            pool, control, enabled = "WORLD", "ALWAYS_ON_WORLD_LOOT", options.randomize_world_loot
        elif item.source_class == "DIRECT_QUEST":
            pool, control, enabled = "BEQUEATHED_FORMAE", "ALWAYS_ON_BEQUEATHED_FORMAE", True
        elif item.source_class == "SHOP":
            pool, control, enabled = ("SHOP_WORLD_SHARED" if options.mix_shops_with_world else "SHOP"), "randomize_shops", options.randomize_shops
        else:
            pool, control, enabled = "WORLD", "ALWAYS_ON_WORLD_LOOT", options.randomize_world_loot
        if item.source_class == "DIRECT_QUEST":
            intrinsic = item.world_loot_destination_id is not None or len(formae.separate_direct_rewards) > 1
        elif item.source_class == "SHOP":
            intrinsic = item.shop_reward_eligible
        else:
            intrinsic = item.world_reward_eligible
        status, reason, detail = _option_status(enabled, intrinsic, control)
        forma_available = inventory is None or (
            inventory.contains(item.asset_path)
            and all(inventory.contains(grant.quest_asset) for grant in item.direct_grants)
        )
        if inventory is not None and not forma_available:
            status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The canonical Bequeathed Forma Article or one of its direct grant packages is absent from the validated extracted cache."
        row = _new_row(
            category="BEQUEATHED_FORMAE", subcategory=item.source_class, system_owner="BEQUEATHED_FORMAE",
            status=status, intrinsically_eligible=intrinsic, enabled_by_current_config=enabled,
            exclusion_reason=reason, exclusion_detail=detail,
            location_id=f"candidate:bequeathed_formae:{item.object}", source_type="CANONICAL_REWARD_CANDIDATE",
            article_name=item.object, display_name=item.game_name, resolved_article=item.asset_path,
            content_class=item.content_class, eligible_as_source=intrinsic and forma_available,
            eligible_as_destination=False, randomizer_pool=pool, controlled_by_option=control,
            notes=f"world_reward_eligible={str(item.world_reward_eligible).lower()}; shop_reward_eligible={str(item.shop_reward_eligible).lower()}; logical_group={item.logical_reward_group or ''}; direct_grants={len(item.direct_grants)}",
        )
        _set_article_protection(row, item.asset_path, catalogs)
        _set_availability(row, inventory, "", item.asset_path, unavailable)
        if inventory is not None and not forma_available:
            row["available_in_cache"] = False
        rows.append(row)

    formae_pool_enabled = options.randomize_bequeathed_formae_quest_rewards
    formae_pool_eligible = len(formae.separate_direct_rewards) > 1
    for item in formae.direct_rewards:
        if item.world_loot_destination_id is not None:
            for grant_index, grant in enumerate(item.direct_grants[1:], start=1):
                grant_status, grant_reason, grant_detail = (
                    "EXCLUDED", "LOGICAL_REWARD_MIRROR",
                    f"This is alternate route {grant_index + 1} in logical reward group "
                    f"{item.logical_reward_group}; its item is randomized once at "
                    f"{item.world_loot_destination_id} and mirrored here.",
                )
                if inventory is not None and (
                    not inventory.contains(grant.quest_asset) or not inventory.contains(item.asset_path)
                ):
                    grant_status, grant_reason, grant_detail = (
                        "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT",
                        "The alternate quest package or Forma Article is absent from the validated extracted cache.",
                    )
                grant_row = _new_row(
                    category="BEQUEATHED_FORMAE", subcategory="ALTERNATE_QUEST_GRANT_PATH",
                    system_owner="BEQUEATHED_FORMAE", status=grant_status,
                    intrinsically_eligible=item.world_reward_eligible,
                    enabled_by_current_config=options.randomize_world_loot,
                    exclusion_reason=grant_reason, exclusion_detail=grant_detail,
                    location_id=f"{grant.quest_asset}#{grant.export_index}",
                    source_type="ALTERNATE_QUEST_REWARD_PATH", display_name=item.game_name,
                    article_name=item.object, resolved_article=item.asset_path,
                    content_class=item.content_class, package=grant.quest_asset,
                    object_path=f"{grant.quest_asset}#export:{grant.export_index}",
                    actor=f"export:{grant.export_index}", reward_owner=grant.quest_asset,
                    count=grant.count, is_scripted=True, is_one_time=True,
                    eligible_as_source=False, eligible_as_destination=False,
                    randomizer_pool="WORLD_MIRROR", controlled_by_option="ALWAYS_ON_WORLD_LOOT",
                    notes=(f"logical_group={item.logical_reward_group}; mirrors primary destination "
                           f"{item.world_loot_destination_id}; alternate path {grant_index + 1} of {len(item.direct_grants)}"),
                )
                _set_availability(grant_row, inventory, grant.quest_asset, item.asset_path, unavailable)
                rows.append(grant_row)
            continue
        intrinsic = formae_pool_eligible
        status, reason, detail = _option_status(
            formae_pool_enabled, intrinsic, "ALWAYS_ON_BEQUEATHED_FORMAE",
        )
        for grant_index, grant in enumerate(item.direct_grants):
            grant_status, grant_reason, grant_detail = status, reason, detail
            if inventory is not None and (
                not inventory.contains(grant.quest_asset) or not inventory.contains(item.asset_path)
            ):
                grant_status, grant_reason, grant_detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The direct quest package or Forma Article is absent from the validated extracted cache."
            grant_row = _new_row(
                category="BEQUEATHED_FORMAE", subcategory="DIRECT_QUEST_GRANT",
                system_owner="BEQUEATHED_FORMAE", status=grant_status,
                intrinsically_eligible=intrinsic, enabled_by_current_config=formae_pool_enabled,
                exclusion_reason=grant_reason, exclusion_detail=grant_detail,
                location_id=f"{grant.quest_asset}#{grant.export_index}",
                source_type="DIRECT_QUEST_ARTICLE_REWARD", display_name=item.game_name,
                article_name=item.object, resolved_article=item.asset_path,
                content_class=item.content_class, package=grant.quest_asset,
                object_path=f"{grant.quest_asset}#export:{grant.export_index}",
                actor=f"export:{grant.export_index}", reward_owner=grant.quest_asset,
                count=grant.count, is_scripted=True, is_one_time=True,
                eligible_as_source=intrinsic and grant_status != "UNAVAILABLE", eligible_as_destination=intrinsic and grant_status != "UNAVAILABLE",
                randomizer_pool="BEQUEATHED_FORMAE", controlled_by_option="ALWAYS_ON_BEQUEATHED_FORMAE",
                notes=f"file={grant.relative_file}; UI export #{grant.ui_export_number}; grant occurrence {grant_index + 1}; notification={str(grant.notification).lower()}",
            )
            _set_availability(grant_row, inventory, grant.quest_asset, item.asset_path, unavailable)
            rows.append(grant_row)

    for grant in gadgets.grants:
        enabled = options.randomize_world_loot
        status, reason, detail = _option_status(enabled, True, "ALWAYS_ON_WORLD_LOOT")
        grant_available = inventory is None or (
            inventory.contains(grant.package_path) and inventory.contains(grant.original_asset_path)
        )
        if inventory is not None and not grant_available:
            status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "The Defensive Gadget NPC grant package or its Article source is absent from the validated extracted cache."
        row = _new_row(
            category="DEFENSIVE_GADGET", subcategory="NPC_DIRECT_GRANT", system_owner="WORLD_LOOT",
            status=status, intrinsically_eligible=True, enabled_by_current_config=enabled,
            exclusion_reason=reason, exclusion_detail=detail,
            location_id=grant.slot_id, source_type="NPC_DIRECT_ARTICLE_REWARD",
            article_name=grant.original_asset_path.rsplit("/", 1)[-1],
            resolved_article=grant.original_asset_path, content_class=grant.content_class,
            package=grant.package_path, object_path=f"{grant.package_path}.{grant.export_name}",
            actor=grant.export_name, reward_owner=grant.slot_id, reward_index=grant.reward_index,
            count=grant.count, is_scripted=True, is_one_time=True,
            eligible_as_source=grant_available, eligible_as_destination=grant_available,
            randomizer_pool="WORLD", controlled_by_option="ALWAYS_ON_WORLD_LOOT",
            notes=f"family={grant.family}; property={grant.property_path}; notification={str(grant.notification).lower()}",
        )
        _set_article_protection(row, grant.original_asset_path, catalogs)
        _set_availability(row, inventory, grant.package_path, grant.original_asset_path, unavailable)
        rows.append(row)

    for grant in gadgets.pending_grants:
        rows.append(_new_row(
            category="DEFENSIVE_GADGET", subcategory="PENDING_NPC_GRANT", system_owner="WORLD_LOOT",
            status="UNSUPPORTED", intrinsically_eligible=False, enabled_by_current_config=options.randomize_world_loot,
            exclusion_reason="UNSUPPORTED_REWARD_STRUCTURE", exclusion_detail=grant.source_description,
            location_id=grant.slot_id, source_type="UNCONFIRMED_NPC_GRANT",
            display_name=grant.family, content_class=grant.content_class,
            eligible_as_source=False, eligible_as_destination=False,
            randomizer_pool="NONE", controlled_by_option="ALWAYS_ON_WORLD_LOOT",
            notes=f"catalog status={grant.status}; no confirmed Article path or package was supplied.",
        ))


# Read append field enemy protected keys
def _append_field_enemy_protected_keys(
    rows: list[dict[str, Any]], catalog: Any, inventory: PackageInventory | None,
    unavailable: set[str],
) -> None:
    for item in catalog.protected_exclusions:
        row = _new_row(
            category="ONE_TIME_ENEMY_REWARD", subcategory="FIELD_ENEMY_PROTECTED_KEY",
            system_owner="ENEMY_DROPS", status="PROTECTED", intrinsically_eligible=False,
            enabled_by_current_config=False, exclusion_reason="KEY_ITEM",
            exclusion_detail="This FieldEnemy key reward is an explicit protected exclusion and is never routed into World Loot.",
            location_id=item.destination_id, source_type="PROTECTED_QUEST_KEY_REWARD",
            article_name=item.article_path.rsplit("/", 1)[-1], resolved_article=item.article_path,
            package=item.package_path, object_path=item.package_path, actor=item.destination_id,
            reward_owner=item.destination_id, is_key_item=True, is_progression_item=True,
            is_scripted=True, is_one_time=True, eligible_as_source=False,
            eligible_as_destination=False, randomizer_pool="NONE",
            controlled_by_option="LOCKED_PROTECTION",
            notes="Confirmed FieldEnemy exclusion; vanilla key reward remains unchanged.",
        )
        _set_article_protection(row, item.article_path, None)
        _set_availability(row, inventory, item.package_path, item.article_path, unavailable)
        rows.append(row)


# List append unsafe package rows
def _append_unsafe_package_rows(rows: list[dict[str, Any]], catalogs: RuntimeCatalogs) -> None:
    known_sources = {location.source_file.casefold() for location in catalogs.world_loot}
    for source_file in sorted(catalogs.unsafe_packages, key=str.casefold):
        if source_file.casefold() in known_sources:
            continue
        rows.append(_new_row(
            category="OTHER_SUPPORTED", subcategory="UNSAFE_PACKAGE", system_owner="WORLD_LOOT",
            status="UNSUPPORTED", intrinsically_eligible=False, enabled_by_current_config=True,
            exclusion_reason="UNSAFE_PACKAGE", exclusion_detail="This source is listed in the validated unsafe-package exclusion catalog.",
            location_id=f"unsafe-package:{source_file}", source_type="UNSAFE_OR_UNPARSED_PACKAGE",
            package=source_file, object_path=source_file, eligible_as_source=False,
            eligible_as_destination=False, randomizer_pool="NONE",
            controlled_by_option="LOCKED_PROTECTION", notes="No safe parsed reward location is available for this package.",
        ))


# List append initial inventory rows
def _append_initial_inventory_rows(rows: list[dict[str, Any]], options: GenerationOptions,
                                  reservations: RewardReservationSet, inventory: PackageInventory | None,
                                  unavailable: set[str]) -> None:
    start_available = inventory is None or inventory.contains(INITIAL_INVENTORY_PACKAGE)
    for slot_id, display, category, pool in _STARTING_SLOTS:
        enabled = options.randomize_initial_inventory
        status, reason, detail = _option_status(enabled, True, "randomize_initial_inventory")
        if inventory is not None and not start_available:
            status, reason, detail = "UNAVAILABLE", "OPTIONAL_CONTENT_ABSENT", "DA_InitialInventory_Player is absent from the validated extracted cache."
        rows.append(_new_row(
            category="INITIAL_INVENTORY", subcategory=category.upper(), system_owner="INITIAL_INVENTORY",
            status=status, intrinsically_eligible=True, enabled_by_current_config=enabled,
            exclusion_reason=reason, exclusion_detail=detail,
            location_id=slot_id, source_type="INITIAL_INVENTORY_SLOT", display_name=display,
            package=INITIAL_INVENTORY_PACKAGE, object_path=f"{INITIAL_INVENTORY_PACKAGE}.{slot_id}",
            actor=slot_id, actor_class="", reward_owner=INITIAL_INVENTORY_PACKAGE,
            eligible_as_source=False, eligible_as_destination=start_available,
            randomizer_pool=pool, controlled_by_option="randomize_initial_inventory",
            notes="Actual source Article is inspected from the installed DA_InitialInventory_Player asset at Generate time.",
        ))
        _set_availability(rows[-1], inventory, INITIAL_INVENTORY_PACKAGE, "", unavailable)
    for article_name, article_path in _STARTING_FIXED_ITEMS:
        row = _new_row(
            category="INITIAL_INVENTORY", subcategory="VANILLA_FIXED_ITEM", system_owner="INITIAL_INVENTORY",
            status="PROTECTED", intrinsically_eligible=False, enabled_by_current_config=False,
            exclusion_reason="INITIAL_FIXED_ITEM", exclusion_detail="This startup item remains vanilla and is not one of the three randomized starting slots.",
            location_id=f"fixed:{article_name}", source_type="INITIAL_FIXED_ITEM",
            article_name=article_name, resolved_article=article_path, content_class="UNKNOWN",
            package=INITIAL_INVENTORY_PACKAGE, reward_owner=INITIAL_INVENTORY_PACKAGE,
            is_progression_item=article_name == "DA_BloodCode_Lou_01",
            eligible_as_source=False, eligible_as_destination=False,
            randomizer_pool="NONE", controlled_by_option="LOCKED_PROTECTION",
            notes="Validated startup data preserves this item; starting Blood Code is separately granted by the beginning quest.",
        )
        _set_availability(row, inventory, INITIAL_INVENTORY_PACKAGE, article_path, unavailable)
        rows.append(row)


# List append unsupported CDO rows
def _append_unsupported_cdo_rows(
    rows: list[dict[str, Any]], catalogs: RuntimeCatalogs,
    inventory: PackageInventory | None, unavailable: set[str],
) -> None:
    ingredient_definitions = {
        (item.resource_class_package.casefold(), item.reward.article_path.casefold())
        for item in catalogs.materials
    }
    for item in sorted(catalogs.world_loot_unsupported_cdo_sources, key=lambda value: value.blueprint_package.casefold()):
        # These CDO references describe renewable ingredient classes whose
        # physical pickups are already reported in the Ingredients section.
        # Emitting a second unsupported World Loot row for the same class and
        # Article makes the inventory look as if the ingredient were excluded.
        if (item.blueprint_package.casefold(), item.reward.article_path.casefold()) in ingredient_definitions:
            continue
        row = _new_row(
            category="OTHER_SUPPORTED", subcategory="UNSUPPORTED_CDO_ARTICLE_BULK", system_owner="WORLD_LOOT",
            status="UNSUPPORTED", intrinsically_eligible=False, enabled_by_current_config=True,
            exclusion_reason="UNSUPPORTED_REWARD_STRUCTURE", exclusion_detail=item.reason,
            location_id=f"unsupported-cdo:{item.blueprint_package}#{item.cdo_export_index}",
            source_type="UNSUPPORTED_BLUEPRINT_CDO", article_name=item.reward.article_path.rsplit("/", 1)[-1],
            resolved_article=item.reward.article_path, content_class=item.reward.content_class,
            package=item.blueprint_package, object_path=f"{item.blueprint_package}.{item.cdo_export_name}",
            actor=item.cdo_export_name, actor_class=item.blueprint_class, reward_owner=item.blueprint_package,
            count=item.reward.count, eligible_as_source=False, eligible_as_destination=False,
            randomizer_pool="NONE", controlled_by_option="LOCKED_PROTECTION",
            notes=f"physical_instances={item.physical_instance_count}; unsupported_instances={item.unsupported_instance_count}",
        )
        _set_availability(row, inventory, item.blueprint_package, item.reward.article_path, unavailable)
        rows.append(row)


# Find world category
def _world_category(location: WorldLootLocation, *, is_field_enemy: bool) -> tuple[str, str]:
    if is_field_enemy:
        return "ONE_TIME_ENEMY_REWARD", "FIELD_ENEMY_DIRECT_REWARD"
    if location.location_type in _WORLD_TYPES:
        return "HEALING_UPGRADE", location.location_type
    if _is_renewable_location(location):
        return "RENEWABLE_INGREDIENT", "WORLD_RENEWABLE_NODE"
    destination = (location.destination_id or "").casefold()
    if destination.startswith(_ENEMY_LOCATION_PREFIXES):
        return "ONE_TIME_ENEMY_REWARD", location.destination_kind
    if location.destination_kind in {"RAW_ARTICLE_GRANT", "QUEST_ARTICLE_GRANT"}:
        return ("QUEST_REWARD" if location.destination_kind == "QUEST_ARTICLE_GRANT" else "DIRECT_REWARD"), location.destination_kind
    if location.actor_category == "TREASURE_BOX":
        return "CHEST", "TREASURE_BOX"
    if location.actor_category == "GROUND_PICKUP":
        return "GROUND_PICKUP", "GROUND_PICKUP"
    return "WORLD_LOOT", location.actor_category


# Find world control
def _world_control(location: WorldLootLocation, *, is_field_enemy: bool) -> str:
    if is_field_enemy or (location.destination_id or "").casefold().startswith(_ENEMY_LOCATION_PREFIXES):
        return "randomize_enemy_drops"
    if location.location_type in _WORLD_TYPES:
        return "randomize_healing"
    if _is_renewable_location(location):
        return "randomize_materials"
    return "ALWAYS_ON_WORLD_LOOT"


# Find world pool
def _world_pool(options: GenerationOptions, location: WorldLootLocation, *, is_field_enemy: bool) -> str:
    if _is_renewable_location(location):
        return "INGREDIENTS"
    if options.mix_shops_with_world and len(location.rewards) == 1:
        return "SHOP_WORLD_SHARED"
    return "WORLD"


# Find world exclusion status
def _world_exclusion_status(location: WorldLootLocation, planner_reason: str | None,
                            unmatched: bool, catalogs: RuntimeCatalogs) -> str:
    if _is_renewable_location(location) or planner_reason == "RENEWABLE_INGREDIENT_DISABLED":
        return "DISABLED"
    if unmatched:
        return "UNSUPPORTED"
    if location.source_file.casefold() in catalogs.unsafe_packages or "unsafe" in (planner_reason or "").casefold():
        return "UNSUPPORTED"
    if planner_reason and any(word in planner_reason.upper() for word in ("UNSUPPORTED", "UNVALIDATED", "OPAQUE", "UNKNOWN")):
        return "UNSUPPORTED"
    if planner_reason and "EXTRACTOR_BOOSTER_RESERVED" in planner_reason.upper():
        return "EXCLUDED"
    return "PROTECTED"


# Explain world exclusion reason
def _world_exclusion_reason(location: WorldLootLocation, planner_reason: str | None,
                            unmatched: bool, catalogs: RuntimeCatalogs) -> tuple[str, str]:
    if _is_renewable_location(location) or planner_reason == "RENEWABLE_INGREDIENT_DISABLED":
        return "RENEWABLE_INGREDIENT_DISABLED", "Renewable gathering nodes are parked and do not join World Loot in the current build."
    if unmatched:
        return "UNSUPPORTED_REWARD_STRUCTURE", "The current planner could not balance this reward arity with a compatible source group."
    if location.source_file.casefold() in catalogs.unsafe_packages:
        return "UNSAFE_PACKAGE", "The source package is on the validated unsafe-package exclusion list."
    if planner_reason:
        code = _reason_code(planner_reason)
        return code, planner_reason
    return "UNSUPPORTED_REWARD_STRUCTURE", "The production World Loot planner did not assign this location."


# Find world notes
def _world_notes(location: WorldLootLocation, material: MaterialLocation | None,
                 metadata: dict[str, str] | None, is_field_enemy: bool) -> str:
    notes: list[str] = []
    if location.save_id:
        notes.append(f"UniqueSaveID={location.save_id}")
    if location.mirror_of_package_path:
        notes.append(f"mirror_of={location.mirror_of_package_path}#{location.mirror_of_export_index}")
    if location.scripted_reward_mode:
        notes.append(f"scripted_reward_mode={location.scripted_reward_mode}; destination flags/save identity are preserved")
    if (location.destination_id or "").casefold().startswith("hot_spring_"):
        notes.append("Recipe Articles are one-time unlocks; repeated Hot Spring recipe source entries are replaced with ordinary World Loot.")
    if is_field_enemy:
        notes.append("One-time direct Article reward routed to World Loot only while Enemy Drops is enabled.")
    if material:
        notes.append(f"resource_class={material.resource_class_name}; resource_class_package={material.resource_class_package}")
    if metadata:
        for key in ("enemy_name", "map", "dungeon_area", "reward_source_type", "guaranteed_semantics"):
            value = (metadata.get(key) or "").strip()
            if value:
                notes.append(f"{key}={value}")
    if location.physical_instance_count != 1:
        notes.append(f"physical_instances={location.physical_instance_count}")
    return "; ".join(notes)


# Check renewable location
def _is_renewable_location(location: WorldLootLocation) -> bool:
    return location.location_type == "RENEWABLE_INGREDIENT" or location.destination_kind == "RENEWABLE_INGREDIENT"


# List assigned world slots
def _assigned_world_slots(plan: WorldLootRandomizationPlan) -> set[tuple[str, int]]:
    assigned = {
        _slot_key(item.destination.package_path, item.destination.export_index)
        for item in plan.assignments
    }
    locked = {
        _slot_key(item.location.package_path, item.location.export_index)
        for item in plan.locked_locations
    }
    excluded = {_slot_key(item.package_path, item.export_index) for item in plan.excluded_locations}
    disabled = {_slot_key(item.package_path, item.export_index) for item in plan.disabled_locations}
    unmatched = {_slot_key(item.package_path, item.export_index) for item in plan.unmatched_locations}
    for mirror in plan.mirrored_locations:
        mirror_key = _slot_key(mirror.package_path, mirror.export_index)
        if mirror.mirror_of_package_path is None or mirror.mirror_of_export_index is None:
            continue
        source_key = _slot_key(mirror.mirror_of_package_path, mirror.mirror_of_export_index)
        if source_key in assigned and mirror_key not in locked | excluded | disabled | unmatched:
            assigned.add(mirror_key)
    return assigned


# Read one time enemy context
def _read_one_time_enemy_context() -> tuple[dict[str, str], ...]:
    path = application_data_path("enemy_drops", "one_time_enemy_rewards.csv")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return tuple(dict(row) for row in csv.DictReader(stream))
    except OSError as error:
        raise RuntimeError(f"Could not read the shipped one-time Enemy reward audit: {error}") from error


# Read append quest article grant audit
def _append_quest_article_grant_audit(
    rows: list[dict[str, Any]],
    catalogs: RuntimeCatalogs,
    inventory: PackageInventory | None,
    unavailable: set[str],
    formae: Any,
) -> dict[str, Any]:
    """Mark catalogued quest grants and add visible rows for unmapped grants.

    The audit records every concrete ``QSTask_GainItem`` Article reference
    extracted from the locally inspected quest packages. Unmapped rows are
    diagnostic only: they do not create randomizer source or destination slots.
    """
    audit = _read_quest_article_grants()
    protected_slots, repeatable_slots = _read_quest_reward_classifications()
    by_source: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_source[_quest_grant_row_key(
            str(row.get("package") or ""),
            str(row.get("object_path") or ""),
            str(row.get("reward_index", "")),
            str(row.get("resolved_article") or ""),
        )].append(row)

    matched_rows = 0
    added_rows = 0
    policy = catalogs.important_item_exclusions
    key_identities = policy.key_identities if policy else frozenset()
    quest_identities = policy.quest_flag_identities if policy else frozenset()
    logical_reward_mirrors: dict[tuple[str, int, str], tuple[str, str]] = {}
    supported_quest_grant_keys: set[tuple[str, str, str, str]] = set()
    world_by_destination_id = {
        location.destination_id: location
        for location in catalogs.world_loot
        if location.destination_id is not None
    }
    for location in catalogs.world_loot:
        if location.destination_kind != "QUEST_ARTICLE_GRANT":
            continue
        for layout in location.quest_grant_layouts:
            task_keys = [(location.package_path, layout.export_name)]
            task_keys.extend(
                (location.package_path, mirror.export_name)
                for mirror in layout.mirrored_exports
            )
            for package_path, task_name in task_keys:
                key = (
                    package_path.casefold(), f"{package_path}.{task_name}".casefold(),
                    str(layout.reward_index), layout.expected_article_path.casefold(),
                )
                if key in supported_quest_grant_keys:
                    raise RuntimeError(
                        "A physical quest Article grant is registered by multiple World Loot destinations: "
                        f"{package_path}.{task_name} Article[{layout.reward_index}]."
                    )
                supported_quest_grant_keys.add(key)
    for item in formae.world_loot_direct_rewards:
        if not item.logical_reward_group or not item.world_loot_destination_id:
            continue
        canonical = world_by_destination_id.get(item.world_loot_destination_id)
        if canonical is None:
            continue
        for direct_grant in item.direct_grants:
            grant_package = direct_grant.quest_asset.casefold()
            if (
                grant_package == canonical.package_path.casefold()
                and direct_grant.export_index == canonical.export_index
            ):
                continue
            logical_reward_mirrors[(
                grant_package, direct_grant.export_index, item.asset_path.casefold(),
            )] = (item.logical_reward_group, str(canonical.destination_id))

    for grant in audit["grants"]:
        package = str(grant["package"])
        task_name = str(grant["export_name"])
        article_path = str(grant["article_path"])
        reward_index = str(grant["reward_index"])
        row_key = _quest_grant_row_key(
            package,
            f"{package}.{task_name}",
            reward_index,
            article_path,
        )
        matches = by_source.get(row_key, [])
        if len(matches) > 1:
            raise RuntimeError(
                "Quest Article grant matched multiple inventory rows; refusing to mark an ambiguous source: "
                f"{package}.{task_name} Article[{reward_index}] {article_path}."
            )
        if matches:
            matches[0]["quest_grant_audit"] = True
            matched_rows += 1
            continue
        if row_key in supported_quest_grant_keys:
            # Mirrored QSTask exports are serialized from one logical World Loot
            # destination. The mirror has no separate inventory destination row.
            matched_rows += 1
            continue

        slot_key = (package.casefold(), task_name.casefold(), article_path.casefold())
        logical_mirror = logical_reward_mirrors.get((
            package.casefold(), int(grant["export_index"]), article_path.casefold(),
        ))
        if article_path.casefold() in key_identities:
            status, reason = "PROTECTED", "KEY_ITEM"
            detail = "This exact Article is protected by the key-item policy; the quest grant is listed for audit only."
            is_key, is_quest = True, False
        elif article_path.casefold() in quest_identities or slot_key in protected_slots:
            status, reason = "PROTECTED", "QUEST_ITEM"
            detail = "This exact quest/progression Article is protected; the quest grant is listed for audit only."
            is_key, is_quest = False, True
        elif slot_key in repeatable_slots:
            status, reason = "EXCLUDED", "REPEATABLE_QUEST_REWARD"
            detail = "This is a repeatable multiplayer quest reward, not a one-time quest location."
            is_key, is_quest = False, False
        elif logical_mirror is not None:
            group, canonical_destination_id = logical_mirror
            status, reason = "EXCLUDED", "LOGICAL_REWARD_MIRROR"
            detail = (
                f"This direct grant is another route in logical reward group {group}; "
                f"the randomized item is already represented by {canonical_destination_id}. "
                "It is not an additional item or randomizer slot."
            )
            is_key, is_quest = False, False
        else:
            status, reason = "UNSUPPORTED", "QUEST_GRANT_AUDIT_ONLY"
            detail = (
                "The Article grant was found in a quest package, but this quest task has no registered "
                "randomizer slot. This row is informational and does not randomize the grant. "
                "An Important-style asset name alone does not make the Article a protected quest item."
            )
            is_key = is_quest = False

        package_name = str(grant["package_name_value"])
        package_number = int(grant["package_name_number"])
        asset_name = str(grant["asset_name_value"])
        asset_number = int(grant["asset_name_number"])
        note = (
            "Extracted QSTask_GainItem Article grant; audit scope: "
            f"{audit['quest_packages_inspected']} quest packages, "
            f"{audit['inspection_failures']} inspection failures. "
            f"Serialized Article FNames: package={package_name}[{package_number}], "
            f"asset={asset_name}[{asset_number}], bulk_index={grant['bulk_index']}, "
            f"notification={str(grant['notification']).lower()}."
        )
        row = _new_row(
            category="QUEST_REWARD_AUDIT",
            subcategory="UNMAPPED_QUEST_ARTICLE_GRANT",
            system_owner="QUESTS",
            status=status,
            intrinsically_eligible=False,
            enabled_by_current_config=False,
            exclusion_reason=reason,
            exclusion_detail=detail,
            location_id=(
                f"quest_article_grant:{package}#{grant['export_index']}"
                f":bulk:{grant['bulk_index']}:reward:{reward_index}"
            ),
            source_type="QUEST_TASK_GAIN_ITEM_AUDIT",
            article_name=str(grant["article_asset"]),
            article_literal_fname=asset_name,
            article_fname_number=asset_number,
            resolved_article=article_path,
            package=package,
            object_path=f"{package}.{task_name}",
            actor=task_name,
            actor_class="QSTask_GainItem",
            reward_owner=package,
            reward_index=reward_index,
            count=grant["count"],
            is_key_item=is_key,
            is_quest_item=is_quest,
            is_progression_item=is_key or is_quest,
            is_scripted=True,
            is_one_time=False,
            eligible_as_source=False,
            eligible_as_destination=False,
            randomizer_pool="NONE",
            controlled_by_option="QUEST_GRANT_AUDIT_ONLY",
            notes=_join_notes(note, "Quest grant repeatability is not inferred by this audit."),
            quest_grant_audit=True,
        )
        _set_availability(row, inventory, package, article_path, unavailable)
        rows.append(row)
        added_rows += 1

    if matched_rows + added_rows != audit["article_grant_slots"]:
        raise RuntimeError(
            "Quest Article grant audit did not map every extracted reward slot: "
            f"matched={matched_rows}, added={added_rows}, expected={audit['article_grant_slots']}."
        )
    return {
        "schema": audit["schema"],
        "source_game_version": audit["source_game_version"],
        "quest_packages_inspected": audit["quest_packages_inspected"],
        "packages_with_article_grants": audit["packages_with_article_grants"],
        "article_grant_slots": audit["article_grant_slots"],
        "inspection_failures": audit["inspection_failures"],
        "matched_existing_rows": matched_rows,
        "added_audit_rows": added_rows,
        "scope_note": audit["source_description"],
    }


# Read quest article grants
def _read_quest_article_grants() -> dict[str, Any]:
    path = application_data_path("quest_rewards", "current", "quest_article_grants.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read the quest Article grant audit: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != "cv2-quest-article-grant-audit-v1"
        or document.get("catalog_revision") != "current"
        or document.get("inspection_failures") != 0
        or not isinstance(document.get("grants"), list)
        or document.get("article_grant_slots") != len(document["grants"])
    ):
        raise RuntimeError("The quest Article grant audit is incomplete or has an unsupported schema.")
    seen: set[tuple[str, int, int, int]] = set()
    for grant in document["grants"]:
        try:
            package = grant["package"]
            article_path = grant["article_path"]
            export_name = grant["export_name"]
            export_index = grant["export_index"]
            bulk_index = grant["bulk_index"]
            reward_index = grant["reward_index"]
            count = grant["count"]
            articles_count = grant["articles_count"]
        except (KeyError, TypeError) as error:
            raise RuntimeError("The quest Article grant audit contains a malformed reward record.") from error
        if (
            not isinstance(package, str) or not package.startswith("/Game/Quest/")
            or not isinstance(article_path, str) or not article_path.startswith("/Game/Blueprint/Article/")
            or not isinstance(export_name, str) or not export_name.startswith("QSTask_GainItem")
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                   for value in (export_index, bulk_index, reward_index, articles_count))
            or isinstance(count, bool) or not isinstance(count, int) or count < 1
            or reward_index >= articles_count
        ):
            raise RuntimeError("The quest Article grant audit contains an invalid reward identity or index.")
        key = (package.casefold(), export_index, bulk_index, reward_index)
        if key in seen:
            raise RuntimeError("The quest Article grant audit contains a duplicate task reward slot.")
        seen.add(key)
    if document.get("quest_packages_inspected", 0) < document.get("packages_with_article_grants", 0):
        raise RuntimeError("The quest Article grant audit package counts are inconsistent.")
    return document


# Read quest reward classifications
def _read_quest_reward_classifications() -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    path = application_data_path("enemy_drops", "one_time_enemy_rewards.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        graph = document["quest_graph_audit"]
        protected = {
            (str(item["package"]).casefold(), str(item["task"]).casefold(), str(item["article_path"]).casefold())
            for item in graph["protected_key_quest_progression_slots"]
        }
        repeatable = {
            (str(item["package"]).casefold(), str(item["task"]).casefold(), str(item["article_path"]).casefold())
            for item in graph["repeatable_multiplayer_slots_excluded"]
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(f"Could not read quest reward protection classifications: {error}") from error
    return protected, repeatable


# Build quest grant row key
def _quest_grant_row_key(package: str, object_path: str, reward_index: str,
                         article_path: str) -> tuple[str, str, str, str]:
    normalized_index = "" if reward_index is None else str(reward_index)
    return package.casefold(), object_path.casefold(), normalized_index, article_path.casefold()


# List new row
def _new_row(**values: Any) -> dict[str, Any]:
    row = {field: "" for field in INVENTORY_COLUMNS}
    row.update({
        "category": "OTHER_SUPPORTED", "subcategory": "", "system_owner": "",
        "status": "UNSUPPORTED", "included_in_randomizer": False,
        "intrinsically_eligible": False, "enabled_by_current_config": None,
        "exclusion_reason": "", "exclusion_detail": "", "location_id": "",
        "source_type": "", "display_name": "", "article_name": "",
        "article_literal_fname": "", "article_fname_number": "", "resolved_article": "",
        "content_class": "UNKNOWN", "base_dlc_after_ending_unknown": "UNKNOWN",
        "map": "", "area": "", "package": "", "object_path": "", "actor": "",
        "actor_class": "", "reward_owner": "", "reward_index": "", "count": "",
        "stock": "", "drop_chance": "", "unlock_condition": "", "is_key_item": False,
        "is_quest_item": False, "is_progression_item": False, "is_scripted": False,
        "is_renewable": False, "is_one_time": False, "available_in_installed_game": "",
        "available_in_cache": "", "eligible_as_source": False, "eligible_as_destination": False,
        "randomizer_pool": "NONE", "controlled_by_option": "", "notes": "",
        "quest_grant_audit": False,
    })
    row.update(values)
    row["included_in_randomizer"] = row["status"] == "INCLUDED"
    row["base_dlc_after_ending_unknown"] = row.get("content_class") or "UNKNOWN"
    return row


# Set article protection
def _set_article_protection(row: dict[str, Any], article_path: str,
                            catalogs: RuntimeCatalogs | None) -> None:
    if not article_path:
        return
    row["resolved_article"] = row["resolved_article"] or article_path
    row["article_name"] = row["article_name"] or article_path.rsplit("/", 1)[-1]
    row["is_key_item"] = bool(catalogs and catalogs.important_item_exclusions and article_path.casefold() in catalogs.important_item_exclusions.key_identities)
    row["is_quest_item"] = bool(catalogs and catalogs.important_item_exclusions and article_path.casefold() in catalogs.important_item_exclusions.quest_flag_identities)
    row["is_progression_item"] = row["is_key_item"] or row["is_quest_item"]


# Set availability
def _set_availability(row: dict[str, Any], inventory: PackageInventory | None,
                      package_path: str, article_path: str, unavailable: set[str]) -> None:
    if inventory is None:
        row["available_in_cache"] = ""
        row["available_in_installed_game"] = ""
        return
    packages = [path for path in (package_path, article_path) if path]
    present = all(inventory.contains(path) for path in packages)
    row["available_in_cache"] = present
    row["available_in_installed_game"] = True if present else ""
    if not present:
        absent = [path for path in packages if not inventory.contains(path)]
        if any(path.casefold() in unavailable for path in absent):
            row["available_in_installed_game"] = False


# Read option status
def _option_status(enabled: bool, intrinsically_eligible: bool, control: str) -> tuple[str, str, str]:
    if not intrinsically_eligible:
        return "UNSUPPORTED", "UNSUPPORTED_REWARD_STRUCTURE", "The production catalog does not establish a safe randomizable slot for this source."
    if enabled:
        return "INCLUDED", "", ""
    return "DISABLED", "OPTION_DISABLED", f"Feature control {control} is off in the selected configuration."


# Read option enabled
def _option_enabled(options: GenerationOptions, control: str) -> bool:
    return {
        "ALWAYS_ON_WORLD_LOOT": True,
        "ALWAYS_ON_BEQUEATHED_FORMAE": True,
        "randomize_materials": options.randomize_materials,
        "randomize_healing": options.randomize_healing,
        "randomize_initial_inventory": options.randomize_initial_inventory,
        "randomize_blood_codes": options.randomize_blood_codes,
        "randomize_boosters": options.randomize_boosters,
        "randomize_extractor_recipes": options.randomize_extractor_recipes,
        "randomize_shops": options.randomize_shops,
        "randomize_enemy_drops": options.randomize_enemy_drops,
    }.get(control, False)


# Return reason code
def _reason_code(value: str) -> str:
    normalized = value.strip().upper().replace(" ", "_").replace("-", "_")
    if "KEY" in normalized:
        return "KEY_ITEM"
    if "SCENARIO" in normalized or "SCRIPT" in normalized:
        return "SCRIPTED_FLAG_PICKUP"
    if "QUEST" in normalized or "FLAG" in normalized:
        return "QUEST_ITEM"
    if "UNSAFE" in normalized:
        return "UNSAFE_PACKAGE"
    if "UNPARSED" in normalized or "OPAQUE" in normalized:
        return "UNPARSED_PACKAGE"
    if "STOCK" in normalized and "INFINITE" in normalized:
        return "INFINITE_SHOP_STOCK"
    return normalized or "UNSUPPORTED_REWARD_STRUCTURE"


# Check protection reason
def _is_protection_reason(reason: str) -> bool:
    return any(value in reason.upper() for value in (
        "KEY", "QUEST", "PROGRESSION", "SCRIPT", "FLAG", "MAGMELL_TOOL_SHOP",
    ))


# Build slot key
def _slot_key(package: str, export_index: int) -> tuple[str, int]:
    return package.casefold(), export_index


# Build world sort key
def _world_sort_key(location: WorldLootLocation) -> tuple[str, int, str]:
    return location.package_path.casefold(), location.export_index, location.export_name.casefold()


# Build row sort key
def _row_sort_key(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row["category"]).casefold(),
        str(row["system_owner"]).casefold(),
        str(row["location_id"]).casefold(),
        str(row["article_name"]).casefold(),
        str(row["package"]).casefold(),
        str(row["object_path"]).casefold(),
        str(row["reward_index"]).casefold(),
    )


# Build article summary
def _build_article_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        path = str(row.get("resolved_article") or "").strip()
        if path.startswith("/Game/"):
            grouped[path.casefold()].append(row)
    result: list[dict[str, Any]] = []
    for key, items in sorted(grouped.items()):
        statuses = Counter(str(item["status"]) for item in items)
        exclusion_reasons = sorted({
            str(item["exclusion_reason"]) for item in items if item["exclusion_reason"]
        }, key=str.casefold)
        result.append({
            "article_name": str(items[0]["article_name"] or key.rsplit("/", 1)[-1]),
            "display_name": next((str(item["display_name"]) for item in items if item["display_name"]), ""),
            "content_class": next((str(item["content_class"]) for item in items if item["content_class"] != "UNKNOWN"), "UNKNOWN"),
            "total_locations": len(items),
            "included_locations": statuses["INCLUDED"],
            "excluded_locations": statuses["EXCLUDED"],
            "protected_locations": statuses["PROTECTED"],
            "disabled_locations": statuses["DISABLED"],
            "unsupported_locations": statuses["UNSUPPORTED"],
            "unavailable_locations": statuses["UNAVAILABLE"],
            "categories": ";".join(sorted({str(item["category"]) for item in items}, key=str.casefold)),
            "pools": ";".join(sorted({str(item["randomizer_pool"]) for item in items if item["randomizer_pool"] != "NONE"}, key=str.casefold)),
            "can_appear_as_source": any(item["eligible_as_source"] for item in items),
            "can_appear_as_destination": any(item["eligible_as_destination"] for item in items),
            "exclusion_reasons": ";".join(exclusion_reasons),
        })
    return result


# Build category summary
def _build_category_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        counts[str(row["category"])][str(row["status"]).lower()] += 1
        counts[str(row["category"])]["total"] += 1
    result = [
        {"scope": "CATEGORY", "category": category, **{name: counts[category][name] for name in (
            "total", "included", "excluded", "protected", "disabled", "unsupported", "unavailable",
        )}}
        for category in sorted(counts, key=str.casefold)
    ]
    world_locations = [
        row for row in rows
        if row["source_type"] != "CANONICAL_REWARD_CANDIDATE"
        and (
            "WORLD_LOOT" in str(row["system_owner"]).split(";")
            or (row["system_owner"] == "ENEMY_DROPS" and row["category"] == "ONE_TIME_ENEMY_REWARD")
        )
    ]
    world_counts = Counter(str(row["status"]).lower() for row in world_locations)
    result.append({
        "scope": "SYSTEM_OWNER",
        "category": "WORLD_LOOT",
        **{
            name: (len(world_locations) if name == "total" else world_counts[name])
            for name in ("total", "included", "excluded", "protected", "disabled", "unsupported", "unavailable")
        },
    })
    return result


# Validate known cases
def _validate_known_cases(rows: list[dict[str, Any]], catalogs: RuntimeCatalogs,
                          world_slots: dict[tuple[str, int], dict[str, Any]], options: GenerationOptions) -> dict[str, bool]:
    tool_shop = [
        row for row in rows
        if row["category"] == "SHOP" and row["exclusion_reason"] == "MAGMELL_TOOL_SHOP"
    ]
    infinite_shop = [row for row in rows if row["category"] in {"SHOP", "NPC_TRADER"} and row["exclusion_reason"] == "INFINITE_SHOP_STOCK"]
    renewable = [row for row in rows if row["category"] == "RENEWABLE_INGREDIENT"]
    normal_ground = [row for row in rows if row["category"] == "GROUND_PICKUP" and row["status"] == "INCLUDED"]
    one_time = [row for row in rows if row["category"] == "ONE_TIME_ENEMY_REWARD" and row["source_type"] != "RESPAWNABLE_ENEMY_DROP_ROW" and not row["is_key_item"] and not row["is_quest_item"]]
    key_rows = [row for row in rows if (row["is_key_item"] or row["is_quest_item"]) and row["category"] in {"WORLD_LOOT", "CHEST", "GROUND_PICKUP", "ONE_TIME_ENEMY_REWARD", "QUEST_REWARD", "DIRECT_REWARD"}]
    unsafe_rows = [row for row in rows if row["exclusion_reason"] == "UNSAFE_PACKAGE"]
    npc_eligible = [row for row in rows if row["category"] == "NPC_TRADER" and row["intrinsically_eligible"]]
    blood = [row for row in rows if row["category"] == "BLOOD_CODE"]
    booster = [row for row in rows if row["category"] == "BOOSTER"]
    crafting = [row for row in rows if row["category"] == "BOOSTER_CRAFTING"]
    scenario_reports = [
        row for row in rows
        if row["category"] == "GROUND_PICKUP"
        and row["article_name"] in {"DA_Report_A", "DA_Report_B", "DA_Report_C"}
        and row["package"] in {
            "/Game/Map/MainWorld/MainWorld/_Generated_/9LFQFB2HAT6FVDAA311S0L2ZD",
            "/Game/Map/MainWorld/DlcWorld/_Generated_/AJH345RIDQ5AW61DCGZIYS6DT",
        }
    ]
    world_direct = [row for row in one_time if row["status"] == "INCLUDED" and row["randomizer_pool"] in {"WORLD", "SHOP_WORLD_SHARED"}]
    one_time_gate_valid = bool(one_time) and all(
        row["status"] == ("INCLUDED" if options.randomize_enemy_drops else "DISABLED")
        for row in one_time
        if row["status"] not in {"UNAVAILABLE", "UNSUPPORTED", "PROTECTED", "EXCLUDED"}
    )
    return {
        "tool_shop_reason_correct": bool(tool_shop) and all(row["status"] == "PROTECTED" for row in tool_shop),
        "infinite_shop_reason_correct": bool(infinite_shop) and all(row["status"] == "EXCLUDED" for row in infinite_shop),
        "renewable_ingredients_use_separate_pool": bool(renewable) and all(
            row["system_owner"] == "MATERIALS"
            and row["randomizer_pool"] == "INGREDIENTS"
            and row["controlled_by_option"] == "randomize_materials"
            and row["status"] == (
                "UNAVAILABLE" if row["available_in_cache"] is False
                else "INCLUDED" if options.randomize_materials
                else "DISABLED"
            )
            for row in renewable
        ),
        "normal_ground_pickup_included": bool(normal_ground),
        "one_time_ordinary_reward_included_in_world": bool(world_direct) if options.randomize_enemy_drops else one_time_gate_valid,
        "key_and_quest_items_retained_with_protection": bool(key_rows) and all(
            row["status"] != "INCLUDED"
            and not row["included_in_randomizer"]
            and not row["eligible_as_source"]
            and not row["eligible_as_destination"]
            and row["exclusion_reason"] in {"KEY_ITEM", "QUEST_ITEM", "UNSAFE_PACKAGE", "UNSUPPORTED_REWARD_STRUCTURE", "OPTIONAL_CONTENT_ABSENT"}
            for row in key_rows
        ),
        "unsupported_unsafe_rows_retained": bool(unsafe_rows) and all(row["status"] == "UNSUPPORTED" for row in unsafe_rows),
        "npc_trader_slots_classified": bool(npc_eligible) and all(
            row["status"] in ({"INCLUDED", "DISABLED"} if options.randomize_shops else {"DISABLED"})
            for row in npc_eligible
        ),
        "blood_codes_use_separate_pool": bool(blood) and all(row["randomizer_pool"] == "BLOOD_CODE_ONLY" for row in blood),
        "booster_mastery_is_separate_from_crafting": bool(booster) and bool(crafting) and all(row["randomizer_pool"] == "BOOSTER_ONLY" for row in booster) and all(row["randomizer_pool"] == "BOOSTER_CRAFTING_ONLY" for row in crafting),
        "scenario_report_pickups_randomized": len(scenario_reports) == 6 and all(
            row["status"] in {"INCLUDED", "DISABLED"}
            and "ARTICLE_ONLY_PRESERVE_SCENARIO" in row["notes"]
            for row in scenario_reports
        ),
        "uses_production_catalogs_and_planner": True,
        "deterministic_sort_order": rows == sorted(rows, key=_row_sort_key),
    }


# Parse csv bool
def _csv_bool(value: Any) -> bool:
    return str(value).strip().casefold() in {"true", "1", "yes"}


# Parse int or none
def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


# Join notes
def _join_notes(*values: str) -> str:
    return "; ".join(value for value in values if value)


# Count csv bytes
def _csv_bytes(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bytes:
    import io
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_value(row.get(key, "")) for key in columns})
    return stream.getvalue().encode("utf-8-sig")


# List csv value
def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# Count JSON bytes
def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# Read atomic write set
def _atomic_write_set(output_dir: Path, payloads: dict[str, bytes]) -> None:
    temporary_root: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=".cv2-inventory-", dir=output_dir) as name:
            temporary_root = Path(name)
            for filename, payload in payloads.items():
                path = temporary_root / filename
                path.write_bytes(payload)
            for filename in payloads:
                os.replace(temporary_root / filename, output_dir / filename)
    except OSError:
        if temporary_root is not None:
            # TemporaryDirectory owns cleanup; this branch only preserves the
            # original write/replace failure for the caller.
            pass
        raise


# Set reward identity
def _set_reward_identity(row: dict[str, Any], identity: SoftObjectPathIdentity | None) -> None:
    if identity is None:
        return
    row["article_literal_fname"] = identity.asset_name.value
    row["article_fname_number"] = identity.asset_name.number
