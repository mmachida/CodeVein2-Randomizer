"""Deterministic permutations of the mastery Booster reward identity bundles."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from cv2randomizer.core.determinism import StableRandom
from cv2randomizer.core.seed import NormalizedSeed, normalize_seed
from cv2randomizer.game.booster_catalog import (
    BoosterCatalog,
    BoosterMasterySlot,
    BoosterRewardIdentity,
)


class BoosterPlanError(ValueError):
    """Raised when a Booster mastery reward pool cannot be safely permuted."""


@dataclass(frozen=True, slots=True)
class BoosterAssignment:
    destination: BoosterMasterySlot
    source: BoosterMasterySlot

    # List assigned reward
    @property
    def assigned_reward(self) -> BoosterRewardIdentity:
        return self.source.original_reward


@dataclass(frozen=True, slots=True)
class BoosterPoolStats:
    content_class: str
    slot_count: int
    changed_count: int


@dataclass(frozen=True, slots=True)
class BoosterPlan:
    seed: NormalizedSeed
    catalog_revision: str
    mix_postgame_dlc_with_base: bool
    assignments: tuple[BoosterAssignment, ...]
    pool_stats: tuple[BoosterPoolStats, ...]
    reward_multiset_preserved: bool

    # Find changed slots
    @property
    def changed_slots(self) -> int:
        return sum(item.changed_count for item in self.pool_stats)

    # Find unchanged assignments
    @property
    def self_maps(self) -> int:
        return sum(item.slot_count - item.changed_count for item in self.pool_stats)


# Create a seed-based random stream
def _rng(seed: NormalizedSeed, pool: str) -> StableRandom:
    material = f"cv2-boosters-v1\0{seed.internal_value}\0{pool}".encode("utf-8")
    return StableRandom.from_material(material)


# Build booster plan
def build_booster_plan(
    catalog: BoosterCatalog,
    seed: str | int | NormalizedSeed | None,
    *,
    mix_postgame_dlc_with_base: bool = False,
    enabled: bool = True,
) -> BoosterPlan:
    if type(mix_postgame_dlc_with_base) is not bool or type(enabled) is not bool:
        raise BoosterPlanError("Booster mix/enabled switches must be booleans.")
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    if not enabled:
        return BoosterPlan(normalized, catalog.revision, mix_postgame_dlc_with_base, (), (), True)
    slots = tuple(sorted(catalog.slots, key=lambda slot: (slot.row_index, slot.row_name.casefold())))
    _validate_slots(slots)
    grouped: dict[str, list[BoosterMasterySlot]] = {}
    for slot in slots:
        pool = "ALL" if mix_postgame_dlc_with_base else slot.content_class
        grouped.setdefault(pool, []).append(slot)
    assignments: list[BoosterAssignment] = []
    stats: list[BoosterPoolStats] = []
    for pool in sorted(grouped):
        destinations = grouped[pool]
        sources = list(destinations)
        _rng(normalized, pool).shuffle(sources)
        pool_assignments = tuple(BoosterAssignment(destination, source) for destination, source in zip(destinations, sources, strict=True))
        if Counter(item.assigned_reward.reward_id.casefold() for item in pool_assignments) != Counter(slot.original_reward.reward_id.casefold() for slot in destinations):
            raise BoosterPlanError(f"Booster reward identity multiset changed in pool {pool}.")
        assignments.extend(pool_assignments)
        stats.append(BoosterPoolStats(
            pool,
            len(destinations),
            sum(item.destination.original_reward != item.assigned_reward for item in pool_assignments),
        ))
    assignments.sort(key=lambda item: (item.destination.row_index, item.destination.row_name.casefold()))
    if (len(assignments) != len(slots)
            or len({item.destination.row_name.casefold() for item in assignments}) != len(slots)):
        raise BoosterPlanError("Booster plan must assign every available unique row exactly once.")
    return BoosterPlan(normalized, catalog.revision, mix_postgame_dlc_with_base, tuple(assignments), tuple(stats), True)


# Validate slots
def _validate_slots(slots: tuple[BoosterMasterySlot, ...]) -> None:
    if (len(slots) > 102 or any(not 0 <= slot.row_index < 102 for slot in slots)
            or len({slot.row_index for slot in slots}) != len(slots)):
        raise BoosterPlanError("Booster catalog contains duplicate or unexpected available row indexes.")
    if len({slot.row_name.casefold() for slot in slots}) != len(slots):
        raise BoosterPlanError("Booster catalog contains duplicate row names.")
    if len({slot.original_reward.reward_id.casefold() for slot in slots}) != len(slots):
        raise BoosterPlanError("Booster catalog contains duplicate reward identity bundles.")
    if any(not slot.content_class.strip() for slot in slots):
        raise BoosterPlanError("Booster catalog contains an empty content class.")
