"""Deterministic permutations for the separate direct Bequeathed Forma quest pool."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from cv2randomizer.core.determinism import StableRandom
from cv2randomizer.core.seed import NormalizedSeed, normalize_seed
from cv2randomizer.game.bequeathed_formae_catalog import BequeathedForma, BequeathedFormaCatalog


class BequeathedFormaPlanError(ValueError):
    """Raised when the direct Forma reward pool cannot be safely permuted."""


@dataclass(frozen=True, slots=True)
class BequeathedFormaAssignment:
    destination: BequeathedForma
    source: BequeathedForma


@dataclass(frozen=True, slots=True)
class BequeathedFormaPlan:
    seed: NormalizedSeed
    catalog_revision: str
    assignments: tuple[BequeathedFormaAssignment, ...]
    reward_multiset_preserved: bool

    # List changed rewards
    @property
    def changed_rewards(self) -> int:
        return sum(
            assignment.destination.asset_path.casefold() != assignment.source.asset_path.casefold()
            for assignment in self.assignments
        )

    # Count physical grant count
    @property
    def physical_grant_count(self) -> int:
        return sum(len(assignment.destination.direct_grants) for assignment in self.assignments)


# Build bequeathed formae plan
def build_bequeathed_formae_plan(
    catalog: BequeathedFormaCatalog,
    seed: str | int | NormalizedSeed | None,
    *,
    enabled: bool = True,
) -> BequeathedFormaPlan:
    if type(enabled) is not bool:
        raise BequeathedFormaPlanError("Bequeathed Forma enabled option must be a boolean.")
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    destinations = tuple(sorted(catalog.separate_direct_rewards, key=lambda item: (item.logical_reward_group or "", item.object.casefold())))
    if not enabled:
        return BequeathedFormaPlan(normalized, catalog.revision, (), True)
    if len(destinations) < 2:
        return BequeathedFormaPlan(normalized, catalog.revision, (), True)
    if any(not item.direct_grants for item in destinations):
        raise BequeathedFormaPlanError("Direct Bequeathed Forma pool entries need physical quest rewards.")
    groups = [item.logical_reward_group for item in destinations]
    if len(set(groups)) != len(groups) or len({item.asset_path.casefold() for item in destinations}) != 2:
        raise BequeathedFormaPlanError("Direct Bequeathed Forma rewards must have unique logical groups and assets.")

    sources = list(destinations)
    _direct_rng(normalized).shuffle(sources)
    assignments = tuple(
        BequeathedFormaAssignment(destination, source)
        for destination, source in zip(destinations, sources, strict=True)
    )
    if Counter(item.destination.asset_path.casefold() for item in assignments) != Counter(
        item.source.asset_path.casefold() for item in assignments
    ):
        raise BequeathedFormaPlanError("The direct Bequeathed Forma reward multiset changed during permutation.")
    return BequeathedFormaPlan(normalized, catalog.revision, assignments, True)


# Create direct RNG
def _direct_rng(seed: NormalizedSeed) -> StableRandom:
    material = f"cv2-bequeathed-formae-v1\0bequeathed_formae.direct\0{seed.internal_value}".encode("utf-8")
    return StableRandom.from_material(material)
