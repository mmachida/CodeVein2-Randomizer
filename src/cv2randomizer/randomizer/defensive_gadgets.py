"""Deterministic planners for Defensive Gadget World Loot and NPC rewards."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cv2randomizer.core.determinism import StableRandom, canonical_sort_key
from cv2randomizer.core.seed import NormalizedSeed, normalize_seed
from cv2randomizer.game.catalogs import RewardPayload
from cv2randomizer.game.defensive_gadget_catalog import (
    DefensiveGadget,
    DefensiveGadgetCatalog,
    DefensiveGadgetGrantSlot,
)
from cv2randomizer.game.fname_identity import SoftObjectPathIdentity
from cv2randomizer.randomizer.world_loot import WorldLootRewardCandidate
from cv2randomizer.randomizer.source_occurrences import RewardSourceOccurrence


class DefensiveGadgetPlanError(ValueError):
    """Raised when a Defensive Gadget pool cannot be planned safely."""


@dataclass(frozen=True, slots=True)
class DefensiveGadgetNpcAssignment:
    destination: DefensiveGadgetGrantSlot
    original: DefensiveGadget
    assigned: DefensiveGadget
    source_occurrence_id: str = ""


@dataclass(frozen=True, slots=True)
class DefensiveGadgetNpcPlan:
    seed: NormalizedSeed
    catalog_revision: str
    mix_postgame_dlc_with_base: bool
    candidate_count: int
    assignments: tuple[DefensiveGadgetNpcAssignment, ...]
    pending_grants: tuple[str, ...]

    # Find changed slots
    @property
    def changed_slots(self) -> int:
        return sum(item.original.object != item.assigned.object for item in self.assignments)

    # Find unchanged assignments
    @property
    def self_maps(self) -> int:
        return len(self.assignments) - self.changed_slots


# List defensive gadget world candidates
def defensive_gadget_world_candidates(
    catalog: DefensiveGadgetCatalog,
) -> tuple[WorldLootRewardCandidate, ...]:
    """Expose canonical rewards as payload candidates, without fabricating locations."""
    return tuple(
        WorldLootRewardCandidate(
            candidate_id=f"defensive-gadget:{item.object.casefold()}",
            rewards=(RewardPayload(
                item.asset_path,
                1,
                True,
                item.content_class,
                SoftObjectPathIdentity.from_literal_path(item.asset_path),
            ),),
            source_location=None,
            candidate_category="defensive_gadget",
            canonical_identity=item.object,
            identity_aliases=(
                item.object,
                item.asset_path,
                item.asset_path.rsplit("/", 1)[-1],
                *item.aliases,
            ),
        )
        for item in sorted(catalog.items, key=lambda item: canonical_sort_key(item.object))
        if item.world_reward_eligible
    )


# Build defensive gadget NPC plan
def build_defensive_gadget_npc_plan(
    catalog: DefensiveGadgetCatalog,
    seed: str | int | NormalizedSeed | None,
    *,
    mix_postgame_dlc_with_base: bool = False,
    enabled: bool = True,
    reserved_reward_identities: Iterable[str] = (),
    source_occurrences: Iterable[RewardSourceOccurrence] | None = None,
) -> DefensiveGadgetNpcPlan:
    if type(mix_postgame_dlc_with_base) is not bool or type(enabled) is not bool:
        raise DefensiveGadgetPlanError("Defensive Gadget NPC mix/enabled options must be booleans.")
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    if not enabled:
        return DefensiveGadgetNpcPlan(
            normalized, catalog.revision, mix_postgame_dlc_with_base, 0, (),
            tuple(slot.slot_id for slot in catalog.pending_grants),
        )
    reserved = {value.casefold() for value in reserved_reward_identities}
    candidates = tuple(sorted(
        catalog.candidates(mix_postgame_dlc_with_base=mix_postgame_dlc_with_base),
        key=lambda item: item.object.casefold(),
    ))
    candidates = tuple(item for item in candidates if item.asset_path.casefold() not in reserved)
    all_slots = tuple(sorted(catalog.grants, key=lambda slot: slot.slot_id.casefold()))
    slots = all_slots
    if not slots:
        raise DefensiveGadgetPlanError("No structurally confirmed Defensive Gadget NPC grants are available.")
    occurrence_candidates: tuple[tuple[DefensiveGadget, str], ...] | None = None
    if source_occurrences is not None:
        resolved: list[tuple[DefensiveGadget, str]] = []
        for occurrence in source_occurrences:
            item = catalog.resolve(occurrence.article_path)
            if (
                item is not None
                and item.npc_reward_eligible
                and (mix_postgame_dlc_with_base or item.content_class == "BASE")
                and item.asset_path.casefold() not in reserved
            ):
                resolved.append((item, occurrence.occurrence_id))
        occurrence_ids = [identity.casefold() for _item, identity in resolved]
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise DefensiveGadgetPlanError("Defensive Gadget source occurrence pool contains duplicate IDs.")
        occurrence_candidates = tuple(sorted(
            resolved, key=lambda pair: (pair[1].casefold(), pair[1]),
        ))
        if len(occurrence_candidates) < len(slots):
            raise DefensiveGadgetPlanError("Fewer movable Defensive Gadget source occurrences than confirmed NPC grant slots.")
    else:
        if len(candidates) < len(slots):
            raise DefensiveGadgetPlanError("Fewer eligible Defensive Gadgets than confirmed NPC grant slots.")
        if len({item.object.casefold() for item in candidates}) != len(candidates):
            raise DefensiveGadgetPlanError("Defensive Gadget candidate pool contains duplicate identities.")
    rng = _npc_rng(normalized, "defensive_gadget_npc_grants")
    assigned = (
        rng.sample(list(candidates), len(slots))
        if occurrence_candidates is None
        else rng.sample(list(occurrence_candidates), len(slots))
    )
    assignments: list[DefensiveGadgetNpcAssignment] = []
    for slot, selected in zip(slots, assigned, strict=True):
        item, occurrence_id = selected if occurrence_candidates is not None else (selected, "")
        original = catalog.resolve(slot.original_asset_path)
        if original is None:
            raise DefensiveGadgetPlanError(f"Grant slot {slot.slot_id} has no canonical vanilla item.")
        assignments.append(DefensiveGadgetNpcAssignment(slot, original, item, occurrence_id))
    assignments.sort(key=lambda item: item.destination.slot_id.casefold())
    return DefensiveGadgetNpcPlan(
        normalized,
        catalog.revision,
        mix_postgame_dlc_with_base,
        len(occurrence_candidates) if occurrence_candidates is not None else len(candidates),
        tuple(assignments),
        tuple(slot.slot_id for slot in catalog.pending_grants),
    )


# Create NPC RNG
def _npc_rng(seed: NormalizedSeed, domain: str) -> StableRandom:
    key = f"cv2-defensive-gadget-v1\0{domain}\0{seed.internal_value}".encode("utf-8")
    return StableRandom.from_material(key)
