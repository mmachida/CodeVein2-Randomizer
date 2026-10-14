"""Deterministic, no-replacement reward assignment for Booster crafting recipes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from cv2randomizer.core.determinism import StableRandom, canonical_package_key, canonical_sort_key
from cv2randomizer.core.seed import NormalizedSeed, normalize_seed
from cv2randomizer.game.booster_reward_pool import BoosterRewardCandidate
from cv2randomizer.game.extractor_catalog import ExtractorCatalog, ExtractorRecipeSlot


class ExtractorPlanError(ValueError):
    """Raised when the available Booster identity pool cannot fill crafting recipes."""


@dataclass(frozen=True, slots=True)
class ExtractorAssignment:
    destination: ExtractorRecipeSlot
    assigned_reward: BoosterRewardCandidate


@dataclass(frozen=True, slots=True)
class ExtractorPoolStats:
    pool: str
    candidate_count: int
    destination_count: int
    unallocated_count: int


@dataclass(frozen=True, slots=True)
class ExtractorPlan:
    seed: NormalizedSeed
    catalog_revision: str
    mix_postgame_dlc_with_base: bool
    canonical_candidate_count: int
    assignments: tuple[ExtractorAssignment, ...]
    remaining_candidates: tuple[BoosterRewardCandidate, ...]
    pool_stats: tuple[ExtractorPoolStats, ...]

    # List changed rows
    @property
    def changed_rows(self) -> int:
        return sum(item.destination.original_article_path.casefold() != item.assigned_reward.article_path.casefold()
                   for item in self.assignments)

    # Find unchanged assignments
    @property
    def self_maps(self) -> int:
        return len(self.assignments) - self.changed_rows


# Build extractor plan
def build_extractor_plan(
    catalog: ExtractorCatalog,
    candidates: tuple[BoosterRewardCandidate, ...],
    seed: str | int | NormalizedSeed | None,
    *,
    mix_postgame_dlc_with_base: bool,
) -> ExtractorPlan:
    if type(mix_postgame_dlc_with_base) is not bool:
        raise ExtractorPlanError("Extractor content-mix switch must be a boolean.")
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    destinations = tuple(sorted(
        catalog.slots,
        key=lambda item: (canonical_sort_key(item.content_class), canonical_package_key(item.virtual_package), item.row_index),
    ))
    available = tuple(sorted(
        candidates,
        key=lambda item: canonical_package_key(item.article_path),
    ))
    path_keys = [item.article_path.casefold() for item in available]
    if len(path_keys) != len(set(path_keys)):
        raise ExtractorPlanError("Canonical Booster pool contains duplicate Article identities.")
    groups: dict[str, list[ExtractorRecipeSlot]] = {}
    for destination in destinations:
        key = "ALL" if mix_postgame_dlc_with_base else destination.content_class
        groups.setdefault(key, []).append(destination)
    assignments: list[ExtractorAssignment] = []
    stats: list[ExtractorPoolStats] = []
    allocated_paths: set[str] = set()
    for pool, pool_destinations in sorted(groups.items()):
        if mix_postgame_dlc_with_base:
            pool_candidates = list(available)
        else:
            pool_candidates = [item for item in available if item.content_class == pool]
        if len(pool_candidates) < len(pool_destinations):
            raise ExtractorPlanError(
                f"Pool {pool} has {len(pool_candidates)} unique Booster identities for "
                f"{len(pool_destinations)} Extractor recipe slots."
            )
        rng = _rng(normalized, pool)
        selected = rng.sample(pool_candidates, len(pool_destinations))
        # Keep each destination/reward pair deterministic and permit self-maps.
        selected.sort(key=lambda item: canonical_package_key(item.article_path))
        for destination, reward in zip(pool_destinations, selected, strict=True):
            assignments.append(ExtractorAssignment(destination, reward))
            allocated_paths.add(reward.article_path.casefold())
        stats.append(ExtractorPoolStats(pool, len(pool_candidates), len(pool_destinations),
                                        len(pool_candidates) - len(pool_destinations)))
    remaining = tuple(item for item in available if item.article_path.casefold() not in allocated_paths)
    if len(assignments) != len(destinations) or len(allocated_paths) != len(assignments):
        raise ExtractorPlanError("Extractor allocation is incomplete or reuses a Booster identity.")
    if Counter(item.article_path.casefold() for item in (*[a.assigned_reward for a in assignments], *remaining)) != Counter(path_keys):
        raise ExtractorPlanError("Allocated and unallocated Booster pools do not partition the canonical inventory.")
    return ExtractorPlan(normalized, catalog.revision, mix_postgame_dlc_with_base,
                         len(available), tuple(assignments), remaining, tuple(stats))


# Create a seed-based random stream
def _rng(seed: NormalizedSeed, pool: str) -> StableRandom:
    material = f"cv2-extractor-booster-recipes-v1\0{seed.internal_value}\0{pool}".encode("utf-8")
    return StableRandom.from_material(material)
