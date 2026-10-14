"""Build the ingredient-only pool from audited respawning world pickups."""

from __future__ import annotations

from collections.abc import Iterable

from .catalogs import MaterialLocation, WorldLootLocation, is_world_map_package


_PROTECTED_REASON = "RENEWABLE_INGREDIENT_DISABLED"


# Find renewable material locations
def renewable_material_locations(
    locations: Iterable[WorldLootLocation],
) -> tuple[MaterialLocation, ...]:
    """Convert audited renewable pickups into the separate Ingredients pool.

    World Loot keeps these rows protected. This view only removes that single
    category lock after verifying the location is an audited renewable pickup;
    every other protection and World Loot membership rule stays untouched.
    """
    result: list[MaterialLocation] = []
    for location in locations:
        if location.location_type != "RENEWABLE_INGREDIENT":
            continue
        if (
            location.destination_kind != "RENEWABLE_INGREDIENT"
            or not location.protected
            or location.protection_reasons != (_PROTECTED_REASON,)
            or not is_world_map_package(location.package_path)
            or not location.source_file.casefold().endswith(".umap")
            or location.export_index < 1
            or not location.export_name.strip()
            or not location.actor_class.strip()
            or not location.structural_class_package
            or not location.structural_class_package.startswith("/Game/Environment/Item/")
            or not location.save_id
            or len(location.rewards) != 1
            or location.rewards[0].count != 1
            or not location.rewards[0].notification
            or ((location.mirror_of_package_path is None) != (location.mirror_of_export_index is None))
        ):
            raise ValueError(
                "An audited renewable ingredient has incomplete or unexpected patch metadata: "
                f"{location.package_path}#{location.export_index}."
            )
        class_name = location.structural_class_package.rsplit("/", 1)[-1] + "_C"
        result.append(MaterialLocation(
            package_path=location.package_path,
            source_file=location.source_file,
            export_index=location.export_index,
            export_name=location.export_name,
            # actor_class on a world-loot row is the instance export-name prefix;
            # it can differ from the Blueprint referenced by the map ClassIndex.
            # The structural package is the validated class used by the actor.
            actor_class=class_name,
            resource_class_package=location.structural_class_package,
            resource_class_name=class_name,
            reward=location.rewards[0],
            save_id=location.save_id,
            protected=False,
            protection_reason=None,
            where_content_class=location.content_class,
            resource_content_class="UNKNOWN",
            reward_content_class=location.rewards[0].content_class,
            mirror_of_package_path=location.mirror_of_package_path,
            mirror_of_export_index=location.mirror_of_export_index,
        ))
    return tuple(result)
