"""Logical Jail rewards that have more than one physical acquisition route."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cv2randomizer.game.content_availability import PackageInventory
from cv2randomizer.game.fname_identity import SoftObjectPathIdentity
from cv2randomizer.game.shop_tables import ShopTableProduct

from .shops import ArticleRewardIdentity, ShopAssignment
from .world_loot import WorldLootRandomizationPlan

STINGER_ARTICLE = "/Game/Blueprint/Article/Jail/DA_Jail_Stinger"
STINGER_WORLD_PACKAGE = "/Game/Map/MainWorld/MainWorld/_Generated_/61Y5LAV87NWVD213F35X2VK87"
STINGER_WORLD_EXPORT = 272
STINGER_WORLD_EXPORT_NAME = (
    "BP_PickupItem_WithFlagChange_C_UAID_A8A159A4A14B9D8902_1822678819_3eb3acb6c4c82637"
)
STINGER_SHOP_FAMILY = "NORMAL"
STINGER_SHOP_ROW = "Shop_SC_Normal1"
STINGER_SHOP_PRODUCT_INDEX = 20
STINGER_SHOP_TABLE_PACKAGE = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"
OGRE_ARTICLE = "/Game/Blueprint/Article/Jail/DA_Jail_Ogre"
OGRE_SHOP_FAMILY = "NORMAL"
OGRE_SHOP_ROW = "Shop_Jadwiga_Weapon"
OGRE_SHOP_PRODUCT_INDEX = 29


@dataclass(frozen=True, slots=True)
class JailShopRoute:
    """Audited finite-shop occurrence of a canonical player Jail."""

    article_path: str
    family: str
    shop_row: str
    product_index: int
    route_kind: str


JAIL_SHOP_ROUTES = (
    JailShopRoute(
        article_path=OGRE_ARTICLE,
        family=OGRE_SHOP_FAMILY,
        shop_row=OGRE_SHOP_ROW,
        product_index=OGRE_SHOP_PRODUCT_INDEX,
        route_kind="linked_initial_inventory",
    ),
    JailShopRoute(
        article_path=STINGER_ARTICLE,
        family=STINGER_SHOP_FAMILY,
        shop_row=STINGER_SHOP_ROW,
        product_index=STINGER_SHOP_PRODUCT_INDEX,
        route_kind="linked_alternate",
    ),
)


# Validate jail shop routes
def validate_jail_shop_routes(products: Iterable[object]) -> tuple[tuple[JailShopRoute, object], ...]:
    """Fail closed on unreviewed Jail Shop copies or changed audited rows.

    The Stinger route mirrors its canonical World reward. The finite Ogre row
    mirrors the Jail in Initial Inventory: its Article declares the same
    `Gain_Jail_Ogre` story flag as the starting reward. Neither alternate is
    an independent source occurrence in the randomizer's pools.
    """
    expected_by_path = {route.article_path.casefold(): route for route in JAIL_SHOP_ROUTES}
    seen_paths: set[str] = set()
    validated: list[tuple[JailShopRoute, object]] = []
    for product in products:
        path = getattr(product, "original_product_path", None)
        if not isinstance(path, str):
            path = getattr(product, "article_package_path", None)
        if not isinstance(path, str):
            continue
        normalized_path = path.casefold()
        basename = normalized_path.rsplit("/", 1)[-1]
        if (
            not normalized_path.startswith("/game/blueprint/article/jail/")
            or not basename.startswith("da_jail_")
        ):
            continue
        route = expected_by_path.get(normalized_path)
        if route is None:
            raise ValueError(f"Unreviewed Shop route for unique Jail Article {path}.")
        if normalized_path in seen_paths:
            raise ValueError(f"Unique Jail Shop route {path} appears in more than one product slot.")
        actual_slot = (
            str(getattr(product, "family", "")).upper(),
            getattr(product, "shop_row", None),
            getattr(product, "product_index", None),
        )
        expected_slot = (route.family, route.shop_row, route.product_index)
        if actual_slot != expected_slot:
            raise ValueError(
                f"Unique Jail Shop route {path} moved from audited slot {expected_slot!r} to {actual_slot!r}."
            )
        uses_stock = getattr(product, "uses_stock", None)
        if uses_stock is None:
            uses_stock = getattr(product, "catalog_uses_stock", None)
        if uses_stock is None:
            uses_stock = getattr(product, "destination_uses_stock", None)
        stock = getattr(product, "stock", None)
        if stock is None:
            stock = getattr(product, "catalog_stock", None)
        if stock is None:
            stock = getattr(product, "logical_stock", None)
        if uses_stock is not True or stock != 1:
            raise ValueError(f"Unique Jail Shop route {path} is no longer a one-stock finite product.")
        seen_paths.add(normalized_path)
        validated.append((route, product))
    return tuple(validated)


# Check stinger shop slot
def is_stinger_shop_slot(product: object) -> bool:
    """Identify the one Josee Stinger shop row linked to the Outpost pickup."""
    return (
        str(getattr(product, "family", "")).upper() == STINGER_SHOP_FAMILY
        and getattr(product, "shop_row", None) == STINGER_SHOP_ROW
        and getattr(product, "product_index", None) == STINGER_SHOP_PRODUCT_INDEX
    )


# Check ogre shop slot
def is_ogre_shop_slot(product: object) -> bool:
    """Identify the Jadwiga Ogre product linked to the starting Jail slot."""
    return (
        str(getattr(product, "family", "")).upper() == OGRE_SHOP_FAMILY
        and getattr(product, "shop_row", None) == OGRE_SHOP_ROW
        and getattr(product, "product_index", None) == OGRE_SHOP_PRODUCT_INDEX
    )


# Exclude linked jail shop routes
def exclude_linked_jail_shop_routes(products: Iterable[object]) -> tuple[object, ...]:
    """Keep linked Jail shop routes out of every independent Shop pool.

    Both Josee's Stinger alternate and Jadwiga's starting-Jail mirror are
    excluded.
    """
    return tuple(
        product for product in products
        if not is_stinger_shop_slot(product) and not is_ogre_shop_slot(product)
    )


# Find stinger shop product
def find_stinger_shop_product(products: Iterable[object]) -> object | None:
    """Find and validate the exact finite-shop alternate Stinger route."""
    matches = [product for product in products if is_stinger_shop_slot(product)]
    if len(matches) > 1:
        raise ValueError("The Josee Stinger alternate shop slot is duplicated in the parsed product table.")
    if not matches:
        return None
    product = matches[0]
    path = getattr(product, "article_package_path", None)
    if path is None:
        path = getattr(product, "original_product_path", None)
        if isinstance(path, str):
            number = getattr(product, "article_path_number", 0) or 0
            if number:
                path = f"{path}_{number - 1}"
    if not isinstance(path, str) or path.casefold() != STINGER_ARTICLE.casefold():
        raise ValueError("The Josee Stinger shop slot no longer contains DA_Jail_Stinger.")
    return product


# Explain stinger route unavailable reason
def stinger_route_unavailable_reason(shop_product: object | None, inventory: PackageInventory) -> str | None:
    """Explain why the linked Stinger pickup must stay vanilla in this build."""
    if shop_product is None:
        return "the audited shop alternate is not catalogued"
    if not inventory.contains(STINGER_ARTICLE):
        return "the Stinger Article is absent"
    if not inventory.contains(STINGER_SHOP_TABLE_PACKAGE):
        return "the base Shop table is absent"
    return None


# Find ogre shop product
def find_ogre_shop_product(products: Iterable[object]) -> object | None:
    """Find and validate the audited finite Jadwiga starting-Jail mirror."""
    matches = [product for product in products if is_ogre_shop_slot(product)]
    if len(matches) > 1:
        raise ValueError("The Jadwiga Ogre shop slot is duplicated in the parsed product table.")
    if not matches:
        return None
    product = matches[0]
    path = getattr(product, "article_package_path", None)
    if path is None:
        path = getattr(product, "original_product_path", None)
    if not isinstance(path, str) or path.casefold() != OGRE_ARTICLE.casefold():
        raise ValueError("The Jadwiga Ogre shop slot no longer contains DA_Jail_Ogre.")
    return product


# Build ogre shop mirror assignment
def build_ogre_shop_mirror_assignment(
    starting_jail_article_path: str,
    shop_product: ShopTableProduct,
) -> ShopAssignment | None:
    """Make Jadwiga's row mirror the one Jail awarded in Initial Inventory.

    Both routes resolve through the same Jail Article and its unique story
    flag. The row is excluded from Shop/World pools and follows the starting
    assignment, so moving the starting Jail never creates an independent
    second Ogre occurrence.
    """
    if not is_ogre_shop_slot(shop_product):
        raise ValueError("The Ogre mirror assignment target is not the audited Jadwiga shop row.")
    if shop_product.article_package_path.casefold() != OGRE_ARTICLE.casefold():
        raise ValueError("The Jadwiga Ogre shop slot no longer contains DA_Jail_Ogre.")
    if (
        not isinstance(starting_jail_article_path, str)
        or not starting_jail_article_path.casefold().startswith("/game/blueprint/article/jail/")
        or not starting_jail_article_path.rsplit("/", 1)[-1].casefold().startswith("da_jail_")
    ):
        raise ValueError("The assigned starting Jail is not a canonical player Jail Article.")
    soft_path = SoftObjectPathIdentity.from_literal_path(starting_jail_article_path)
    if soft_path.resolved_path.casefold() == OGRE_ARTICLE.casefold():
        return None
    source = ArticleRewardIdentity(
        source_id="linked-jail-ogre:jadwiga-starting-jail-mirror",
        canonical_path=soft_path.resolved_path,
        article_path=soft_path.package_name.value,
        item_object=soft_path.asset_name.value,
        article_path_number=soft_path.package_name.number,
        item_object_number=soft_path.asset_name.number,
        origin_kind="LINKED_INITIAL_INVENTORY",
        origin_label="Jadwiga's starting Jail alternate route",
        content_class=(
            "DLC" if "/game/blueprint/article/jail/dlc/" in soft_path.resolved_path.casefold()
            else "BASE"
        ),
    )
    return ShopAssignment(shop_product, source, article_only=True)


# Build stinger shop mirror assignment
def build_stinger_shop_mirror_assignment(
    world_plan: WorldLootRandomizationPlan,
    shop_product: ShopTableProduct,
) -> ShopAssignment | None:
    """Mirror Josee's randomized Outpost reward into its one-time shop alternate.

    The shop row and the Outpost pickup share the same acquisition flag. The
    shop row is therefore a second serialized route for one logical reward,
    not an additional source or destination in the permutation.
    """
    if not is_stinger_shop_slot(shop_product):
        raise ValueError("The Stinger mirror assignment target is not the audited Josee shop row.")
    matches = [
        assignment for assignment in world_plan.assignments
        if assignment.destination.package_path.casefold() == STINGER_WORLD_PACKAGE.casefold()
        and assignment.destination.export_name.casefold() == STINGER_WORLD_EXPORT_NAME.casefold()
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one canonical Josee Stinger World Loot assignment, found {len(matches)}.")
    reward = matches[0].assigned_rewards[0]
    soft_path = reward.soft_object_path_identity or SoftObjectPathIdentity.from_literal_path(reward.article_path)
    if soft_path.resolved_path.casefold() == STINGER_ARTICLE.casefold():
        return None
    source = ArticleRewardIdentity(
        source_id="linked-jail-stinger:josee-shop-alternate",
        canonical_path=soft_path.resolved_path,
        article_path=soft_path.package_name.value,
        item_object=soft_path.asset_name.value,
        article_path_number=soft_path.package_name.number,
        item_object_number=soft_path.asset_name.number,
        origin_kind="LINKED_WORLD_REWARD",
        origin_label="Josee's Outpost Stinger alternate route",
        content_class=reward.content_class,
    )
    return ShopAssignment(shop_product, source, article_only=True)


__all__ = [
    "JAIL_SHOP_ROUTES",
    "OGRE_ARTICLE",
    "OGRE_SHOP_FAMILY",
    "OGRE_SHOP_PRODUCT_INDEX",
    "OGRE_SHOP_ROW",
    "JailShopRoute",
    "STINGER_ARTICLE",
    "STINGER_SHOP_FAMILY",
    "STINGER_SHOP_PRODUCT_INDEX",
    "STINGER_SHOP_ROW",
    "STINGER_WORLD_EXPORT",
    "STINGER_WORLD_EXPORT_NAME",
    "STINGER_WORLD_PACKAGE",
    "build_stinger_shop_mirror_assignment",
    "build_ogre_shop_mirror_assignment",
    "exclude_linked_jail_shop_routes",
    "find_stinger_shop_product",
    "find_ogre_shop_product",
    "is_ogre_shop_slot",
    "is_stinger_shop_slot",
    "validate_jail_shop_routes",
]
