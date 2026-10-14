"""Resolve source-package availability independently from content labels.

``BASE``/``DLC``/``AFTER_ENDING`` are useful catalog metadata, but they do not
prove that a source package is present. Runtime eligibility is resolved from
the installed package inventory (or an extracted Content cache).
"""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


class AssetAvailabilityState(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class PackageInventory:
    """Read-only package and IoStore container inventory."""

    package_paths: frozenset[str]
    container_names: frozenset[str]
    package_containers: tuple[tuple[str, tuple[str, ...]], ...] = ()
    package_variants: tuple[tuple[str, tuple[str, ...]], ...] = ()
    package_path_spellings: tuple[tuple[str, str], ...] = ()

    # Create the availability record
    @classmethod
    def create(
        cls,
        package_paths: Iterable[str],
        container_names: Iterable[str] = (),
        package_containers: Iterable[tuple[str, str]] = (),
    ) -> "PackageInventory":
        owners: dict[str, set[str]] = {}
        for package_path, container_name in package_containers:
            owners.setdefault(package_path.casefold(), set()).add(container_name)
        spellings: dict[str, str] = {}
        for value in package_paths:
            key = value.casefold()
            previous = spellings.get(key)
            if previous is None or value < previous:
                spellings[key] = value
        normalized_paths = frozenset(spellings)
        variants: dict[str, list[str]] = defaultdict(list)
        for package_path in normalized_paths:
            match = re.search(r"_(?:0|[1-9]\d*)$", package_path)
            if match is not None:
                variants[package_path[:match.start()]].append(package_path)
        return cls(
            normalized_paths,
            frozenset(value.casefold() for value in container_names),
            tuple(sorted(
                (path, tuple(sorted(names, key=str.casefold)))
                for path, names in owners.items()
            )),
            tuple(sorted(
                (base, tuple(sorted(paths, key=str.casefold)))
                for base, paths in variants.items()
            )),
            tuple(sorted(spellings.items())),
        )

    # Build content tree
    @classmethod
    def from_content_tree(cls, content_root: Path) -> "PackageInventory":
        """Build the same lookup from a clean extracted Unreal ``Content`` tree."""
        root = content_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Extracted asset cache root must be a directory.")
        paths = (
            "/Game/" + item.relative_to(root).with_suffix("").as_posix()
            for item in root.rglob("*")
            if item.is_file() and item.suffix.casefold() in {".uasset", ".umap"}
        )
        return cls.create(paths)

    # Check whether the item exists
    def contains(self, package_path: str) -> bool:
        """Return whether an exact or uniquely resolved numbered package exists."""
        return self.resolve_package_path(package_path)[0] is not None

    # Find containers for this game build
    def containers_for(self, package_path: str) -> tuple[str, ...]:
        key = package_path.casefold()
        low, high = 0, len(self.package_containers)
        while low < high:
            middle = (low + high) // 2
            path, containers = self.package_containers[middle]
            if path < key:
                low = middle + 1
            else:
                high = middle
        if low < len(self.package_containers) and self.package_containers[low][0] == key:
            return self.package_containers[low][1]
        return ()

    # Resolve package path
    def resolve_package_path(self, package_path: str) -> tuple[str | None, tuple[str, ...]]:
        """Resolve exact or uniquely observed numbered package-name variants."""
        key = package_path.casefold()
        if key in self.package_paths:
            return key, (key,)
        low, high = 0, len(self.package_variants)
        while low < high:
            middle = (low + high) // 2
            base_path, variants = self.package_variants[middle]
            if base_path < key:
                low = middle + 1
            else:
                high = middle
        if low < len(self.package_variants) and self.package_variants[low][0] == key:
            variants = self.package_variants[low][1]
            return (variants[0], variants) if len(variants) == 1 else (None, variants)
        return None, ()

    # Resolve package path spelling
    def resolve_package_path_spelling(self, package_path: str) -> tuple[str | None, tuple[str, ...]]:
        """Resolve a package while preserving the exact casing from the asset inventory.

        Membership and ambiguity checks remain case-insensitive, but tools such as
        Retoc use case-sensitive filename filters. Callers that extract files need
        the spelling returned by the game's package listing.
        """
        resolved, variants = self.resolve_package_path(package_path)
        if resolved is None:
            return None, tuple(self._package_path_spelling(value) for value in variants)
        return self._package_path_spelling(resolved), tuple(
            self._package_path_spelling(value) for value in variants
        )

    # Build package path spelling
    def _package_path_spelling(self, normalized_path: str) -> str:
        """Find the preserved spelling without copying the full inventory map.

        ``package_path_spellings`` is sorted by normalized path in ``create``.
        Extract resolves thousands of paths against inventories with over
        200,000 entries, so rebuilding a dictionary for each lookup copies
        billions of mapping entries. Binary search keeps each lookup logarithmic.
        """
        rows = self.package_path_spellings
        index = bisect_left(rows, (normalized_path,))
        if index < len(rows) and rows[index][0] == normalized_path:
            return rows[index][1]
        return normalized_path


@dataclass(frozen=True, slots=True)
class SourceAssetAvailability:
    subsystem: str
    package_path: str
    state: AssetAvailabilityState
    reason: str = ""


@dataclass(frozen=True, slots=True)
class FeatureAvailability:
    subsystem: str
    available: int
    missing: int
    invalid: int
    state: str


@dataclass(frozen=True, slots=True)
class ContentAvailability:
    """Immutable, deterministic availability snapshot for known source packages."""

    sources: tuple[SourceAssetAvailability, ...]

    # Get status for this game build
    def state_for(self, package_path: str, *, subsystem: str | None = None) -> AssetAvailabilityState | None:
        key = package_path.casefold()
        for source in self.sources:
            if source.package_path.casefold() == key and (subsystem is None or source.subsystem == subsystem):
                return source.state
        return None

    # Check whether the item exists
    def contains(self, package_path: str, *, subsystem: str | None = None) -> bool:
        return self.state_for(package_path, subsystem=subsystem) is AssetAvailabilityState.AVAILABLE

    # Use invalid
    def with_invalid(self, package_path: str, reason: str, *, subsystem: str | None = None) -> "ContentAvailability":
        key = package_path.casefold()
        updated = tuple(
            replace(source, state=AssetAvailabilityState.INVALID, reason=reason)
            if source.package_path.casefold() == key and (subsystem is None or source.subsystem == subsystem)
            else source
            for source in self.sources
        )
        return ContentAvailability(updated)

    # List available features
    @property
    def features(self) -> tuple[FeatureAvailability, ...]:
        grouped: dict[str, list[SourceAssetAvailability]] = defaultdict(list)
        for source in self.sources:
            grouped[source.subsystem].append(source)
        result: list[FeatureAvailability] = []
        for name in sorted(grouped, key=str.casefold):
            rows = grouped[name]
            available = sum(row.state is AssetAvailabilityState.AVAILABLE for row in rows)
            missing = sum(row.state is AssetAvailabilityState.MISSING for row in rows)
            invalid = sum(row.state is AssetAvailabilityState.INVALID for row in rows)
            state = (
                "MISSING" if not available and not invalid
                else "INVALID" if not available and invalid and not missing
                else "AVAILABLE" if not missing and not invalid
                else "PARTIAL"
            )
            result.append(FeatureAvailability(name, available, missing, invalid, state))
        return tuple(result)

    # List available packages
    @property
    def available_packages(self) -> frozenset[str]:
        return frozenset(
            source.package_path.casefold()
            for source in self.sources if source.state is AssetAvailabilityState.AVAILABLE
        )

    # Convert settings to a dictionary
    def to_dict(self) -> dict[str, object]:
        return {
            "features": {
                item.subsystem: {
                    "state": item.state,
                    "available": item.available,
                    "missing": item.missing,
                    "invalid": item.invalid,
                }
                for item in self.features
            },
            "sources": [
                {
                    "subsystem": source.subsystem,
                    "package": source.package_path,
                    "state": source.state.value,
                    **({"reason": source.reason} if source.reason else {}),
                }
                for source in self.sources
            ],
            "missing_optional_sources": [
                {"subsystem": source.subsystem, "package": source.package_path}
                for source in self.sources if source.state is AssetAvailabilityState.MISSING
            ],
            "missing_sources": [
                {"subsystem": source.subsystem, "package": source.package_path}
                for source in self.sources if source.state is AssetAvailabilityState.MISSING
            ],
            "invalid_sources": [
                {"subsystem": source.subsystem, "package": source.package_path, "reason": source.reason}
                for source in self.sources if source.state is AssetAvailabilityState.INVALID
            ],
        }


# Resolve content availability
def resolve_content_availability(
    known_sources: Mapping[str, Iterable[str]],
    inventory: PackageInventory,
) -> ContentAvailability:
    """Classify exact known packages before planners request extraction."""
    rows: dict[tuple[str, str], SourceAssetAvailability] = {}
    for subsystem, package_paths in known_sources.items():
        if not isinstance(subsystem, str) or not subsystem.strip():
            raise ValueError("Availability subsystem names must be non-empty strings.")
        for package_path in package_paths:
            normalized = _normalize_package_path(package_path)
            key = (subsystem.strip(), normalized.casefold())
            resolved, variants = inventory.resolve_package_path(normalized)
            state = (AssetAvailabilityState.AVAILABLE if resolved is not None else
                     AssetAvailabilityState.INVALID if variants else AssetAvailabilityState.MISSING)
            reason = "Ambiguous numbered package: " + ", ".join(variants) if resolved is None and variants else ""
            rows[key] = SourceAssetAvailability(key[0], normalized, state, reason)
    return ContentAvailability(tuple(
        rows[key] for key in sorted(rows, key=lambda value: (value[0].casefold(), value[1]))
    ))


# Normalize package path
def _normalize_package_path(value: str) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("Availability sources must use Unreal /Game package paths.")
    path = PurePosixPath(value)
    if (
        not value.startswith("/Game/") or path.suffix or len(path.parts) < 3
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ValueError(f"Invalid Unreal source package path {value!r}.")
    return value


# Build package path from object path
def package_path_from_object_path(value: str) -> str:
    """Strip optional Unreal object/subobject notation from an asset path."""
    package = value.split("::", 1)[0].split(".", 1)[0].strip()
    if package.casefold().startswith("/game/"):
        package = "/Game/" + package[6:]
    return _normalize_package_path(package)
