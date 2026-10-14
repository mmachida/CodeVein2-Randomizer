"""Available Weapon identities assembled from existing World Loot and Shop catalogs."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
import re

from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.catalogs import RuntimeCatalogs
from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES

_WEAPON_PREFIX = "/game/blueprint/article/weapon/"


@dataclass(frozen=True, slots=True)
class WeaponRewardCandidate:
    asset_path: str
    origins: tuple[str, ...]


# Build available weapon pool
def build_available_weapon_pool(
    catalogs: RuntimeCatalogs,
    inventory: PackageInventory,
    *,
    unsafe_packages: Iterable[str] = (),
) -> tuple[WeaponRewardCandidate, ...]:
    """Deduplicate available, unprotected Weapon Articles already in runtime catalogs."""
    unsafe = frozenset(value.casefold() for value in unsafe_packages)
    origins: dict[str, tuple[str, set[str]]] = {}

    # Find an available identity
    def available_identity(path: str) -> str | None:
        key = path.casefold()
        if key in inventory.package_paths:
            return path
        resolved, _variants = inventory.resolve_package_path(path)
        if resolved is None:
            return None
        if resolved.startswith(key):
            return path + resolved[len(key):]
        suffix = re.search(r"_\d+$", key)
        if suffix is not None and resolved == key[:suffix.start()]:
            return path[:suffix.start()]
        return None

    # Add the requested reward item
    def add(path: str, origin: str) -> None:
        normalized_path = available_identity(path)
        if normalized_path is None:
            return
        key = normalized_path.casefold()
        if not key.startswith(_WEAPON_PREFIX):
            return
        previous = origins.get(key)
        if previous is None:
            origins[key] = (normalized_path, {origin})
        else:
            previous[1].add(origin)

    for location in catalogs.world_loot:
        if (
            location.protected
            or location.source_file.casefold() in unsafe
            or location.actor_category not in {"GROUND_PICKUP", "TREASURE_BOX"}
            or location.destination_kind != "ARTICLE_BULK"
            or location.raw_article_layout is not None
            or location.quest_grant_layouts
            or "withflagchange" in location.actor_class.casefold()
            or "withsmallscenario" in location.actor_class.casefold()
            or location.protection_reasons
            or len(location.rewards) != 1
            or not inventory.contains(location.package_path)
        ):
            continue
        add(location.rewards[0].article_path, f"world:{location.package_path}#{location.export_index}")

    for product in catalogs.shops:
        if product.protected:
            continue
        table = SHOP_TABLE_NAMES.get(product.family.upper())
        if table is None or not inventory.contains(f"/Game/Blueprint/GameSystem/Shop/{table}"):
            continue
        add(product.original_product_path, f"shop:{product.family.upper()}:{product.shop_row}:{product.product_index}")

    return tuple(
        WeaponRewardCandidate(path, tuple(sorted(item_origins, key=str.casefold)))
        for path, item_origins in sorted(origins.values(), key=lambda item: item[0].casefold())
    )


__all__ = ["WeaponRewardCandidate", "build_available_weapon_pool"]
