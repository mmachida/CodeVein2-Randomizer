"""Shared, evidence-based package and canonical Article classification."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.content_availability import PackageInventory

_CONTENT_CLASSES = frozenset({"BASE", "DLC", "AFTER_ENDING"})
_ARTICLE_PATH_FIELDS = frozenset({
    "article_path",
    "asset_path",
    "original_article_path",
    "original_product_path",
    "original_asset_path",
    "original_blood_code_package",
    "item_path",
    "booster_article_path",
})
_EXPLICIT_ARTICLE_SOURCE_DIRS = (
    "blood_codes/current",
    "boosters/current",
    "extractor/current",
    "enemy_drops",
    "defensive_gadgets/current",
    "jails/current",
    "bFormae/current",
    "reward_pools/current",
)


@dataclass(frozen=True, slots=True)
class ExplicitArticleClassification:
    """Class for one exact Article identity and the catalogs that establish it."""

    article_path: str
    content_class: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExplicitArticleClassIndex:
    """Conflict-checked canonical Article classifications from reviewed catalogs."""

    entries: tuple[ExplicitArticleClassification, ...]
    conflicts: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...]

    # Get the source classification
    def get(self, article_path: str) -> ExplicitArticleClassification | None:
        key = article_path.casefold()
        return next((entry for entry in self.entries if entry.article_path.casefold() == key), None)


@dataclass(frozen=True, slots=True)
class PackageContentClassification:
    """Auditable content class for one virtual package."""

    virtual_package_path: str
    source_containers: tuple[str, ...]
    content_class: str
    classification_source: str
    audit_status: str
    evidence: str | None = None


# Build explicit article class index
def build_explicit_article_class_index(data_root: Path | None = None) -> ExplicitArticleClassIndex:
    """Collect exact Article identities only from curated, explicitly classed catalogs.

    World, Materials, and Shops are intentionally excluded as sources because
    their reward fields are the catalogs currently being audited. Classification
    never comes from a path/name pattern or from the location that held an item.
    """
    if data_root is None:
        data_root = application_data_path()
    observed: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    display_paths: dict[str, str] = {}

    # Visit the next classification node
    def visit(value: Any, source: str) -> None:
        if isinstance(value, dict):
            content_class = value.get("content_class")
            if isinstance(content_class, str) and content_class.strip().upper() in _CONTENT_CLASSES:
                normalized_class = content_class.strip().upper()
                for field in _ARTICLE_PATH_FIELDS:
                    article_path = value.get(field)
                    if isinstance(article_path, str) and article_path.startswith("/Game/Blueprint/Article/"):
                        key = article_path.casefold()
                        display_paths.setdefault(key, article_path)
                        observed[key][normalized_class].add(f"{source}:{field}")
            for nested in value.values():
                visit(nested, source)
        elif isinstance(value, list):
            for nested in value:
                visit(nested, source)

    source_files: list[Path] = []
    for relative_dir in _EXPLICIT_ARTICLE_SOURCE_DIRS:
        directory = data_root / relative_dir
        if directory.is_dir():
            source_files.extend(directory.rglob("*.json"))
    for path in sorted(set(source_files), key=lambda item: item.as_posix().casefold()):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        visit(document, path.relative_to(data_root).as_posix())

    entries: list[ExplicitArticleClassification] = []
    conflicts: list[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]] = []
    for key, classes in sorted(observed.items()):
        if len(classes) != 1:
            conflicts.append((display_paths[key], tuple(
                (name, tuple(sorted(sources, key=str.casefold)))
                for name, sources in sorted(classes.items())
            )))
            continue
        content_class, evidence = next(iter(classes.items()))
        entries.append(ExplicitArticleClassification(
            display_paths[key], content_class, tuple(sorted(evidence, key=str.casefold)),
        ))
    return ExplicitArticleClassIndex(tuple(entries), tuple(conflicts))


# Classify package content
def classify_package_content(
    virtual_package_path: str,
    inventory: PackageInventory,
    *,
    package_overrides: Mapping[str, tuple[str, str]] | None = None,
    container_classes: Mapping[str, str] | None = None,
) -> PackageContentClassification:
    """Apply exact package overrides, then exact IoStore container provenance.

    ``package_overrides`` maps a complete virtual package path to
    ``(content_class, evidence)``. ``container_classes`` maps a complete .utoc
    filename to a reviewed class. No folder, package-name, or DLC-string
    heuristics are applied.
    """
    path_key = virtual_package_path.casefold()
    owners = inventory.containers_for(virtual_package_path)
    exact_overrides = {key.casefold(): value for key, value in (package_overrides or {}).items()}
    override = exact_overrides.get(path_key)
    if override is not None:
        content_class, evidence = override
        normalized = content_class.strip().upper()
        if normalized not in _CONTENT_CLASSES:
            raise ValueError(f"Unsupported package override class {content_class!r}.")
        return PackageContentClassification(
            virtual_package_path, owners, normalized, "VERSION_OVERRIDE", "CLASSIFIED", evidence,
        )

    classes_by_container = {
        name.casefold(): value.strip().upper()
        for name, value in (container_classes or {}).items()
    }
    owner_classes = {classes_by_container[owner.casefold()] for owner in owners if owner.casefold() in classes_by_container}
    if owners and len(owner_classes) == 1 and all(owner.casefold() in classes_by_container for owner in owners):
        content_class = next(iter(owner_classes))
        if content_class not in _CONTENT_CLASSES:
            raise ValueError(f"Unsupported container content class {content_class!r}.")
        return PackageContentClassification(
            virtual_package_path, owners, content_class, "PACKAGE_CONTAINER", "CLASSIFIED",
            "All exact owning containers have the same reviewed content class.",
        )

    present = path_key in inventory.package_paths
    return PackageContentClassification(
        virtual_package_path,
        owners,
        "UNKNOWN",
        "PACKAGE_CONTAINER" if owners else "UNKNOWN",
        "EXHAUSTIVE_UNRESOLVED" if present else "NOT_IN_INSTALLED_INVENTORY",
        "Package/container inventory contains no reviewed class evidence." if present else None,
    )


# Classify shop where
def classify_shop_where(family: str) -> PackageContentClassification:
    """Classify the existing shop table family independently from its WHAT."""
    normalized = family.strip().upper()
    if normalized in {"NORMAL", "LUXURY", "TRADE"}:
        content_class = "BASE"
    elif normalized in {"DLC", "TRADE_DLC"}:
        content_class = "DLC"
    else:
        content_class = "UNKNOWN"
    return PackageContentClassification(
        f"shop-family:{normalized}", (), content_class, "TABLE_FAMILY",
        "CLASSIFIED" if content_class != "UNKNOWN" else "EXHAUSTIVE_UNRESOLVED",
        "Reviewed shop table-family classification." if content_class != "UNKNOWN" else None,
    )


# Count article class counts
def article_class_counts(index: ExplicitArticleClassIndex) -> dict[str, int]:
    """Stable counts for the reviewed Article-class evidence set."""
    counts = {name: 0 for name in ("BASE", "DLC", "AFTER_ENDING")}
    for entry in index.entries:
        counts[entry.content_class] += 1
    return counts
