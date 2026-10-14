"""Deterministic permutations of the confirmed direct Blood Code grants."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from cv2randomizer.core.determinism import StableRandom
from cv2randomizer.core.seed import NormalizedSeed, normalize_seed
from cv2randomizer.game.blood_code_catalog import BloodCodeCatalog, BloodCodeGrant

class BloodCodePlanError(ValueError):
    """Raised when a Blood Code grant pool cannot be safely permuted."""


@dataclass(frozen=True, slots=True)
class BloodCodeAssignment:
    destination: BloodCodeGrant
    source: BloodCodeGrant


@dataclass(frozen=True, slots=True)
class BloodCodePoolStats:
    content_class: str
    slot_count: int
    changed_count: int


@dataclass(frozen=True, slots=True)
class BloodCodePlan:
    seed: NormalizedSeed
    catalog_revision: str
    mix_postgame_dlc_with_base: bool
    assignments: tuple[BloodCodeAssignment, ...]
    pool_stats: tuple[BloodCodePoolStats, ...]
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
    material = f"cv2-blood-codes-v1\0{seed.internal_value}\0{pool}".encode("utf-8")
    return StableRandom.from_material(material)


# Build blood code plan
def build_blood_code_plan(
    catalog: BloodCodeCatalog,
    seed: str | int | NormalizedSeed | None,
    *,
    mix_postgame_dlc_with_base: bool = False,
    enabled: bool = True,
) -> BloodCodePlan:
    if type(mix_postgame_dlc_with_base) is not bool or type(enabled) is not bool:
        raise BloodCodePlanError("Blood Code mix/enabled switches must be booleans.")
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    if not enabled:
        return BloodCodePlan(normalized, catalog.revision, mix_postgame_dlc_with_base, (), (), True)
    slots = tuple(sorted(catalog.slots, key=lambda slot: slot.sort_key))
    _validate_slots(slots)
    grouped: dict[str, list[BloodCodeGrant]] = {}
    for slot in slots:
        pool = "ALL" if mix_postgame_dlc_with_base else slot.content_class
        grouped.setdefault(pool, []).append(slot)
    assignments: list[BloodCodeAssignment] = []
    stats: list[BloodCodePoolStats] = []
    for pool in sorted(grouped):
        destinations = grouped[pool]
        sources = list(destinations)
        _rng(normalized, pool).shuffle(sources)
        pool_assignments = tuple(BloodCodeAssignment(destination, source) for destination, source in zip(destinations, sources, strict=True))
        if Counter(item.source.original_blood_code_package.casefold() for item in pool_assignments) != Counter(slot.original_blood_code_package.casefold() for slot in destinations):
            raise BloodCodePlanError(f"Blood Code reward multiset changed in pool {pool}.")
        assignments.extend(pool_assignments)
        stats.append(BloodCodePoolStats(
            pool,
            len(destinations),
            sum(item.destination.original_blood_code_package.casefold() != item.source.original_blood_code_package.casefold() for item in pool_assignments),
        ))
    assignments.sort(key=lambda item: item.destination.slot_id.casefold())
    if len(assignments) != len(slots) or len({item.destination.slot_id for item in assignments}) != len(slots):
        raise BloodCodePlanError("Blood Code plan must assign every available unique destination exactly once.")
    return BloodCodePlan(normalized, catalog.revision, mix_postgame_dlc_with_base, tuple(assignments), tuple(stats), True)


# Validate slots
def _validate_slots(slots: tuple[BloodCodeGrant, ...]) -> None:
    if len({slot.slot_id for slot in slots}) != len(slots):
        raise BloodCodePlanError("Blood Code catalog contains duplicate or unexpected slots.")
    if any(not slot.content_class.strip() for slot in slots):
        raise BloodCodePlanError("Blood Code catalog contains an empty content class.")
    source_identities = [slot.original_blood_code_package.casefold() for slot in slots]
    if len(set(source_identities)) != len(source_identities):
        raise BloodCodePlanError("Blood Code catalog contains duplicate logical reward identities.")
