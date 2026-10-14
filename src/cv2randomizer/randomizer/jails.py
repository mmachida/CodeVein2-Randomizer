"""World Loot reward candidates backed by the canonical player Jail catalog."""

from __future__ import annotations

from cv2randomizer.game.catalogs import RewardPayload, WorldLootLocation
from cv2randomizer.game.fname_identity import SoftObjectPathIdentity
from cv2randomizer.game.jail_catalog import JailCatalog, JailReward
from cv2randomizer.randomizer.world_loot import WorldLootRewardCandidate, world_loot_occurrence_id


# List jail world candidates
def jail_world_candidates(
    catalog: JailCatalog,
    *,
    world_loot: tuple[WorldLootLocation, ...] | None = None,
) -> tuple[WorldLootRewardCandidate, ...]:
    """Return canonical Jail identities without creating synthetic locations."""
    rewards_by_source: dict[tuple[str, int], WorldLootLocation] = {}
    if world_loot is not None:
        rewards_by_source = {
            (location.package_path.casefold(), location.export_index): location
            for location in world_loot
        }
    return tuple(
        WorldLootRewardCandidate(
            candidate_id=f"jail:{item.asset_path.casefold()}",
            rewards=(
                _jail_reward_payload(item, rewards_by_source)
                if world_loot is not None
                else RewardPayload(item.asset_path, 1, True, item.content_class),
            ),
            source_location=(
                _jail_source_location(item, rewards_by_source)
                if world_loot is not None else None
            ),
            candidate_category="jail",
            canonical_identity=item.asset_path,
            # Catalog identity is only used to classify a physical occurrence;
            # it never creates an additional copy on its own.
            identity_aliases=(item.asset_path,),
            source_occurrence_ids=(
                (_jail_occurrence_id(item, rewards_by_source),)
                if world_loot is not None else ()
            ),
        )
        for item in sorted(catalog.items, key=lambda item: item.asset_path.casefold())
        if item.world_reward_eligible
        and item.vanilla_source.source_type in {"GROUND_PICKUP", "TREASURE_BOX"}
        and item.vanilla_source.movable_world_loot
        # A catalog-validated Jail may still have no active World Loot WHERE
        # in a particular build (for example, the item's map package is absent).
        # Keep it vanilla and leave the rest of the unique Jail pool available.
        and (world_loot is None or _jail_source_location(item, rewards_by_source) is not None)
    )


# Find jail source location
def _jail_source_location(
    item: JailReward,
    sources: dict[tuple[str, int], WorldLootLocation],
) -> WorldLootLocation | None:
    source = item.vanilla_source
    if source.package_path is None or source.export_index is None:
        return None
    return sources.get((source.package_path.casefold(), source.export_index))


# Read jail occurrence ID
def _jail_occurrence_id(
    item: JailReward,
    sources: dict[tuple[str, int], WorldLootLocation],
) -> str:
    source = item.vanilla_source
    if source.package_path is None or source.export_index is None or source.reward_index is None:
        raise ValueError(f"Movable Jail reward {item.object} has no physical source occurrence.")
    location = sources.get((source.package_path.casefold(), source.export_index))
    if location is None:
        raise ValueError(f"Movable Jail reward {item.object} is missing from the World Loot source catalog.")
    return world_loot_occurrence_id(location, source.reward_index)


# Read jail reward payload
def _jail_reward_payload(item: JailReward, sources: dict[tuple[str, int], WorldLootLocation]) -> RewardPayload:
    """Reuse exact source FName identity from the audited World Loot location."""
    source = item.vanilla_source
    if source.source_type == "INITIAL_INVENTORY":
        # Starting Jail is a canonical catalog candidate rather than a World
        # Loot source. The Initial Inventory patcher serializes assigned paths
        # as literal FNames, so use the same representation here.
        return RewardPayload(
            item.asset_path,
            1,
            True,
            item.content_class,
            SoftObjectPathIdentity.from_literal_path(item.asset_path),
        )
    if source.package_path is None or source.export_index is None or source.reward_index is None:
        raise ValueError(f"Jail reward {item.object} has no audited physical Article source.")
    location = sources.get((source.package_path.casefold(), source.export_index))
    if location is None or source.reward_index >= len(location.rewards):
        raise ValueError(f"Jail reward {item.object} is missing its audited World Loot source location.")
    reward = location.rewards[source.reward_index]
    if reward.article_path.casefold() != item.asset_path.casefold():
        raise ValueError(f"Jail reward {item.object} differs from its audited World Loot source.")
    return RewardPayload(
        item.asset_path,
        1,
        True,
        item.content_class,
        reward.soft_object_path_identity or SoftObjectPathIdentity.from_literal_path(item.asset_path),
    )
