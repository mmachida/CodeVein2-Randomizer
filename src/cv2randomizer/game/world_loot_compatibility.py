"""Version-specific export compatibility for map-based World Loot."""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from .catalogs import WorldLootLocation


# Rebind catalogued world exports
def apply_world_loot_export_rebindings(
    locations: Iterable[WorldLootLocation],
    dependency_index: object,
) -> tuple[WorldLootLocation, ...]:
    """Apply validated per-build export indices from the extracted cache."""
    if not isinstance(dependency_index, dict):
        return tuple(locations)
    rows = dependency_index.get("world_loot_export_rebindings", [])
    if not isinstance(rows, list):
        raise ValueError("Cached World Loot export rebindings are invalid.")

    index_by_source: dict[tuple[str, int], int] = {}
    name_by_source: dict[tuple[str, int], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Cached World Loot export rebindings contain an invalid row.")
        package = row.get("package_path")
        source_index = row.get("catalog_export_index")
        target_index = row.get("resolved_export_index")
        export_name = row.get("export_name")
        if (
            not isinstance(package, str) or not package.startswith("/Game/Map/")
            or not isinstance(source_index, int) or isinstance(source_index, bool) or source_index < 1
            or not isinstance(target_index, int) or isinstance(target_index, bool) or target_index < 1
            or source_index == target_index
            or not isinstance(export_name, str) or not export_name
        ):
            raise ValueError("Cached World Loot export rebindings contain invalid identities.")
        key = (package.casefold(), source_index)
        if key in index_by_source:
            raise ValueError("Cached World Loot export rebindings repeat a catalog identity.")
        index_by_source[key] = target_index
        name_by_source[key] = export_name

    rebound: list[WorldLootLocation] = []
    resolved_slots: set[tuple[str, int]] = set()
    for location in locations:
        source_key = (location.package_path.casefold(), location.export_index)
        target_index = index_by_source.get(source_key, location.export_index)
        if source_key in name_by_source and name_by_source[source_key] != location.export_name:
            raise ValueError(
                "Cached World Loot export rebinding no longer matches the catalogued object name."
            )
        mirror_index = location.mirror_of_export_index
        if mirror_index is not None and location.mirror_of_package_path is not None:
            mirror_index = index_by_source.get(
                (location.mirror_of_package_path.casefold(), mirror_index), mirror_index,
            )
        destination_id = location.destination_id
        if destination_id == f"{location.package_path}#{location.export_index}":
            destination_id = f"{location.package_path}#{target_index}"
        updated = replace(
            location,
            export_index=target_index,
            destination_id=destination_id,
            mirror_of_export_index=mirror_index,
        )
        if location.package_path.casefold().startswith("/game/map/"):
            resolved_key = (updated.package_path.casefold(), updated.export_index)
            if resolved_key in resolved_slots:
                raise ValueError("Cached World Loot export rebindings collide at one physical map export.")
            resolved_slots.add(resolved_key)
        rebound.append(updated)
    return tuple(rebound)
