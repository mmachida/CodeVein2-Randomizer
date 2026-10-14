"""Diagnose decoded shop stock fields against the trusted catalog and source CSV."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from cv2randomizer.game.catalogs import CatalogStore
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES, read_cooked_name_map
from cv2randomizer.game.shop_tables import parse_shop_table
from cv2randomizer.game.usmap import read_usmap

_CSV_FAMILIES = {
    "normal": "NORMAL",
    "dlc": "DLC",
    "luxury": "LUXURY",
    "trade": "TRADE",
    "trade_dlc": "TRADE_DLC",
}


# Read source rows
def _read_source_rows(path: Path) -> dict[tuple[str, str, int], dict[str, str]]:
    rows: dict[tuple[str, str, int], dict[str, str]] = {}
    positions: Counter[tuple[str, str]] = Counter()
    with path.open(encoding="utf-8-sig", newline="") as source_file:
        for record in csv.DictReader(source_file):
            source_family = record.get("table", "").strip().casefold()
            family = _CSV_FAMILIES.get(source_family)
            if family is None:
                raise ValueError(f"Unknown shop table family in source CSV: {source_family!r}.")
            row = record.get("shop_row", "")
            if not row:
                raise ValueError("Source CSV has a row with no shop row name.")
            index = positions[(family, row)]
            positions[(family, row)] += 1
            key = (family, row, index)
            if key in rows:
                raise ValueError(f"Duplicate source CSV slot: {key!r}.")
            rows[key] = record
    return rows


# Parse source bool
def _source_bool(value: str, *, label: str) -> bool:
    normalized = value.strip().casefold()
    if normalized not in {"true", "false"}:
        raise ValueError(f"Source CSV {label} is not True or False: {value!r}.")
    return normalized == "true"


# Inspect shop tables
def inspect_shop_tables(
    *,
    assets_dir: Path,
    mapping_path: Path,
    catalogs_dir: Path,
    catalog_revision: str,
    source_csv: Path,
) -> dict[str, Any]:
    """Parse all local tables without assuming unknown or discrepant stock semantics."""
    mappings = read_usmap(mapping_path)
    catalogs = CatalogStore(catalogs_dir).load(catalog_revision)
    source_rows = _read_source_rows(source_csv)
    family_reports: dict[str, dict[str, Any]] = {}
    totals: Counter[str] = Counter()
    mismatch_examples: list[dict[str, Any]] = []

    for family, table_name in SHOP_TABLE_NAMES.items():
        expected = tuple(product for product in catalogs.shops if product.family == family)
        name_map = read_cooked_name_map(
            assets_dir / f"{table_name}.uasset",
            expected_package_name=f"/Game/Blueprint/GameSystem/Shop/{table_name}",
        )
        parsed = parse_shop_table(
            (assets_dir / f"{table_name}.uexp").read_bytes(),
            family=family,
            name_map=name_map,
            mappings=mappings,
            expected_products=expected,
            source=f"{table_name}.uexp",
            verify_stock_against_catalog=False,
        )
        source_by_slot = {
            (row, index): values
            for (source_family, row, index), values in source_rows.items()
            if source_family == family
        }
        if len(parsed.products) != len(expected):
            raise ValueError(
                f"{family}: parsed {len(parsed.products)} products, catalog contains {len(expected)}."
            )
        stats: Counter[str] = Counter(
            article_path_offsets_checked=0,
            article_path_offset_mismatches=0,
            stock_payload_unvalidated_slots=0,
        )
        for product in parsed.products:
            key = (product.shop_row, product.product_index)
            record = source_by_slot.get(key)
            if record is None:
                raise ValueError(f"{family}: source CSV is missing slot {key!r}.")
            catalog_product = next(
                (item for item in expected if (item.shop_row, item.product_index) == key),
                None,
            )
            if catalog_product is None:
                raise ValueError(f"{family}: catalog is missing slot {key!r}.")
            csv_path = record.get("item_path", "")
            csv_object = record.get("item_object", "")
            try:
                csv_price = int(record["price"])
                csv_uses_stock = _source_bool(record["uses_stock"], label="uses_stock")
                csv_stock = int(record["stock"])
                csv_article_offset = int(record["binary_offset_hex"], 16)
            except (KeyError, ValueError) as error:
                raise ValueError(f"{family} {key}: invalid source CSV fields: {error}.") from error
            if (
                csv_path != catalog_product.original_product_path
                or csv_object != catalog_product.item_object
                or csv_price != catalog_product.price
                or csv_uses_stock != catalog_product.uses_stock
                or csv_stock != catalog_product.stock
            ):
                raise ValueError(f"{family} {key}: source CSV differs from the runtime catalog.")

            stats["article_path_offsets_checked"] += 1
            if csv_article_offset != product.article_path_offset:
                stats["article_path_offset_mismatches"] += 1

            if product.uses_stock is None:
                stats["uses_stock_defaulted"] += 1
            elif product.uses_stock != csv_uses_stock:
                stats["uses_stock_mismatch"] += 1
            if product.stock is None:
                stats["stock_defaulted"] += 1
            elif product.stock != csv_stock:
                stats["stock_mismatch"] += 1
                if len(mismatch_examples) < 12:
                    mismatch_examples.append(
                        {
                            "family": family,
                            "shop_row": product.shop_row,
                            "product_index": product.product_index,
                            "csv_stock": csv_stock,
                            "serialized_stock": product.stock,
                            "binary_offset_hex": record.get("binary_offset_hex"),
                        }
                    )
            if (
                product.uses_stock is None or product.stock is None
                or product.uses_stock != csv_uses_stock or product.stock != csv_stock
            ):
                stats["stock_payload_unvalidated_slots"] += 1
        family_reports[family] = {
            "rows": len(parsed.rows),
            "products": len(parsed.products),
            **dict(stats),
        }
        totals.update(stats)
        totals["rows"] += len(parsed.rows)
        totals["products"] += len(parsed.products)

    offsets_aligned = totals["article_path_offset_mismatches"] == 0
    return {
        "catalog_revision": catalog_revision,
        "table_families": family_reports,
        "totals": dict(totals),
        "article_path_offsets_aligned": offsets_aligned,
        "stock_payload_unvalidated_slots": totals["stock_payload_unvalidated_slots"],
        "stock_behavior_in_game_validation_pending": True,
        "explicit_stock_mismatch_examples": mismatch_examples,
    }


# Start the application
def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    default_assets = repository_root / "workspace" / "research_assets" / "shop"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path, default=default_assets)
    parser.add_argument(
        "--mapping",
        type=Path,
        default=repository_root / "workspace" / "research_assets" / "CV2.usmap",
    )
    parser.add_argument(
        "--catalogs-dir",
        type=Path,
        default=repository_root / "data" / "catalogs",
    )
    parser.add_argument("--catalog-revision", default="current")
    parser.add_argument(
        "--source-csv",
        type=Path,
        default=repository_root / "data" / "catalogs" / "current" / "shop_catalog_all_tables.csv",
    )
    arguments = parser.parse_args()
    try:
        report = inspect_shop_tables(
            assets_dir=arguments.assets_dir,
            mapping_path=arguments.mapping,
            catalogs_dir=arguments.catalogs_dir,
            catalog_revision=arguments.catalog_revision,
            source_csv=arguments.source_csv,
        )
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(2, f"Shop table validation failed: {error}\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if not report["article_path_offsets_aligned"] else 0


if __name__ == "__main__":
    raise SystemExit(main())