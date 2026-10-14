"""Version-bound metadata for the two Booster Extractor recipe DataTables."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import application_data_path

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TABLES = {
    "/Game/Blueprint/GameSystem/ItemSynthetic/Booster/DT_BoosterRecipe": ("DT_BoosterRecipe", "BASE", 36),
    "/Game/Blueprint/GameSystem/ItemSynthetic/Booster/DT_BoosterRecipe_DLC": ("DT_BoosterRecipe_DLC", "DLC", 7),
}
_CLASSES = {"BASE", "DLC", "AFTER_ENDING"}


class ExtractorCatalogError(ValueError):
    """Raised when recipe metadata is absent, stale, or unsafe."""


@dataclass(frozen=True, slots=True)
class ExtractorRecipeSlot:
    virtual_package: str
    table_name: str
    content_class: str
    row_index: int
    row_name: str
    original_article_path: str
    immutable_fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class ExtractorRecipeTable:
    virtual_package: str
    table_name: str
    content_class: str
    expected_row_count: int
    source_package_sha256: dict[str, str]
    rows: tuple[ExtractorRecipeSlot, ...]


@dataclass(frozen=True, slots=True)
class ExtractorCatalog:
    revision: str
    tables: tuple[ExtractorRecipeTable, ...]

    # List slots
    @property
    def slots(self) -> tuple[ExtractorRecipeSlot, ...]:
        return tuple(row for table in self.tables for row in table.rows)

    # Build table
    def table(self, virtual_package: str) -> ExtractorRecipeTable | None:
        return next((table for table in self.tables if table.virtual_package.casefold() == virtual_package.casefold()), None)


class ExtractorCatalogStore:
    """Load the checksum-bound recipe inventory without scanning game packages."""

    # Initialize this object
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_path("extractor", "current")

    # Load and validate catalog data
    def load(self, revision: str) -> ExtractorCatalog:
        if not isinstance(revision, str) or not revision.strip():
            raise ExtractorCatalogError("Extractor catalog revision must be non-empty.")
        try:
            manifest = json.loads((self.data_dir / "manifest.json").read_text(encoding="utf-8"))
            payload = (self.data_dir / "booster_recipes.json").read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ExtractorCatalogError(f"Could not read Extractor catalog: {error}") from error
        if not isinstance(manifest, dict) or manifest.get("schema") != "cv2-extractor-catalog-v1":
            raise ExtractorCatalogError("Extractor catalog manifest has an unknown schema.")
        if manifest.get("catalog_revision") != revision:
            raise ExtractorCatalogError("Extractor catalog revision does not match the selected game catalog.")
        metadata = manifest.get("files", {}).get("booster_recipes.json")
        if not isinstance(metadata, dict) or metadata.get("table_count") != 2 or metadata.get("row_count") != 43:
            raise ExtractorCatalogError("Extractor manifest must declare the two recipe tables and 43 rows.")
        if metadata.get("sha256") != hashlib.sha256(payload).hexdigest():
            raise ExtractorCatalogError("Extractor recipe metadata checksum does not match its manifest.")
        try:
            document: Any = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ExtractorCatalogError(f"Extractor recipe JSON is invalid: {error}") from error
        if (not isinstance(document, dict) or document.get("schema") != "cv2-extractor-booster-recipes-v1"
                or document.get("catalog_revision") != revision):
            raise ExtractorCatalogError("Extractor recipe metadata schema or revision is invalid.")
        raw_tables = document.get("tables")
        if not isinstance(raw_tables, list) or len(raw_tables) != 2:
            raise ExtractorCatalogError("Extractor catalog must contain exactly the base and DLC tables.")
        tables: list[ExtractorRecipeTable] = []
        seen_packages: set[str] = set()
        seen_rewards: set[str] = set()
        for raw in raw_tables:
            if not isinstance(raw, dict):
                raise ExtractorCatalogError("Extractor table entries must be objects.")
            package = _string(raw.get("virtual_package"), "virtual_package")
            expected = _TABLES.get(package)
            table_name = _string(raw.get("table_name"), "table_name")
            content_class = _string(raw.get("content_class"), "content_class").upper()
            row_count = _integer(raw.get("expected_row_count"), "expected_row_count", minimum=1)
            if expected is None or (table_name, content_class, row_count) != expected:
                raise ExtractorCatalogError(f"Extractor source table {package!r} differs from the validated inventory.")
            if package.casefold() in seen_packages:
                raise ExtractorCatalogError("Extractor table package is duplicated.")
            fingerprints = raw.get("source_package_sha256")
            if (not isinstance(fingerprints, dict) or set(fingerprints) != {".uasset", ".uexp"}
                    or any(not isinstance(value, str) or not _SHA256.fullmatch(value) for value in fingerprints.values())):
                raise ExtractorCatalogError(f"Source package hashes are incomplete for {table_name}.")
            raw_rows = raw.get("rows")
            if not isinstance(raw_rows, list) or len(raw_rows) != row_count:
                raise ExtractorCatalogError(f"Extractor table {table_name} does not declare exactly {row_count} rows.")
            rows: list[ExtractorRecipeSlot] = []
            row_names: set[str] = set()
            for index, item in enumerate(raw_rows):
                if not isinstance(item, dict):
                    raise ExtractorCatalogError("Extractor recipe rows must be objects.")
                row_index = _integer(item.get("row_index"), "row_index", minimum=0)
                row_name = _string(item.get("row_name"), "row_name")
                article_path = _string(item.get("original_article_path"), "original_article_path")
                immutable = _string(item.get("immutable_fingerprint_sha256"), "immutable_fingerprint_sha256").casefold()
                if row_index != index or row_name.casefold() in row_names:
                    raise ExtractorCatalogError(f"Extractor table {table_name} row identity or order is invalid.")
                if not article_path.startswith("/Game/Blueprint/Article/Booster/") or not article_path.rsplit("/", 1)[-1].startswith("DA_Booster_"):
                    raise ExtractorCatalogError(f"Extractor row {row_name} does not reference a package-level Booster Article.")
                if not _SHA256.fullmatch(immutable) or article_path.casefold() in seen_rewards:
                    raise ExtractorCatalogError("Extractor immutable fingerprints must be valid and reward paths unique.")
                seen_rewards.add(article_path.casefold())
                row_names.add(row_name.casefold())
                rows.append(ExtractorRecipeSlot(package, table_name, content_class, row_index, row_name, article_path, immutable))
            tables.append(ExtractorRecipeTable(package, table_name, content_class, row_count,
                                               {key: value.casefold() for key, value in fingerprints.items()}, tuple(rows)))
            seen_packages.add(package.casefold())
        if seen_packages != {package.casefold() for package in _TABLES} or sum(len(table.rows) for table in tables) != 43:
            raise ExtractorCatalogError("Extractor catalog is missing a required table or recipe row.")
        return ExtractorCatalog(revision, tuple(tables))


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtractorCatalogError(f"{label} must be a non-empty string.")
    return value


# Parse an integer value
def _integer(value: Any, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ExtractorCatalogError(f"{label} must be an integer >= {minimum}.")
    return value
