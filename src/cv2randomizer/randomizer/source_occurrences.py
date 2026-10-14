"""Stable source-occurrence inventory for cross-feature reward transfers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from cv2randomizer.game.catalogs import RewardPayload
from cv2randomizer.game.fname_identity import SoftObjectPathIdentity
from cv2randomizer.game.shop_tables import ShopTableProduct

from .shops import ArticleRewardIdentity, ShopAssignment, ShopRandomizationPlan
from .world_loot import (
    WorldLootAssignment,
    WorldLootRandomizationPlan,
    WorldLootRewardCandidate,
    world_loot_occurrence_id,
)


@dataclass(frozen=True, slots=True)
class RewardSourceOccurrence:
    occurrence_id: str
    article_path: str
    content_class: str

    # Validate initialized settings
    def __post_init__(self) -> None:
        if not self.occurrence_id.strip():
            raise ValueError("Reward source occurrence needs a stable identity.")
        if not self.article_path.startswith("/Game/") or "\\" in self.article_path:
            raise ValueError("Reward source occurrence needs a canonical /Game Article path.")
        if not self.content_class.strip():
            raise ValueError("Reward source occurrence needs a content class.")


# Read planned reward source occurrences
def planned_reward_source_occurrences(
    world_plan: WorldLootRandomizationPlan,
    shop_plan: ShopRandomizationPlan,
) -> tuple[RewardSourceOccurrence, ...]:
    """Return each active World/Shop source occurrence exactly once."""
    occurrences: list[RewardSourceOccurrence] = []
    for assignment in world_plan.assignments:
        if assignment.destination.mirror_of_package_path is not None:
            continue
        candidate = assignment.source_candidate
        if candidate is None:
            raise ValueError("World assignment has no source candidate while building occurrence inventory.")
        ids = candidate.source_occurrence_ids
        if not ids and candidate.source_location is not None:
            ids = tuple(
                world_loot_occurrence_id(candidate.source_location, index)
                for index in range(len(candidate.rewards))
            )
        if len(ids) != len(candidate.rewards):
            raise ValueError(f"World source {candidate.candidate_id!r} has incomplete occurrence identities.")
        occurrences.extend(
            RewardSourceOccurrence(identity, reward.article_path, reward.content_class)
            for identity, reward in zip(ids, candidate.rewards, strict=True)
        )

    for assignment in shop_plan.assignments:
        source = assignment.source
        if isinstance(source, ArticleRewardIdentity):
            occurrence_id = source.source_occurrence_id or source.source_id
            path = source.canonical_path
            content_class = source.content_class
        elif isinstance(source, ShopTableProduct):
            occurrence_id = _shop_occurrence_id(source)
            path = _numbered_path(source.article_path, source.article_path_number)
            content_class = source.what_content_class
        else:
            raise ValueError("Shop assignment has an unsupported source while building occurrence inventory.")
        occurrences.append(RewardSourceOccurrence(occurrence_id, path, content_class))

    ids = [item.occurrence_id.casefold() for item in occurrences]
    if len(ids) != len(set(ids)):
        raise ValueError("Active World/Shop plans contain duplicate source occurrence IDs.")
    return tuple(sorted(occurrences, key=lambda item: (item.occurrence_id.casefold(), item.occurrence_id)))


# Exchange planned reward occurrence
def exchange_planned_reward_occurrence(
    world_plan: WorldLootRandomizationPlan,
    shop_plan: ShopRandomizationPlan,
    *,
    source_occurrence_id: str,
    replacement_article_path: str,
    replacement_occurrence_id: str,
    replacement_content_class: str = "BASE",
) -> tuple[WorldLootRandomizationPlan, ShopRandomizationPlan]:
    """Transfer one source Article to an external reward and return its original reward.

    The source occurrence is consumed at the external grant, while that grant's
    original Article becomes a source occurrence in the source's previous slot.
    """
    source_key = source_occurrence_id.casefold()
    replacement_key = replacement_occurrence_id.casefold()
    if not source_key or not replacement_key or source_key == replacement_key:
        raise ValueError("Reward transfer needs two distinct occurrence identities.")
    if not replacement_article_path.startswith("/Game/") or "\\" in replacement_article_path:
        raise ValueError("Reward transfer replacement must be a canonical /Game Article path.")
    active = planned_reward_source_occurrences(world_plan, shop_plan)
    selected_source = next((item for item in active if item.occurrence_id.casefold() == source_key), None)
    if selected_source is None:
        raise ValueError(f"Reward source occurrence {source_occurrence_id!r} is not in an active pool.")
    if replacement_key in {item.occurrence_id.casefold() for item in active}:
        raise ValueError(f"Replacement occurrence {replacement_occurrence_id!r} is already active.")

    world_matches = 0
    world_assignments: list[WorldLootAssignment] = []
    for assignment in world_plan.assignments:
        candidate = assignment.source_candidate
        if candidate is None:
            world_assignments.append(assignment)
            continue
        ids = candidate.source_occurrence_ids
        if not ids and candidate.source_location is not None:
            ids = tuple(
                world_loot_occurrence_id(candidate.source_location, index)
                for index in range(len(candidate.rewards))
            )
        if source_key not in {identity.casefold() for identity in ids}:
            world_assignments.append(assignment)
            continue
        # Canonical location and its alternate-map mirror share one occurrence.
        # Update both serialized copies while counting only the canonical owner.
        index = next(i for i, identity in enumerate(ids) if identity.casefold() == source_key)
        rewards = list(candidate.rewards)
        rewards[index] = replace(
            rewards[index],
            article_path=replacement_article_path,
            content_class=replacement_content_class,
            soft_object_path_identity=SoftObjectPathIdentity.from_literal_path(replacement_article_path),
        )
        new_ids = list(ids)
        new_ids[index] = replacement_occurrence_id
        candidate = replace(
            candidate,
            candidate_id=f"source-transfer:{replacement_occurrence_id}",
            rewards=tuple(rewards),
            source_location=None,
            candidate_category=None,
            canonical_identity=None,
            identity_aliases=(),
            source_occurrence_ids=tuple(new_ids),
        )
        world_assignments.append(replace(assignment, source=None, source_candidate=candidate))
        if assignment.destination.mirror_of_package_path is None:
            world_matches += 1

    shop_matches = 0
    shop_assignments: list[ShopAssignment] = []
    for assignment in shop_plan.assignments:
        source = assignment.source
        if isinstance(source, ArticleRewardIdentity):
            occurrence_id = source.source_occurrence_id or source.source_id
        elif isinstance(source, ShopTableProduct):
            occurrence_id = _shop_occurrence_id(source)
        else:
            occurrence_id = ""
        if occurrence_id.casefold() != source_key:
            shop_assignments.append(assignment)
            continue
        identity = SoftObjectPathIdentity.from_literal_path(replacement_article_path)
        replacement = replace(
            source if isinstance(source, ArticleRewardIdentity) else _identity_from_product(source),
            source_id=replacement_occurrence_id,
            canonical_path=identity.resolved_path,
            article_path=identity.package_name.value,
            item_object=identity.asset_name.value,
            article_path_number=identity.package_name.number,
            item_object_number=identity.asset_name.number,
            origin_kind="SOURCE_TRANSFER",
            origin_label=f"source transfer {replacement_occurrence_id}",
            source_product=None,
            source_world_location=None,
            candidate_category=None,
            content_class=replacement_content_class,
            source_occurrence_id=replacement_occurrence_id,
        )
        shop_assignments.append(replace(assignment, source=replacement, article_only=True))
        shop_matches += 1

    if world_matches + shop_matches != 1:
        raise ValueError(
            f"Reward source occurrence {source_occurrence_id!r} was found in {world_matches + shop_matches} active owners."
        )

    updated_world = replace(world_plan, assignments=tuple(world_assignments))
    if source_key in {value.casefold() for value in world_plan.source_occurrence_ids}:
        ids = tuple(
            replacement_occurrence_id if value.casefold() == source_key else value
            for value in updated_world.source_occurrence_ids
        )
        article_paths = list(updated_world.source_article_paths)
        for index, article_path in enumerate(article_paths):
            if article_path.casefold() == selected_source.article_path.casefold():
                article_paths.pop(index)
                break
        else:
            raise ValueError("World source Article count does not contain the transferred occurrence.")
        article_paths.append(replacement_article_path)
        updated_world = replace(
            updated_world,
            source_occurrence_ids=tuple(sorted(ids, key=str.casefold)),
            source_article_paths=tuple(sorted(article_paths, key=str.casefold)),
        )
    updated_shop = replace(shop_plan, assignments=tuple(shop_assignments))
    planned_reward_source_occurrences(updated_world, updated_shop)
    return updated_world, updated_shop


# Find shop occurrence ID
def _shop_occurrence_id(product: ShopTableProduct) -> str:
    return f"shop:{product.family.upper()}:{product.shop_row}:{product.product_index}"


# Resolve numbered path
def _numbered_path(path: str, number: int) -> str:
    return path if number == 0 else f"{path}_{number - 1}"


# Resolve identity from product
def _identity_from_product(product: ShopTableProduct) -> ArticleRewardIdentity:
    identity = SoftObjectPathIdentity.from_literal_path(_numbered_path(product.article_path, product.article_path_number))
    occurrence_id = _shop_occurrence_id(product)
    return ArticleRewardIdentity(
        source_id=occurrence_id,
        canonical_path=identity.resolved_path,
        article_path=identity.package_name.value,
        item_object=identity.asset_name.value,
        article_path_number=identity.package_name.number,
        item_object_number=identity.asset_name.number,
        origin_kind="SHOP",
        origin_label=f"{product.family}:{product.shop_row}:{product.product_index}",
        source_product=product,
        candidate_category="shop",
        content_class=product.what_content_class,
        source_occurrence_id=occurrence_id,
    )


__all__ = [
    "RewardSourceOccurrence",
    "exchange_planned_reward_occurrence",
    "planned_reward_source_occurrences",
]
