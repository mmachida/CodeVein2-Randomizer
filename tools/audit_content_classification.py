"""Audit class coverage against exact installed IoStore package provenance."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cv2randomizer.game.asset_extraction import AssetExtractionError, IoStoreAssetExtractor  # noqa: E402
from cv2randomizer.game.content_availability import (  # noqa: E402
    PackageInventory,
)
from cv2randomizer.game.content_classification import (  # noqa: E402
    ExplicitArticleClassIndex,
    article_class_counts,
    build_explicit_article_class_index,
    classify_package_content,
    classify_shop_where,
)
from cv2randomizer.game.field_enemy_catalog import FieldEnemyCatalogStore  # noqa: E402
from cv2randomizer.game.catalogs import CatalogStore  # noqa: E402
from cv2randomizer.game.blood_code_catalog import BloodCodeCatalogStore  # noqa: E402
from cv2randomizer.game.enemy_drop_generation import ENEMY_DROP_TABLES  # noqa: E402
from cv2randomizer.game.enemy_drop_catalog import EnemyDropCatalogStore  # noqa: E402
from cv2randomizer.game.extractor_catalog import ExtractorCatalogStore  # noqa: E402
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTENT_CLASSES = ("BASE", "DLC", "AFTER_ENDING", "UNKNOWN")


# Return class counts
def _class_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in CONTENT_CLASSES}
    for record in records:
        value = str(record.get("content_class", "UNKNOWN")).upper()
        counts[value if value in counts else "UNKNOWN"] += 1
    return counts


# Resolve article package path
def _article_package_path(article_path: str, inventory: PackageInventory) -> str:
    """Resolve an object-name suffix to a package only when Retoc lists that package."""
    resolved, _candidates = inventory.resolve_package_path(article_path)
    return resolved or article_path


# Load rules
def _load_rules(path: Path) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read classification rules {path.name}: {error}.") from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != "cv2-package-content-classification-v1"
        or not isinstance(document.get("container_classes"), dict)
        or not isinstance(document.get("package_overrides"), list)
    ):
        raise ValueError("Package classification rules have an invalid schema.")
    container_classes = document["container_classes"]
    package_overrides: dict[str, tuple[str, str]] = {}
    for item in document["package_overrides"]:
        if not isinstance(item, dict):
            raise ValueError("Package overrides must be objects.")
        package_path = item.get("virtual_package_path")
        content_class = item.get("content_class")
        evidence = item.get("evidence")
        if not all(isinstance(value, str) and value.strip() for value in (package_path, content_class, evidence)):
            raise ValueError("Package overrides require path, class, and evidence strings.")
        if package_path.casefold() in {value.casefold() for value in package_overrides}:
            raise ValueError(f"Duplicate package classification override: {package_path}.")
        package_overrides[package_path] = (content_class, evidence)
    return container_classes, package_overrides


# Build package result
def _package_result(
    package_path: str,
    inventory: PackageInventory,
    *,
    container_classes: dict[str, str],
    package_overrides: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    inventory_path = _article_package_path(package_path, inventory)
    _resolved_path, package_candidates = inventory.resolve_package_path(package_path)
    result = classify_package_content(
        inventory_path,
        inventory,
        package_overrides=package_overrides,
        container_classes=container_classes,
    )
    return {
        "virtual_package_path": package_path,
        "inventory_package_path": inventory_path,
        "package_resolution_candidates": list(package_candidates),
        "source_containers": list(result.source_containers),
        "package_content_class": result.content_class,
        "package_classification_source": result.classification_source,
        "package_audit_status": result.audit_status,
        "package_evidence": result.evidence,
    }


# List resolved record
def _resolved_record(
    *,
    category: str,
    identity: str,
    package_path: str,
    existing_class: str,
    existing_source: str,
    inventory: PackageInventory,
    article_index: ExplicitArticleClassIndex,
    container_classes: dict[str, str],
    package_overrides: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    content_class = existing_class.strip().upper() if isinstance(existing_class, str) else "UNKNOWN"
    classification_source = existing_source
    evidence: list[str] = []
    if content_class != "UNKNOWN":
        audit_status = "CLASSIFIED"
        evidence.append("The runtime catalog explicitly records this class.")
    else:
        article = article_index.get(identity) if identity.startswith("/Game/Blueprint/Article/") else None
        if article is not None:
            content_class = article.content_class
            classification_source = "EXPLICIT_CATALOG"
            audit_status = "CLASSIFIED"
            evidence.extend(article.evidence)
        else:
            package_info = _package_result(
                package_path,
                inventory,
                container_classes=container_classes,
                package_overrides=package_overrides,
            )
            content_class = package_info["package_content_class"]
            classification_source = package_info["package_classification_source"]
            audit_status = package_info["package_audit_status"]
            if package_info.get("package_evidence"):
                evidence.append(package_info["package_evidence"])
    package_info = _package_result(
        package_path,
        inventory,
        container_classes=container_classes,
        package_overrides=package_overrides,
    )
    return {
        "category": category,
        "identity": identity,
        "virtual_package_path": package_path,
        "content_class": content_class if content_class in CONTENT_CLASSES else "UNKNOWN",
        "classification_source": classification_source,
        "audit_status": audit_status,
        "evidence": evidence,
        **package_info,
    }


# Read DLC package request audit
def _dlc_package_request_audit(
    *,
    revision: str,
    catalogs: Any,
    field_enemy: Any,
) -> dict[str, Any]:
    """Count the DLC-only package set for an all-DLC-features-selected profile.

    Quest packages are potential patch inputs: the real generator skips extraction
    for unchanged direct grants. Other feature package inputs are inspected before
    planning. The FieldEnemy spawn DataTable used only as a detection marker is
    deliberately not counted as a generation input.
    """
    blood_codes = BloodCodeCatalogStore(PROJECT_ROOT / "data" / "blood_codes" / "current").load(revision)
    extractor = ExtractorCatalogStore(PROJECT_ROOT / "data" / "extractor" / "current").load(revision)
    dlc_shop_families = {
        product.family.upper() for product in catalogs.shops
        if product.family.upper() in {"DLC", "TRADE_DLC"}
    }
    by_blood_package = blood_codes.by_package()
    groups: dict[str, list[str]] = {
        "shop_tables": [
            f"/Game/Blueprint/GameSystem/Shop/{SHOP_TABLE_NAMES[family]}"
            for family in ("DLC", "TRADE_DLC") if family in dlc_shop_families
        ],
        "enemy_drop_tables": [virtual for content_class, virtual in ENEMY_DROP_TABLES if content_class == "DLC"],
        "extractor_recipe_tables": [table.virtual_package for table in extractor.tables if table.content_class == "DLC"],
        "blood_code_dlc_only_quests": [
            package for package, slots in by_blood_package.items()
            if slots and all(slot.content_class == "DLC" for slot in slots)
        ],
        "field_enemy_quests": [
            location.package_path for location in field_enemy.locations
            if location.content_class.strip().upper() == "DLC"
        ],
        "world_loot_dlc_destinations": [
            location.package_path for location in catalogs.world_loot
            if location.content_class.strip().upper() == "DLC"
        ],
    }
    groups = {
        key: sorted(set(values), key=str.casefold)
        for key, values in groups.items()
    }
    all_paths = {path.casefold(): path for values in groups.values() for path in values}
    return {
        "profile": "all DLC-bearing randomizer features selected; catalogued DLC WHERE packages included",
        "dlc_off_dlc_only_required_package_count": 0,
        "dlc_on_candidate_package_count": len(all_paths),
        "dlc_on_candidate_package_count_is_not_a_per_run_total": True,
        "notes": [
            "The actual request set depends on enabled features and shop sub-options.",
            "World Loot and FieldEnemy quest packages are extracted only when an assignment changes their reward.",
            "The field-enemy spawn DataTable marker is inspected for availability but is not a generation input.",
        ],
        "packages_by_feature": {
            key: {"count": len(values), "virtual_package_paths": values}
            for key, values in groups.items()
        },
        "unique_candidate_virtual_package_paths": sorted(all_paths.values(), key=str.casefold),
    }


# Audit content classification
def audit_content_classification(
    *,
    game_root: Path,
    revision: str,
    output_dir: Path,
    extractor: IoStoreAssetExtractor | None = None,
) -> tuple[Path, Path]:
    data_root = PROJECT_ROOT / "data"
    marker_path = data_root / "content_availability" / f"{revision}.json"
    marker_document = json.loads(marker_path.read_text(encoding="utf-8"))
    package_markers = tuple(marker_document.get("package_markers", ()))
    container_markers = tuple(marker_document.get("container_markers", ()))
    inventory = (extractor or IoStoreAssetExtractor()).inspect_package_inventory(game_root)
    catalogs = CatalogStore(data_root / "catalogs").load(revision)
    enemy_drop_catalog = EnemyDropCatalogStore(data_root / "enemy_drops").load(revision)
    field_enemy = FieldEnemyCatalogStore(data_root / "field_enemy").load(
        revision,
        important_item_exclusions=catalogs.important_item_exclusions,
    )
    article_index = build_explicit_article_class_index(data_root)
    if article_index.conflicts:
        raise ValueError("Conflicting explicit canonical Article classifications exist; audit stopped fail-closed.")
    container_classes, package_overrides = _load_rules(
        data_root / "content_classification" / revision / "rules.json",
    )

    records_by_category: dict[str, list[dict[str, Any]]] = {
        "world_loot_where": [],
        "world_loot_what": [],
        "materials_where": [],
        "materials_resource": [],
        "materials_reward": [],
        "materials_article": [],
        "shops_where": [],
        "shops_what": [],
        "field_enemy_where": [],
        "field_enemy_what": [],
    }
    for location in catalogs.world_loot:
        records_by_category["world_loot_where"].append(_resolved_record(
            category="world_loot_where",
            identity=location.package_path,
            package_path=location.package_path,
            existing_class=location.content_class,
            existing_source="EXPLICIT_CATALOG",
            inventory=inventory,
            article_index=article_index,
            container_classes=container_classes,
            package_overrides=package_overrides,
        ))
        for reward_index, reward in enumerate(location.rewards):
            records_by_category["world_loot_what"].append(_resolved_record(
                category="world_loot_what",
                identity=reward.article_path,
                package_path=reward.article_path,
                existing_class=reward.content_class,
                existing_source="EXPLICIT_CATALOG",
                inventory=inventory,
                article_index=article_index,
                container_classes=container_classes,
                package_overrides=package_overrides,
            ) | {"location_package": location.package_path, "reward_index": reward_index})

    for location in catalogs.materials:
        records_by_category["materials_where"].append(_resolved_record(
            category="materials_where", identity=location.package_path,
            package_path=location.package_path, existing_class=location.where_content_class,
            existing_source="EXPLICIT_CATALOG", inventory=inventory, article_index=article_index,
            container_classes=container_classes, package_overrides=package_overrides,
        ) | {"export_index": location.export_index, "save_id": location.save_id})
        records_by_category["materials_resource"].append(_resolved_record(
            category="materials_resource", identity=location.resource_class_package,
            package_path=location.resource_class_package, existing_class=location.resource_content_class,
            existing_source="EXPLICIT_CATALOG", inventory=inventory, article_index=article_index,
            container_classes=container_classes, package_overrides=package_overrides,
        ) | {"resource_class_name": location.resource_class_name})
        records_by_category["materials_reward"].append(_resolved_record(
            category="materials_reward", identity=location.reward.article_path,
            package_path=location.reward.article_path, existing_class=location.reward_content_class,
            existing_source="EXPLICIT_CATALOG", inventory=inventory, article_index=article_index,
            container_classes=container_classes, package_overrides=package_overrides,
        ) | {"export_index": location.export_index, "save_id": location.save_id})
        records_by_category["materials_article"].append(_resolved_record(
            category="materials_article", identity=location.reward.article_path,
            package_path=location.reward.article_path, existing_class=location.reward.content_class,
            existing_source="EXPLICIT_CATALOG", inventory=inventory, article_index=article_index,
            container_classes=container_classes, package_overrides=package_overrides,
        ) | {"export_index": location.export_index, "save_id": location.save_id})

    for product in catalogs.shops:
        where = classify_shop_where(product.family)
        records_by_category["shops_where"].append({
            "category": "shops_where", "identity": f"{product.family}:{product.shop_row}:{product.product_index}",
            "virtual_package_path": where.virtual_package_path, "content_class": where.content_class,
            "classification_source": where.classification_source, "audit_status": where.audit_status,
            "evidence": [where.evidence] if where.evidence else [],
            "source_containers": [], "package_audit_status": where.audit_status,
        })
        records_by_category["shops_what"].append(_resolved_record(
            category="shops_what", identity=product.original_product_path,
            package_path=product.original_product_path, existing_class=product.what_content_class,
            existing_source="EXPLICIT_CATALOG", inventory=inventory, article_index=article_index,
            container_classes=container_classes, package_overrides=package_overrides,
        ) | {"family": product.family, "shop_row": product.shop_row, "product_index": product.product_index})

    for location in field_enemy.locations:
        records_by_category["field_enemy_where"].append(_resolved_record(
            category="field_enemy_where", identity=str(location.destination_id),
            package_path=location.package_path, existing_class=location.content_class,
            existing_source="EXPLICIT_CATALOG", inventory=inventory, article_index=article_index,
            container_classes=container_classes, package_overrides=package_overrides,
        ))
        reward = location.rewards[0]
        records_by_category["field_enemy_what"].append(_resolved_record(
            category="field_enemy_what", identity=reward.article_path,
            package_path=reward.article_path, existing_class=reward.content_class,
            existing_source="EXPLICIT_CATALOG", inventory=inventory, article_index=article_index,
            container_classes=container_classes, package_overrides=package_overrides,
        ) | {"location_package": location.package_path, "destination_id": location.destination_id})

    unknown_entries = {
        category: [item for item in records if item["content_class"] == "UNKNOWN"]
        for category, records in records_by_category.items()
    }
    distributions = {
        category: _class_counts(records)
        for category, records in records_by_category.items()
    }
    pak_directory = game_root.expanduser().resolve() / "CodeVein2" / "Content" / "Paks"
    container_names = sorted(
        (path.name for path in pak_directory.iterdir() if path.is_file() and path.suffix.casefold() == ".utoc"),
        key=str.casefold,
    )
    package_counts = {
        name: sum(1 for _path, owners in inventory.package_containers if any(owner.casefold() == name.casefold() for owner in owners))
        for name in container_names
    }
    marker_evidence = [
        {
            "package_path": marker,
            "present": marker.casefold() in inventory.package_paths,
            "source_containers": list(inventory.containers_for(marker)),
        }
        for marker in package_markers
    ]
    report = {
        "schema": "cv2-content-classification-audit-v1",
        "catalog_revision": revision,
        "installation_binding": "local_inventory_observation; not an official supported-version signature",
        "classification_methodology": {
            "accepted_evidence": [
                "exact reviewed runtime-catalog content_class",
                "exact canonical Article identity in a curated classed catalog",
                "exact installed package ownership only when every owner has a reviewed container class",
                "exact package override in the revision-bound rules file",
                "reviewed shop DataTable family",
            ],
            "pathname_or_name_heuristics_used": False,
            "unknown_policy": "UNKNOWN is classification metadata and does not block a supported entry when its source package exists.",
            "unknown_status_legend": {
                "EXHAUSTIVE_UNRESOLVED": "Exact catalog identity and installed package/container ownership were checked, but no reviewed class evidence resolves the class.",
                "NOT_IN_INSTALLED_INVENTORY": "The exact package and unique numbered-package resolution are absent from this installation inventory.",
            },
        },
        "explicit_canonical_article_class_counts": article_class_counts(article_index),
        "explicit_canonical_article_identity_count": len(article_index.entries),
        "content_class_counts": distributions,
        "unknown_counts": {category: len(rows) for category, rows in unknown_entries.items()},
        "unknown_status_counts": {
            category: dict(Counter(item["audit_status"] for item in rows))
            for category, rows in unknown_entries.items()
        },
        "unknown_entries": unknown_entries,
        "known_category_counts": {
            "enemy_drops": {
                table: {
                    "eligible_rows": sum(
                        row.table == table and row.classification == "NORMAL_ONLY"
                        for row in enemy_drop_catalog.rows
                    ),
                    "eligible_slots": sum(
                        row.expected_slots for row in enemy_drop_catalog.rows
                        if row.table == table and row.classification == "NORMAL_ONLY"
                    ),
                }
                for table in ("BASE", "AFTER_ENDING", "DLC")
            },
            "extractor": {"BASE": 36, "DLC": 7, "TOTAL": 43},
            "blood_codes": {"BASE": 14, "DLC": 8, "AFTER_ENDING": 13, "TOTAL": 35},
            "mastery_boosters": {"BASE": 57, "DLC": 18, "AFTER_ENDING": 27, "TOTAL": 102},
            "defensive_gadgets": {"BASE": 16, "DLC": 1, "TOTAL": 17},
            "jails": {"BASE": 6, "DLC": 6, "TOTAL": 12},
            "bequeathed_formae": {"BASE": 8, "DLC": 2, "TOTAL": 10},
            "field_enemy": {"WHERE_DLC": 24, "PROTECTED": 10},
        },
        "dlc_package_request_audit": _dlc_package_request_audit(
            revision=revision,
            catalogs=catalogs,
            field_enemy=field_enemy,
        ),
    }
    inventory_evidence = {
        "schema": "cv2-package-inventory-evidence-v1",
        "catalog_revision": revision,
        "retoc_version": "retoc_cli 0.1.5",
        "package_count": len(inventory.package_paths),
        "containers": [
            {"name": name, "package_count": package_counts.get(name, 0)}
            for name in container_names
        ],
        "optional_content_marker_evidence": marker_evidence,
        "distinct_optional_content_container_present": bool(container_markers and any(
            marker.casefold() in inventory.container_names for marker in container_markers
        )),
        "container_classification": container_classes,
        "package_container_classes_inferred_from_names": False,
        "inventory_is_not_ownership_evidence": True,
        "no_dlc_control_installation_inspected": False,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_path = output_dir / "classification_audit.json"
    inventory_path = output_dir / "package_inventory_evidence.json"
    audit_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory_evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return audit_path, inventory_path


# Start the application
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--revision", default="current")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "content_classification" / "current",
    )
    arguments = parser.parse_args()
    try:
        paths = audit_content_classification(
            game_root=arguments.game_root,
            revision=arguments.revision,
            output_dir=arguments.output_dir,
        )
    except (AssetExtractionError, OSError, ValueError) as error:
        parser.error(str(error))
    for path in paths:
        print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
