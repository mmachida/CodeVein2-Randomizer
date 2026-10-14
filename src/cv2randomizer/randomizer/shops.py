"""Deterministic shop-product payload assignments."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from ..core.determinism import StableRandom, canonical_sort_key
from ..core.seed import NormalizedSeed, normalize_seed
from ..game.shop_packages import SHOP_TABLE_NAMES
from ..game.catalogs import ShopProduct
from ..game.shop_tables import ShopTableProduct
from ..game.fname_identity import FNameIdentity, SoftObjectPathIdentity
from .world_loot import (
    WorldLootAssignment,
    WorldLootRandomizationPlan,
    WorldLootRewardCandidate,
    WorldLootLocation,
    world_loot_occurrence_id,
)

ShopSlot = tuple[str, str, int]
NPC_TRADER_FAMILIES = frozenset({"TRADE", "TRADE_DLC"})


# Check NPC trader row
def is_npc_trader_row(family: str, shop_row: str) -> bool:
    """Return whether a catalog/parser row is one of the companion trader rows."""
    return family.upper() in NPC_TRADER_FAMILIES and shop_row.startswith("Character.Type.Buddy.")


@dataclass(frozen=True, slots=True)
class ArticleRewardIdentity:
    """An Article identity detached from origin-specific reward metadata."""

    source_id: str
    canonical_path: str
    article_path: str
    item_object: str
    article_path_number: int
    item_object_number: int
    origin_kind: str
    origin_label: str
    source_product: ShopTableProduct | None = None
    source_world_location: WorldLootLocation | None = None
    candidate_category: str | None = None
    content_class: str = "UNKNOWN"
    source_occurrence_id: str = ""

    # Resolve soft object path identity
    @property
    def soft_object_path_identity(self) -> SoftObjectPathIdentity:
        return SoftObjectPathIdentity(
            FNameIdentity(self.article_path, self.article_path_number),
            FNameIdentity(self.item_object, self.item_object_number),
        )


@dataclass(frozen=True, slots=True)
class ShopAssignment:
    """Move one source item's serialized payload into a destination slot."""

    destination: ShopTableProduct
    source: ShopTableProduct | ArticleRewardIdentity
    article_only: bool = False


@dataclass(frozen=True, slots=True)
class NpcTraderPlanSummary:
    """Trader-specific diagnostics attached to the shared Shop plan."""

    mode: str
    available_families: tuple[str, ...]
    unavailable_families: tuple[str, ...]
    available_rows: tuple[str, ...]
    available_slots: int
    eligible_slots: int
    assignments: tuple[ShopAssignment, ...]
    locked_slots: tuple[ShopTableProduct, ...]
    unvalidated_slots: tuple[ShopTableProduct, ...]
    reserved_slots: tuple[ShopTableProduct, ...]
    cross_companion_assignments: int
    trader_from_world: int = 0
    world_from_trader: int = 0
    trader_from_shop: int = 0
    shop_from_trader: int = 0
    disabled_slots: tuple[ShopTableProduct, ...] = ()


@dataclass(frozen=True, slots=True)
class ShopRandomizationPlan:
    """Permutation plus slots left unchanged for explicit safety reasons."""

    assignments: tuple[ShopAssignment, ...]
    locked_slots: tuple[ShopTableProduct, ...]
    unvalidated_slots: tuple[ShopTableProduct, ...]
    disabled_slots: tuple[ShopTableProduct, ...]
    mix_postgame_dlc_with_base: bool = False
    available_families: tuple[str, ...] = ()
    unavailable_families: tuple[str, ...] = ()
    mix_shops_with_world: bool = False
    global_world_locations: int = 0
    global_shop_locations: int = 0
    global_candidate_count: int = 0
    global_world_from_shop: int = 0
    global_shop_from_world: int = 0
    extractor_reserved_slots: tuple[ShopTableProduct, ...] = ()
    requires_name_map_preparation: bool = False
    npc_traders: NpcTraderPlanSummary | None = None
    # Additional physical Shop edits outside the normal permutation pool.
    # These do not count as source occurrences or normal Shop assignments.
    linked_assignments: tuple[ShopAssignment, ...] = ()


# List slot
def _slot(product: ShopTableProduct) -> ShopSlot:
    return (product.family.upper(), product.shop_row, product.product_index)


# Explain shop slot lock reason
def shop_slot_lock_reason(product: ShopTableProduct) -> str | None:
    """Return the single safety reason that keeps a Shop slot out of every pool.

    A false flag is infinite. When unversioned serialization zero-masks the bool,
    the schema default (False) is known even though the raw payload is absent. A
    finite slot still needs explicit raw bytes for both fields, a positive stock
    count, and no disagreement with the validated catalog.
    """
    return _shop_stock_lock_reason(
        protected=product.protected,
        protection_reason=product.protection_reason,
        logical_uses_stock=product.logical_uses_stock,
        has_explicit_stock_payload=product.has_explicit_stock_payload,
        catalog_uses_stock=product.catalog_uses_stock,
        stock=product.stock,
        catalog_stock=product.catalog_stock,
        destination_uses_stock=product.destination_uses_stock,
        masked_default_false=(
            product.uses_stock is None
            and product.uses_stock_bytes is None
            and product.uses_stock_mask_bit is not None
        ),
    )


# Explain shop catalog slot lock reason
def shop_catalog_slot_lock_reason(product: ShopProduct) -> str | None:
    """Apply the production Shop eligibility policy to a verified runtime catalog row.

    The static catalog carries explicit decoded ``uses_stock`` and ``stock``
    values. Runtime Generate calls :func:`shop_slot_lock_reason` on the cooked,
    reopened table and additionally requires serialized field bytes. Keeping
    the semantic checks in one function makes diagnostics match the same finite
    stock, protection, and unknown-semantics rules.
    """
    return _shop_stock_lock_reason(
        protected=product.protected,
        protection_reason=product.protection_reason,
        logical_uses_stock=product.uses_stock,
        has_explicit_stock_payload=True,
        catalog_uses_stock=product.uses_stock,
        stock=product.stock,
        catalog_stock=product.stock,
        destination_uses_stock=product.uses_stock,
        masked_default_false=False,
    )


# Explain shop stock lock reason
def _shop_stock_lock_reason(
    *,
    protected: bool,
    protection_reason: str | None,
    logical_uses_stock: bool | None,
    has_explicit_stock_payload: bool,
    catalog_uses_stock: bool | None,
    stock: int | None,
    catalog_stock: int | None,
    destination_uses_stock: bool | None,
    masked_default_false: bool,
) -> str | None:
    if protected:
        return (protection_reason or "PROTECTED").upper()

    if logical_uses_stock is False:
        # The mask means the encoded value is exactly the schema default False.
        # Some catalog rows captured a stale True value; the cooked source table
        # is authoritative for this slot's runtime behavior.
        if masked_default_false:
            return "INFINITE_SHOP_STOCK"
        if catalog_uses_stock is True:
            return "UNKNOWN_SHOP_STOCK_SEMANTICS"
        return "INFINITE_SHOP_STOCK"

    if logical_uses_stock is True and catalog_uses_stock is False:
        return "UNKNOWN_SHOP_STOCK_SEMANTICS"

    if (
        catalog_stock is not None
        and stock is not None
        and catalog_stock != stock
    ):
        return "UNKNOWN_SHOP_STOCK_SEMANTICS"

    if destination_uses_stock is False:
        return "INFINITE_SHOP_STOCK"

    if (
        not has_explicit_stock_payload
        or logical_uses_stock is not True
        or stock is None
        or stock <= 0
    ):
        return "UNKNOWN_SHOP_STOCK_SEMANTICS"
    return None


# Create family RNG
def _family_rng(seed_value: int, family: str) -> StableRandom:
    key = f"cv2-shop-payload-v1\0{seed_value}\0{family}".encode("utf-8")
    return StableRandom.from_material(key)


# Create NPC trader RNG
def _npc_trader_rng(seed_value: int) -> StableRandom:
    key = f"cv2-randomizer:npc_traders.only:v1\0{seed_value}".encode("utf-8")
    return StableRandom.from_material(key)


# Build pool key
def _pool_key(family: str, mix_postgame_dlc_with_base: bool) -> str:
    if not mix_postgame_dlc_with_base:
        return family
    if family in {"NORMAL", "DLC"}:
        return "NORMAL+DLC"
    if family in {"TRADE", "TRADE_DLC"}:
        return "TRADE+TRADE_DLC"
    return family


# Read derangement
def _derangement(items: list[ShopTableProduct], rng: StableRandom) -> list[ShopTableProduct]:
    """Return a deterministic random permutation with no fixed slots when possible."""
    if len(items) < 2:
        return items.copy()
    for _ in range(1000):
        shuffled = items.copy()
        rng.shuffle(shuffled)
        if all(_slot(destination) != _slot(source) for destination, source in zip(items, shuffled)):
            return shuffled
    # The fallback is deterministic and still preserves every source exactly once.
    return items[1:] + items[:1]


# Build shop randomization plan
def build_shop_randomization_plan(
    products: Iterable[ShopTableProduct],
    seed: str | int | NormalizedSeed | None,
    *,
    enabled_families: Iterable[str] | None = None,
    excluded_slots: Iterable[ShopSlot] = (),
    mix_postgame_dlc_with_base: bool = False,
    unavailable_families: Iterable[str] = (),
    reserved_reward_identities: Iterable[str] = (),
) -> ShopRandomizationPlan:
    """Build separate deterministic permutations for safe, explicit product fields.

    Protected, infinite, and stock-uncertain slots stay out of both sides of the
    permutation. Only slots with explicit positive finite stock participate.
    """
    if type(mix_postgame_dlc_with_base) is not bool:
        raise ValueError("Shop content-mix switch must be a boolean.")
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    if enabled_families is None:
        families = tuple(SHOP_TABLE_NAMES)
    else:
        families = tuple(dict.fromkeys(value.strip().upper() for value in enabled_families))
        unknown = set(families) - set(SHOP_TABLE_NAMES)
        if unknown:
            raise ValueError(f"Unknown shop table families: {', '.join(sorted(unknown))}.")
    excluded = set(excluded_slots)
    unavailable = tuple(sorted({value.strip().upper() for value in unavailable_families}))
    if set(unavailable) - set(SHOP_TABLE_NAMES):
        raise ValueError("Unavailable shop families must be known table families.")
    reserved = {value.casefold() for value in reserved_reward_identities}

    by_family: dict[str, list[ShopTableProduct]] = {family: [] for family in SHOP_TABLE_NAMES}
    seen: set[ShopSlot] = set()
    all_products = tuple(products)
    for product in all_products:
        family = product.family.upper()
        if family not in SHOP_TABLE_NAMES:
            raise ValueError(f"Unknown shop table family {product.family!r}.")
        key = _slot(product)
        if key in seen:
            raise ValueError(f"Duplicate shop product slot {key!r}.")
        seen.add(key)
        by_family[family].append(product)
    unknown_exclusions = excluded - seen
    if unknown_exclusions:
        raise ValueError(f"Excluded shop slots are not present: {sorted(unknown_exclusions)!r}.")

    assignments: list[ShopAssignment] = []
    locked: list[ShopTableProduct] = []
    unvalidated: list[ShopTableProduct] = []
    extractor_reserved: list[ShopTableProduct] = []
    disabled: list[ShopTableProduct] = []
    pool_products: dict[str, list[ShopTableProduct]] = {}
    for family in SHOP_TABLE_NAMES:
        family_products = sorted(
            by_family[family], key=lambda product: (product.shop_row, product.product_index)
        )
        if family not in families:
            disabled.extend(family_products)
            continue
        eligible: list[ShopTableProduct] = []
        for product in family_products:
            if product.protected or _slot(product) in excluded:
                locked.append(product)
            elif _numbered_name(product.article_path, product.article_path_number).casefold() in reserved:
                locked.append(product)
                extractor_reserved.append(product)
            else:
                reason = shop_slot_lock_reason(product)
                if reason == "UNKNOWN_SHOP_STOCK_SEMANTICS":
                    unvalidated.append(product)
                elif reason is not None:
                    locked.append(product)
                else:
                    eligible.append(product)
        pool_products.setdefault(_pool_key(family, mix_postgame_dlc_with_base), []).extend(eligible)

    for pool_key, eligible in sorted(pool_products.items(), key=lambda item: canonical_sort_key(item[0])):
        sources = _derangement(eligible, _family_rng(normalized.internal_value, pool_key))
        assignments.extend(
            ShopAssignment(destination=destination, source=source)
            for destination, source in zip(eligible, sources, strict=True)
        )
    return ShopRandomizationPlan(
        assignments=tuple(assignments),
        locked_slots=tuple(locked),
        unvalidated_slots=tuple(unvalidated),
        disabled_slots=tuple(disabled),
        mix_postgame_dlc_with_base=mix_postgame_dlc_with_base,
        available_families=tuple(family for family in SHOP_TABLE_NAMES if by_family[family]),
        unavailable_families=unavailable,
        extractor_reserved_slots=tuple(extractor_reserved),
    )


# Build NPC trader only plan
def build_npc_trader_only_plan(
    products: Iterable[ShopTableProduct],
    seed: str | int | NormalizedSeed | None,
    *,
    unavailable_families: Iterable[str] = (),
    reserved_reward_identities: Iterable[str] = (),
) -> ShopRandomizationPlan:
    """Shuffle all available Buddy trader rows together under one RNG domain."""
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    all_products = tuple(products)
    seen: set[ShopSlot] = set()
    reserved = {value.casefold() for value in reserved_reward_identities}
    eligible: list[ShopTableProduct] = []
    locked: list[ShopTableProduct] = []
    unvalidated: list[ShopTableProduct] = []
    extractor_reserved: list[ShopTableProduct] = []
    for product in sorted(all_products, key=lambda item: _slot(item)):
        family = product.family.upper()
        key = _slot(product)
        if family not in NPC_TRADER_FAMILIES or not is_npc_trader_row(family, product.shop_row):
            raise ValueError(f"NPC Trader plan received a non-trader slot {key!r}.")
        if key in seen:
            raise ValueError(f"Duplicate NPC Trader product slot {key!r}.")
        seen.add(key)
        if product.protected:
            locked.append(product)
        elif _numbered_name(product.article_path, product.article_path_number).casefold() in reserved:
            locked.append(product)
            extractor_reserved.append(product)
        else:
            reason = shop_slot_lock_reason(product)
            if reason == "UNKNOWN_SHOP_STOCK_SEMANTICS":
                unvalidated.append(product)
            elif reason is not None:
                locked.append(product)
            else:
                eligible.append(product)

    sources = _derangement(eligible, _npc_trader_rng(normalized.internal_value))
    assignments = tuple(
        ShopAssignment(destination=destination, source=source)
        for destination, source in zip(eligible, sources, strict=True)
    )
    requires_name_map = any(
        assignment.destination.family.upper() != assignment.source.family.upper()
        for assignment in assignments
    )
    unavailable = tuple(sorted({value.strip().upper() for value in unavailable_families}))
    invalid = set(unavailable) - NPC_TRADER_FAMILIES
    if invalid:
        raise ValueError(f"Unavailable NPC Trader families are invalid: {', '.join(sorted(invalid))}.")
    return ShopRandomizationPlan(
        assignments=assignments,
        locked_slots=tuple(locked),
        unvalidated_slots=tuple(unvalidated),
        disabled_slots=(),
        available_families=tuple(family for family in ("TRADE", "TRADE_DLC")
                                 if any(item.family.upper() == family for item in all_products)),
        unavailable_families=unavailable,
        extractor_reserved_slots=tuple(extractor_reserved),
        requires_name_map_preparation=requires_name_map,
    )


# Combine shop randomization plans
def combine_shop_randomization_plans(
    *plans: ShopRandomizationPlan,
) -> ShopRandomizationPlan:
    """Combine disjoint Shop sub-pools while retaining global-pool diagnostics."""
    assignments = tuple(item for plan in plans for item in plan.assignments)
    destinations = [_slot(item.destination) for item in assignments]
    if len(destinations) != len(set(destinations)):
        raise ValueError("Combined Shop plans assign one destination more than once.")
    global_plan = next((plan for plan in plans if plan.mix_shops_with_world), None)
    return ShopRandomizationPlan(
        assignments=assignments,
        locked_slots=tuple(item for plan in plans for item in plan.locked_slots),
        unvalidated_slots=tuple(item for plan in plans for item in plan.unvalidated_slots),
        disabled_slots=tuple(item for plan in plans for item in plan.disabled_slots),
        mix_postgame_dlc_with_base=any(plan.mix_postgame_dlc_with_base for plan in plans),
        available_families=tuple(family for family in SHOP_TABLE_NAMES
                                 if any(family in plan.available_families for plan in plans)),
        unavailable_families=tuple(sorted({family for plan in plans for family in plan.unavailable_families})),
        mix_shops_with_world=global_plan is not None,
        global_world_locations=global_plan.global_world_locations if global_plan else 0,
        global_shop_locations=global_plan.global_shop_locations if global_plan else 0,
        global_candidate_count=global_plan.global_candidate_count if global_plan else 0,
        global_world_from_shop=global_plan.global_world_from_shop if global_plan else 0,
        global_shop_from_world=global_plan.global_shop_from_world if global_plan else 0,
        extractor_reserved_slots=tuple(item for plan in plans for item in plan.extractor_reserved_slots),
        requires_name_map_preparation=any(plan.requires_name_map_preparation for plan in plans),
        linked_assignments=tuple(item for plan in plans for item in plan.linked_assignments),
    )


# Resolve numbered name
def _numbered_name(value: str, number: int) -> str:
    return value if number == 0 else f"{value}_{number - 1}"


# Resolve identity from world
def _identity_from_world(assignment: WorldLootAssignment) -> ArticleRewardIdentity:
    reward = assignment.assigned_rewards[0]
    soft_path = reward.soft_object_path_identity or SoftObjectPathIdentity.from_literal_path(
        reward.article_path,
    )
    if soft_path.resolved_path != reward.article_path:
        raise ValueError("World reward FName identity does not resolve to its canonical Article path.")
    item_object = soft_path.asset_name.value
    source_candidate = assignment.source_candidate
    source_location = (
        source_candidate.source_location if source_candidate is not None else assignment.source
    )
    origin_label = (
        source_candidate.candidate_id if source_candidate is not None
        else f"{assignment.destination.package_path}#{assignment.destination.export_index}"
    )
    return ArticleRewardIdentity(
        source_id=f"world:{assignment.destination.package_path.casefold()}#{assignment.destination.export_index}",
        canonical_path=soft_path.resolved_path,
        article_path=soft_path.package_name.value,
        item_object=item_object,
        article_path_number=soft_path.package_name.number,
        item_object_number=soft_path.asset_name.number,
        origin_kind="WORLD",
        origin_label=origin_label,
        source_world_location=source_location,
        candidate_category=(source_candidate.candidate_category if source_candidate else None),
        content_class=reward.content_class,
        source_occurrence_id=(
            source_candidate.source_occurrence_ids[0]
            if source_candidate is not None and len(source_candidate.source_occurrence_ids) == 1
            else world_loot_occurrence_id(source_location, 0)
            if source_location is not None
            else ""
        ),
    )


# Resolve identity from shop
def _identity_from_shop(product: ShopTableProduct) -> ArticleRewardIdentity:
    canonical_path = _numbered_name(product.article_path, product.article_path_number)
    object_name = product.item_object
    if product.item_object_number:
        object_name = _numbered_name(object_name, product.item_object_number)
    return ArticleRewardIdentity(
        source_id=f"shop:{product.family.upper()}:{product.shop_row}:{product.product_index}",
        canonical_path=canonical_path,
        article_path=product.article_path,
        item_object=product.item_object,
        article_path_number=product.article_path_number,
        item_object_number=product.item_object_number,
        origin_kind="SHOP",
        origin_label=f"{product.family}:{product.shop_row}:{product.product_index}",
        source_product=product,
        candidate_category="shop",
        content_class=product.what_content_class,
        source_occurrence_id=f"shop:{product.family.upper()}:{product.shop_row}:{product.product_index}",
    )


# Create global RNG
def _global_rng(seed_value: int) -> StableRandom:
    key = f"cv2-global-article-shop-world-v1\0{seed_value}".encode("utf-8")
    return StableRandom.from_material(key)


# Build global article mix plans
def build_global_article_mix_plans(
    world_plan: WorldLootRandomizationPlan,
    products: Iterable[ShopTableProduct],
    seed: str | int | NormalizedSeed | None,
    *,
    enabled_families: Iterable[str],
    mix_postgame_dlc_with_base: bool = False,
    unavailable_families: Iterable[str] = (),
    reserved_reward_identities: Iterable[str] = (),
    excluded_slots: Iterable[ShopSlot] = (),
) -> tuple[WorldLootRandomizationPlan, ShopRandomizationPlan]:
    """Join movable World singleton rewards and enabled Shop slots in one Article pool.

    World bundles remain under the existing whole-payload planner. In this mode
    Article identity alone crosses Shop/World boundaries; both destination types
    retain their own non-Article metadata.
    """
    if type(mix_postgame_dlc_with_base) is not bool:
        raise ValueError("Shop content-mix switch must be a boolean.")
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    families = tuple(dict.fromkeys(value.strip().upper() for value in enabled_families))
    unknown = set(families) - set(SHOP_TABLE_NAMES)
    if unknown:
        raise ValueError(f"Unknown shop table families: {', '.join(sorted(unknown))}.")
    unavailable = tuple(sorted({value.strip().upper() for value in unavailable_families}))
    if set(unavailable) - set(SHOP_TABLE_NAMES):
        raise ValueError("Unavailable shop families must be known table families.")
    reserved = {value.casefold() for value in reserved_reward_identities}
    excluded = set(excluded_slots)

    all_products = tuple(products)
    seen_slots: set[ShopSlot] = set()
    by_family: dict[str, list[ShopTableProduct]] = {family: [] for family in SHOP_TABLE_NAMES}
    for product in all_products:
        family = product.family.upper()
        if family not in SHOP_TABLE_NAMES:
            raise ValueError(f"Unknown shop table family {product.family!r}.")
        slot = _slot(product)
        if slot in seen_slots:
            raise ValueError(f"Duplicate shop product slot {slot!r}.")
        seen_slots.add(slot)
        by_family[family].append(product)
    unknown_exclusions = excluded - seen_slots
    if unknown_exclusions:
        raise ValueError(f"Excluded Shop slots are not present: {sorted(unknown_exclusions)!r}.")

    locked: list[ShopTableProduct] = []
    disabled: list[ShopTableProduct] = []
    unvalidated: list[ShopTableProduct] = []
    eligible_shop: list[ShopTableProduct] = []
    extractor_reserved: list[ShopTableProduct] = []
    for family, family_products in by_family.items():
        for product in sorted(family_products, key=lambda item: (item.shop_row, item.product_index)):
            if family not in families:
                disabled.append(product)
            elif _slot(product) in excluded:
                locked.append(product)
            elif product.protected:
                locked.append(product)
            elif _numbered_name(product.article_path, product.article_path_number).casefold() in reserved:
                locked.append(product)
                extractor_reserved.append(product)
            else:
                reason = shop_slot_lock_reason(product)
                if reason == "UNKNOWN_SHOP_STOCK_SEMANTICS":
                    unvalidated.append(product)
                elif reason is not None:
                    locked.append(product)
                else:
                    eligible_shop.append(product)

    flexible_world = [
        assignment for assignment in world_plan.assignments
        if (
            assignment.destination.mirror_of_package_path is None
            and len(assignment.destination.rewards) == 1
        )
    ]
    world_bundles = [
        assignment for assignment in world_plan.assignments
        if len(assignment.destination.rewards) != 1
    ]
    world_identities = [_identity_from_world(assignment) for assignment in flexible_world]
    shop_identities = [_identity_from_shop(product) for product in eligible_shop]
    identities = [*world_identities, *shop_identities]
    identities.sort(key=lambda item: item.source_id)
    destinations: list[tuple[str, object, str]] = [
        ("WORLD", assignment, identity.source_id)
        for assignment, identity in zip(flexible_world, world_identities, strict=True)
    ] + [
        ("SHOP", product, identity.source_id)
        for product, identity in zip(eligible_shop, shop_identities, strict=True)
    ]
    destinations.sort(key=lambda item: item[2])
    identities_by_id = {identity.source_id: identity for identity in identities}
    if len(identities_by_id) != len(identities) or len(destinations) != len(identities):
        raise ValueError("Global Shop/World Article inventory contains duplicate source identities.")
    occurrence_ids = [
        identity.source_occurrence_id or identity.source_id
        for identity in identities
    ]
    if len(occurrence_ids) != len(set(value.casefold() for value in occurrence_ids)):
        raise ValueError("Global Shop/World Article inventory contains duplicate source occurrences.")
    source_article_counts: dict[str, int] = {}
    for identity in identities:
        key = identity.canonical_path.casefold()
        source_article_counts[key] = source_article_counts.get(key, 0) + 1

    destination_keys = [item[2] for item in destinations]
    shuffled = identities.copy()
    rng = _global_rng(normalized.internal_value)
    if len(shuffled) > 1:
        for _ in range(1000):
            rng.shuffle(shuffled)
            if all(key != source.source_id for key, source in zip(destination_keys, shuffled, strict=True)):
                break
        else:
            shuffled = identities.copy()
            for shift in range(1, len(shuffled) + 1):
                rotated = shuffled[shift:] + shuffled[:shift]
                if all(key != source.source_id for key, source in zip(destination_keys, rotated, strict=True)):
                    shuffled = rotated
                    break

    world_assignment_by_slot: dict[tuple[str, int], WorldLootAssignment] = {}
    shop_assignments: list[ShopAssignment] = []
    world_from_shop = 0
    shop_from_world = 0
    for (kind, destination, _), source in zip(destinations, shuffled, strict=True):
        if kind == "WORLD":
            assert isinstance(destination, WorldLootAssignment)
            original_reward = destination.destination.rewards[0]
            new_candidate = WorldLootRewardCandidate(
                candidate_id=f"global-article:{source.source_id}",
                rewards=(replace(
                    original_reward,
                    article_path=source.canonical_path,
                    content_class=source.content_class,
                    soft_object_path_identity=source.soft_object_path_identity,
                ),),
                source_location=source.source_world_location,
                candidate_category=source.candidate_category or source.origin_kind.casefold(),
                canonical_identity=source.canonical_path,
                source_occurrence_ids=(source.source_occurrence_id or source.source_id,),
            )
            # Article identity moves; Count and bNotification stay at the World slot.
            world_assignment_by_slot[
                (destination.destination.package_path.casefold(), destination.destination.export_index)
            ] = WorldLootAssignment(
                destination=destination.destination,
                source=source.source_world_location,
                source_candidate=new_candidate,
            )
            if source.origin_kind == "SHOP":
                world_from_shop += 1
        else:
            assert isinstance(destination, ShopTableProduct)
            shop_assignments.append(ShopAssignment(destination, source, article_only=True))
            if source.origin_kind == "WORLD":
                shop_from_world += 1

    mixed_world_assignments_list: list[WorldLootAssignment] = []
    for assignment in world_plan.assignments:
        destination = assignment.destination
        if destination.mirror_of_package_path is None:
            key = (destination.package_path.casefold(), destination.export_index)
            mixed_world_assignments_list.append(world_assignment_by_slot.get(key, assignment))
            continue
        assert destination.mirror_of_export_index is not None
        canonical_key = (
            destination.mirror_of_package_path.casefold(),
            destination.mirror_of_export_index,
        )
        canonical = world_assignment_by_slot.get(canonical_key)
        if canonical is None:
            mixed_world_assignments_list.append(assignment)
        else:
            # A mirrored map serializes the same logical pickup in another
            # package. It follows its canonical destination but does not add
            # another source occurrence to the shared pool.
            mixed_world_assignments_list.append(WorldLootAssignment(
                destination=destination,
                source=canonical.source,
                source_candidate=canonical.source_candidate,
            ))
    mixed_world_assignments = tuple(mixed_world_assignments_list)
    mixed_world_plan = replace(
        world_plan,
        assignments=mixed_world_assignments,
        source_occurrence_ids=tuple(sorted(occurrence_ids, key=str.casefold)),
        source_article_paths=tuple(sorted(
            (identity.canonical_path for identity in identities), key=str.casefold,
        )),
    )
    shop_plan = ShopRandomizationPlan(
        assignments=tuple(sorted(shop_assignments, key=lambda assignment: _slot(assignment.destination))),
        locked_slots=tuple(locked),
        unvalidated_slots=tuple(unvalidated),
        disabled_slots=tuple(disabled),
        mix_postgame_dlc_with_base=mix_postgame_dlc_with_base,
        available_families=tuple(family for family in SHOP_TABLE_NAMES if by_family[family]),
        unavailable_families=unavailable,
        mix_shops_with_world=True,
        global_world_locations=len(flexible_world),
        global_shop_locations=len(eligible_shop),
        global_candidate_count=len(identities),
        global_world_from_shop=world_from_shop,
        global_shop_from_world=shop_from_world,
        extractor_reserved_slots=tuple(extractor_reserved),
    )
    assigned_occurrences: list[str] = []
    assigned_article_counts: dict[str, int] = {}
    for assignment in mixed_world_assignments:
        if (
            assignment.destination.mirror_of_package_path is not None
            or len(assignment.destination.rewards) != 1
        ):
            continue
        candidate = assignment.source_candidate
        if candidate is None or len(candidate.source_occurrence_ids) != 1 or len(candidate.rewards) != 1:
            raise ValueError("Global World Article assignment lost its source occurrence identity.")
        assigned_occurrences.append(candidate.source_occurrence_ids[0])
        key = candidate.rewards[0].soft_object_path_identity.resolved_path.casefold() \
            if candidate.rewards[0].soft_object_path_identity is not None \
            else candidate.rewards[0].article_path.casefold()
        assigned_article_counts[key] = assigned_article_counts.get(key, 0) + 1
    for assignment in shop_plan.assignments:
        source = assignment.source
        if not isinstance(source, ArticleRewardIdentity):
            raise ValueError("Global Shop assignment lost its source occurrence identity.")
        assigned_occurrences.append(source.source_occurrence_id or source.source_id)
        key = source.canonical_path.casefold()
        assigned_article_counts[key] = assigned_article_counts.get(key, 0) + 1
    if len(assigned_occurrences) != len(set(value.casefold() for value in assigned_occurrences)):
        raise ValueError("Global Shop/World assignment duplicated a source occurrence.")
    if set(value.casefold() for value in assigned_occurrences) != set(value.casefold() for value in occurrence_ids):
        raise ValueError("Global Shop/World assignment lost or introduced a source occurrence.")
    if source_article_counts != assigned_article_counts:
        raise ValueError("Global Shop/World Article multiset changed during cross-pool assignment.")
    return mixed_world_plan, shop_plan
