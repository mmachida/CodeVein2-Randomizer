"""Canonical generation fingerprints and assignment-manifest serialization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cv2randomizer import __version__
from cv2randomizer.core.determinism import (
    RANDOMIZER_ALGORITHM_VERSION,
    canonical_package_identity,
    canonical_package_key,
    canonical_sort_key,
)
from cv2randomizer.core.models import GenerationOptions
from cv2randomizer.core.resources import application_data_path
from cv2randomizer.core.seed import NormalizedSeed
from cv2randomizer.game.content_availability import ContentAvailability
from cv2randomizer.randomizer.shops import is_npc_trader_row


_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_CACHE_IDENTITY_FIELDS = (
    "cache_format_version",
    "cache_revision",
    "game_version",
    "game_executable_sha256",
    "game_archive_sha256",
    "catalog_revision",
    "usmap_sha256",
    "source_registry_revision",
    "world_loot_descriptor_revision",
    "world_loot_preparation_revision",
    "uassetapi_helper_sha256",
)


@dataclass(frozen=True, slots=True)
class ReproducibilityMetadata:
    algorithm_version: int
    config_fingerprint: str
    content_fingerprint: str
    assignment_manifest_sha256: str
    document: dict[str, object]

    # Find spoiler header
    def spoiler_header(self) -> tuple[tuple[str, str], ...]:
        return (
            ("App Version", f"v{__version__}"),
            ("Game Build", str(self.document["game_build"])),
            ("Randomizer Algorithm", f"v{self.algorithm_version}"),
            ("Config Fingerprint", self.config_fingerprint),
            ("Content Fingerprint", self.content_fingerprint),
            ("Assignment Manifest SHA-256", self.assignment_manifest_sha256),
        )


# Count canonical JSON bytes
def canonical_json_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


# Calculate the source fingerprint
def fingerprint(document: object) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


# Find canonical seed text
def canonical_seed_text(seed: NormalizedSeed) -> str:
    if _INTEGER.fullmatch(seed.display_value.strip()):
        return str(int(seed.display_value.strip(), 10))
    return seed.display_value


# Find effective options document
def effective_options_document(options: GenerationOptions) -> dict[str, bool | str]:
    """Only generation-affecting settings; omit install and presentation state."""
    return {
        "bequeathed_formae_direct_rewards": options.randomize_bequeathed_formae_quest_rewards,
        "blood_codes": options.randomize_blood_codes,
        "booster_crafting": options.randomize_extractor_recipes,
        "cheaper_booster_extractor": options.cheaper_booster_extractor,
        "low_blood_code_mastery_exp": options.low_blood_code_mastery_exp,
        "boosters": options.randomize_boosters,
        "defensive_gadget_npc_rewards": options.randomize_defensive_gadget_npc_rewards,
        "enemy_drops": options.randomize_enemy_drops,
        "healing": options.randomize_healing,
        "initial_inventory": options.randomize_initial_inventory,
        "materials": options.randomize_materials,
        "npc_traders": options.randomize_npc_traders,
        "postgame_dlc_with_base": options.mix_postgame_dlc_with_base,
        "shop_world_mix": options.mix_shops_with_world,
        "shops": options.randomize_shops,
        "world_loot": options.randomize_world_loot,
    }


# Find runtime data inputs
def _runtime_data_inputs(catalog_revision: str) -> list[dict[str, object]]:
    """Hash every shipped data file that can affect eligibility or planning.

    This intentionally includes exclusions, version definitions, content
    classifications, and catalogs rather than maintaining a fragile allowlist.
    Only data-root-relative names and file contents enter the fingerprint.
    """
    root = application_data_path()
    if not root.is_dir():
        return [{"missing_data_root": True, "catalog_revision": catalog_revision}]
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: canonical_package_key(path.relative_to(root).as_posix()),
    )
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in files
    ]


# Parse SHA-256 file
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Read availability inputs
def _availability_inputs(availability: ContentAvailability | None) -> list[dict[str, str]]:
    if availability is None:
        return []
    rows = [
        {
            "subsystem": source.subsystem,
            "package": canonical_package_identity(source.package_path),
            "state": str(source.state),
        }
        for source in availability.sources
    ]
    return sorted(rows, key=lambda row: (
        canonical_sort_key(row["subsystem"]),
        canonical_package_key(row["package"]),
        canonical_sort_key(row["state"]),
    ))


# Cache content inputs
def _cache_content_inputs(metadata: Mapping[str, Any] | None) -> dict[str, object]:
    if metadata is None:
        return {}
    identity = {
        key: metadata[key]
        for key in _CACHE_IDENTITY_FIELDS
        if key in metadata and isinstance(metadata[key], (str, int, bool, type(None)))
    }
    inventory = metadata.get("extracted_package_inventory", [])
    normalized_inventory = sorted(
        {
            canonical_package_identity(value)
            for value in inventory
            if isinstance(value, str)
        },
        key=canonical_package_key,
    ) if isinstance(inventory, list) else []
    package_files = metadata.get("package_files", {})
    package_assets: list[dict[str, object]] = []
    if isinstance(package_files, dict):
        for package, raw_files in sorted(package_files.items(), key=lambda item: canonical_package_key(str(item[0]))):
            if not isinstance(package, str) or not isinstance(raw_files, list):
                continue
            files = [
                {
                    "name": canonical_package_identity(str(row["name"])),
                    "size": row["size"],
                    "sha256": str(row["sha256"]).casefold(),
                }
                for row in raw_files
                if isinstance(row, dict)
                and isinstance(row.get("name"), str)
                and isinstance(row.get("size"), int)
                and isinstance(row.get("sha256"), str)
            ]
            files.sort(key=lambda row: (canonical_sort_key(str(row["name"])), str(row["sha256"])))
            package_assets.append({
                "package": canonical_package_identity(package),
                "files": files,
            })
    return {
        "identity": identity,
        "available_packages": normalized_inventory,
        "package_assets": package_assets,
    }


# List article record
def _article_record(
    system: str,
    location_id: str,
    vanilla: str,
    assigned: str,
    *,
    count: int | None = None,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "system": system,
        "location_id": location_id,
        "vanilla_article": vanilla,
        "assigned_article": assigned,
    }
    if count is not None:
        row["count"] = count
    if extra:
        row.update(extra)
    return row


# Resolve shop path
def _shop_path(product: object) -> str:
    value = str(getattr(product, "article_path"))
    number = int(getattr(product, "article_path_number", 0))
    return value if number == 0 else f"{value}_{number - 1}"


# List assignment records
def _assignment_records(
    *,
    world_plan: object,
    shop_plan: object,
    enemy_drop_plan: object | None,
    blood_code_plan: object | None,
    booster_plan: object | None,
    extractor_plan: object | None,
    initial_inventory_plan: object | None,
    defensive_gadget_plan: object | None,
    bequeathed_formae_plan: object | None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for assignment in getattr(world_plan, "assignments", ()):
        destination = assignment.destination
        system = (
            "Healing"
            if destination.location_type in {
                "HEALING_UPGRADE_GOLDEN_SEED",
                "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
            }
            else "World Loot"
        )
        location_id = f"{destination.package_path}#{destination.export_index}"
        for reward_index, (vanilla, assigned) in enumerate(zip(
            destination.rewards, assignment.assigned_rewards, strict=True,
        )):
            records.append(_article_record(
                system,
                f"{location_id}#reward-{reward_index}",
                vanilla.article_path,
                assigned.article_path,
                count=assigned.count,
                extra={"notification": assigned.notification},
            ))

    seen_shop_slots: set[tuple[str, str, int]] = set()
    shop_assignments = list(getattr(shop_plan, "assignments", ()))
    trader_summary = getattr(shop_plan, "npc_traders", None)
    if trader_summary is not None:
        shop_assignments.extend(getattr(trader_summary, "assignments", ()))
    for assignment in shop_assignments:
        destination = assignment.destination
        key = (destination.family.upper(), destination.shop_row, destination.product_index)
        if key in seen_shop_slots:
            continue
        seen_shop_slots.add(key)
        system = "NPC Traders" if is_npc_trader_row(destination.family, destination.shop_row) else "Shops"
        source = assignment.source
        assigned_path = (
            source.canonical_path
            if hasattr(source, "canonical_path")
            else _shop_path(source)
        )
        records.append(_article_record(
            system,
            f"{destination.family.upper()}:{destination.shop_row}:{destination.product_index}",
            _shop_path(destination),
            assigned_path,
            extra={
                "price": destination.price,
                "uses_stock": destination.logical_uses_stock,
                "stock": destination.stock,
            },
        ))

    # Auxiliary edits are serialized outside the permutation pool and do not
    # count as a second source occurrence.
    for assignment in getattr(shop_plan, "linked_assignments", ()):
        destination = assignment.destination
        source = assignment.source
        linked_to = (
            {
                "LINKED_INITIAL_INVENTORY": "Randomized starting Jail in Initial Inventory",
                "LINKED_WORLD_REWARD": "Josee's Outpost Stinger World Loot reward",
            }.get(getattr(source, "origin_kind", None), "Additional linked shop route")
        )
        records.append(_article_record(
            "Linked Alternate Route",
            f"{destination.family.upper()}:{destination.shop_row}:{destination.product_index}",
            _shop_path(destination),
            source.canonical_path if hasattr(source, "canonical_path") else _shop_path(source),
            extra={"linked_to": linked_to},
        ))

    if enemy_drop_plan is not None:
        for assignment in getattr(enemy_drop_plan, "assignments", ()):
            destination = assignment.destination
            source = assignment.source
            records.append(_article_record(
                "Enemy Drops",
                f"{destination.table}:{destination.row_id}:{destination.slot_index}",
                destination.bundle_fingerprint,
                source.bundle_fingerprint,
                extra={"reward_labels": list(source.reward_labels)},
            ))

    if blood_code_plan is not None:
        for assignment in getattr(blood_code_plan, "assignments", ()):
            records.append(_article_record(
                "Blood Codes",
                assignment.destination.slot_id,
                assignment.destination.original_blood_code_package,
                assignment.source.original_blood_code_package,
            ))

    if booster_plan is not None:
        for assignment in getattr(booster_plan, "assignments", ()):
            records.append(_article_record(
                "Boosters",
                f"{assignment.destination.row_index}:{assignment.destination.row_name}",
                assignment.destination.original_reward.article_path,
                assignment.assigned_reward.article_path,
                count=assignment.destination.count,
                extra={
                    "vanilla_story_flag": assignment.destination.original_reward.story_flag,
                    "assigned_story_flag": assignment.assigned_reward.story_flag,
                    "notification": assignment.destination.b_notification,
                },
            ))

    if extractor_plan is not None:
        for assignment in getattr(extractor_plan, "assignments", ()):
            records.append(_article_record(
                "Booster Crafting",
                f"{assignment.destination.virtual_package}#{assignment.destination.row_index}",
                assignment.destination.original_article_path,
                assignment.assigned_reward.article_path,
            ))

    if initial_inventory_plan is not None:
        for assignment in getattr(initial_inventory_plan, "assignments", ()):
            records.append(_article_record(
                "Initial Inventory",
                assignment.slot_id,
                assignment.vanilla_article_path,
                assignment.assigned_article_path,
                count=1,
            ))

    if defensive_gadget_plan is not None:
        for assignment in getattr(defensive_gadget_plan, "assignments", ()):
            records.append(_article_record(
                "Defensive Gadget NPC Rewards",
                assignment.destination.slot_id,
                assignment.original.asset_path,
                assignment.assigned.asset_path,
                count=1,
            ))

    if bequeathed_formae_plan is not None:
        for assignment in getattr(bequeathed_formae_plan, "assignments", ()):
            destination = assignment.destination
            records.append(_article_record(
                "Bequeathed Formae",
                destination.logical_reward_group or destination.object,
                destination.asset_path,
                assignment.source.asset_path,
            ))

    records.sort(key=lambda row: (
        canonical_sort_key(str(row["system"])),
        canonical_package_key(str(row["location_id"])),
        canonical_package_key(str(row["vanilla_article"])),
    ))
    return records


# Build reproducibility metadata
def build_reproducibility_metadata(
    *,
    seed: NormalizedSeed,
    game_version: str,
    catalog_revision: str,
    options: GenerationOptions,
    content_availability: ContentAvailability | None,
    source_cache_metadata: Mapping[str, Any] | None,
    world_plan: object,
    shop_plan: object,
    enemy_drop_plan: object | None = None,
    blood_code_plan: object | None = None,
    booster_plan: object | None = None,
    extractor_plan: object | None = None,
    initial_inventory_plan: object | None = None,
    defensive_gadget_plan: object | None = None,
    bequeathed_formae_plan: object | None = None,
) -> ReproducibilityMetadata:
    effective_options = effective_options_document(options)
    config_fingerprint = fingerprint(effective_options)
    content_document = {
        "app_version": __version__,
        "game_build": game_version,
        "catalog_revision": catalog_revision,
        "runtime_data_resources": _runtime_data_inputs(catalog_revision),
        "content_availability": _availability_inputs(content_availability),
        "vanilla_cache": _cache_content_inputs(source_cache_metadata),
    }
    content_fingerprint = fingerprint(content_document)
    document: dict[str, object] = {
        "schema": "cv2-randomizer-assignment-manifest-v1",
        "app_version": __version__,
        "algorithm_version": RANDOMIZER_ALGORITHM_VERSION,
        "seed": canonical_seed_text(seed),
        "game_build": game_version,
        "catalog_revision": catalog_revision,
        "config_fingerprint": config_fingerprint,
        "content_fingerprint": content_fingerprint,
        "effective_options": effective_options,
        "assignments": _assignment_records(
            world_plan=world_plan,
            shop_plan=shop_plan,
            enemy_drop_plan=enemy_drop_plan,
            blood_code_plan=blood_code_plan,
            booster_plan=booster_plan,
            extractor_plan=extractor_plan,
            initial_inventory_plan=initial_inventory_plan,
            defensive_gadget_plan=defensive_gadget_plan,
            bequeathed_formae_plan=bequeathed_formae_plan,
        ),
    }
    digest = fingerprint(document)
    return ReproducibilityMetadata(
        RANDOMIZER_ALGORITHM_VERSION,
        config_fingerprint,
        content_fingerprint,
        digest,
        document,
    )


# Write assignment manifest
def write_assignment_manifest(path: Path, metadata: ReproducibilityMetadata) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(metadata.document))
    return path
