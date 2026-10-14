"""Deterministic allocation for the three researched starting Article slots."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable

from cv2randomizer.core.determinism import StableRandom, canonical_sort_key
from cv2randomizer.core.seed import NormalizedSeed, normalize_seed
from cv2randomizer.game.defensive_gadget_catalog import DefensiveGadgetCatalog
from cv2randomizer.game.initial_inventory_catalog import WeaponRewardCandidate
from cv2randomizer.game.jail_catalog import JailCatalog
from cv2randomizer.randomizer.reward_reservations import RewardReservationSet
from cv2randomizer.randomizer.source_occurrences import RewardSourceOccurrence

INITIAL_INVENTORY_PACKAGE = "/Game/Blueprint/Article/DA_InitialInventory_Player"
INITIAL_INVENTORY_RNG_DOMAINS = (
    "initial_inventory.weapon",
    "initial_inventory.defensive_gadget",
    "initial_inventory.jail",
)


@dataclass(frozen=True, slots=True)
class InitialInventoryAssignment:
    slot_id: str
    display_name: str
    category: str
    vanilla_article_path: str
    assigned_article_path: str
    source_occurrence_id: str = ""


@dataclass(frozen=True, slots=True)
class InitialInventoryPlan:
    seed: NormalizedSeed
    catalog_revision: str
    assignments: tuple[InitialInventoryAssignment, ...]
    candidate_counts: tuple[tuple[str, int], ...]
    rng_domains: tuple[str, ...] = INITIAL_INVENTORY_RNG_DOMAINS

    # Find changed slots
    @property
    def changed_slots(self) -> int:
        return sum(
            item.vanilla_article_path.casefold() != item.assigned_article_path.casefold()
            for item in self.assignments
        )

    # Read assignment
    def assignment(self, slot_id: str) -> InitialInventoryAssignment:
        matches = [item for item in self.assignments if item.slot_id == slot_id]
        if len(matches) != 1:
            raise KeyError(slot_id)
        return matches[0]


_SLOTS = (
    ("starting_weapon", "Starting Weapon", "weapon", "/Game/Blueprint/Article/Weapon/SingleSword/DA_SingleSword_Future_Lesser_1"),
    ("starting_defensive_gadget", "Starting Defensive Gadget", "defensive_gadget", "/Game/Blueprint/Article/Gadget/Defence/DA_DefGadget_LesserShield_1"),
    ("starting_jail", "Starting Jail", "jail", "/Game/Blueprint/Article/Jail/DA_Jail_Ogre"),
)


# Build initial inventory plan
def build_initial_inventory_plan(
    seed: str | int | NormalizedSeed | None,
    catalog_revision: str,
    *,
    weapons: Iterable[WeaponRewardCandidate],
    defensive_gadgets: DefensiveGadgetCatalog,
    jails: JailCatalog,
    reservations: RewardReservationSet | None = None,
    source_occurrences: Iterable[RewardSourceOccurrence] | None = None,
) -> InitialInventoryPlan:
    """Choose one available identity per category with isolated deterministic streams."""
    if not isinstance(catalog_revision, str) or not catalog_revision.strip():
        raise ValueError("Initial Inventory needs a non-empty catalog revision.")
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    reserved = reservations if reservations is not None else RewardReservationSet()

    pools: dict[str, tuple[str, ...]] = {
        "weapon": _unique_paths((candidate.asset_path for candidate in weapons), "Weapon"),
        "defensive_gadget": _unique_paths(
            (item.asset_path for item in defensive_gadgets.items if item.world_reward_eligible),
            "Defensive Gadget",
        ),
        "jail": _unique_paths(
            (item.asset_path for item in jails.items if item.world_reward_eligible),
            "Jail",
        ),
    }
    occurrence_pools: dict[str, tuple[RewardSourceOccurrence, ...]] | None = None
    if source_occurrences is not None:
        by_category: dict[str, list[RewardSourceOccurrence]] = {key: [] for key in pools}
        weapon_paths = {path.casefold() for path in pools["weapon"]}
        prefixes = {
            "weapon": "/game/blueprint/article/weapon/",
            "defensive_gadget": "/game/blueprint/article/gadget/defence/",
            "jail": "/game/blueprint/article/jail/",
        }
        seen_occurrences: set[str] = set()
        for occurrence in source_occurrences:
            occurrence_key = occurrence.occurrence_id.casefold()
            if occurrence_key in seen_occurrences:
                raise ValueError("Initial Inventory source inventory contains a duplicate occurrence ID.")
            seen_occurrences.add(occurrence_key)
            for category, prefix in prefixes.items():
                if not occurrence.article_path.casefold().startswith(prefix):
                    continue
                if category == "weapon":
                    eligible = occurrence.article_path.casefold() in weapon_paths
                elif category == "defensive_gadget":
                    resolved = defensive_gadgets.resolve(occurrence.article_path)
                    eligible = resolved is not None and resolved.world_reward_eligible
                else:
                    resolved = jails.resolve(occurrence.article_path)
                    eligible = resolved is not None and resolved.world_reward_eligible
                if eligible:
                    by_category[category].append(occurrence)
                break
        occurrence_pools = {
            category: tuple(sorted(values, key=lambda item: (item.occurrence_id.casefold(), item.occurrence_id)))
            for category, values in by_category.items()
        }
    assignments: list[InitialInventoryAssignment] = []
    counts: list[tuple[str, int]] = []
    for domain, (_slot_id, display_name, category, vanilla_path) in zip(
        INITIAL_INVENTORY_RNG_DOMAINS, _SLOTS, strict=True,
    ):
        eligible_occurrences = None if occurrence_pools is None else tuple(
            item for item in occurrence_pools[category]
            if not reserved.contains(category, item.article_path)
        )
        eligible = (
            tuple(path for path in pools[category] if not reserved.contains(category, path))
            if eligible_occurrences is None else tuple(item.article_path for item in eligible_occurrences)
        )
        if not eligible:
            raise ValueError(f"Initial Inventory has no available {display_name} candidate.")
        if eligible_occurrences is None:
            chosen = _rng(normalized, domain, catalog_revision).choice(eligible)
            source_occurrence_id = ""
        else:
            chosen_occurrence = _rng(normalized, domain, catalog_revision).sample(eligible_occurrences, 1)[0]
            chosen = chosen_occurrence.article_path
            source_occurrence_id = chosen_occurrence.occurrence_id
        counts.append((category, len(eligible)))
        assignments.append(InitialInventoryAssignment(
            _slot_id, display_name, category, vanilla_path, chosen, source_occurrence_id,
        ))

    plan = InitialInventoryPlan(
        normalized, catalog_revision, tuple(assignments), tuple(counts),
    )
    return plan


# Resolve unique paths
def _unique_paths(paths: Iterable[str], label: str) -> tuple[str, ...]:
    result: dict[str, str] = {}
    for path in paths:
        key = path.casefold()
        if not path.startswith("/Game/") or "\\" in path:
            raise ValueError(f"{label} pool contains an invalid canonical Article path.")
        if key in result:
            raise ValueError(f"{label} pool contains duplicate canonical Article identities.")
        result[key] = path
    return tuple(sorted(result.values(), key=canonical_sort_key))


# Create a seed-based random stream
def _rng(seed: NormalizedSeed, domain: str, revision: str) -> StableRandom:
    material = f"cv2-initial-inventory-v1\0{domain}\0{revision}\0{seed.internal_value}".encode("utf-8")
    return StableRandom.from_material(material)


__all__ = [
    "INITIAL_INVENTORY_PACKAGE",
    "INITIAL_INVENTORY_RNG_DOMAINS",
    "InitialInventoryAssignment",
    "InitialInventoryPlan",
    "build_initial_inventory_plan",
]
