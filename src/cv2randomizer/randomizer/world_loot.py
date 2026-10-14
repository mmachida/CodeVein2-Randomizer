"""Deterministic whole-location assignments for the combined World Loot pool."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from collections.abc import Iterable, Sequence

from ..core.determinism import StableRandom, canonical_package_key, canonical_sort_key
from ..core.seed import NormalizedSeed, normalize_seed
from ..game.catalogs import (
    SUPPORTED_QUEST_ARTICLE_GRANT_DESTINATIONS,
    RewardPayload,
    WorldLootLocation,
    is_valid_scripted_reward_only_location,
)
from ..game.content_availability import package_path_from_object_path

WorldLootSlot = tuple[str, int]
_SUPPORTED_CATEGORIES = frozenset({"GROUND_PICKUP", "TREASURE_BOX"})
_ENEMY_GATED_WORLD_DESTINATION_PREFIXES = (
    "one_time_enemy_",
    "harmony_moon_",
    "qst_fieldenemy_",
)
_UNIQUE_RECIPE_ARTICLE_PREFIX = (
    "/game/blueprint/article/item/important/da_item_important_recipe_"
)
_HOT_SPRING_REPEAT_RECIPE_FILLER_PREFIX = "hot-spring-repeat-recipe-filler:"


# Filter enemy reward world locations
def filter_enemy_reward_world_locations(
    locations: Iterable[WorldLootLocation],
    *,
    enabled: bool,
) -> tuple[WorldLootLocation, ...]:
    """Keep confirmed one-time enemy rewards in World Loot only when enabled.

    These curated quest-grant destinations are part of the Enemy Drops option,
    while ordinary map pickups remain in the always-on World Loot pool.
    """
    values = tuple(locations)
    if enabled:
        return values
    return tuple(
        location for location in values
        if not (
            location.destination_kind == "QUEST_ARTICLE_GRANT"
            and location.destination_id is not None
            and location.destination_id.casefold().startswith(_ENEMY_GATED_WORLD_DESTINATION_PREFIXES)
        )
    )


@dataclass(frozen=True, slots=True)
class WorldLootAssignment:
    """Move reward Article identities while retaining destination tuple metadata."""

    destination: WorldLootLocation
    source: WorldLootLocation | None
    source_candidate: "WorldLootRewardCandidate | None" = None

    # List assigned rewards
    @property
    def assigned_rewards(self):
        if self.source_candidate is not None:
            source_rewards = self.source_candidate.rewards
        elif self.source is None:
            raise ValueError("World Loot assignment has no source reward candidate.")
        else:
            source_rewards = self.source.rewards
        if len(source_rewards) != len(self.destination.rewards):
            raise ValueError("World Loot assignment changes a location's reward-list arity.")
        return tuple(
            replace(
                source_reward,
                count=destination_reward.count,
                notification=destination_reward.notification,
            )
            for source_reward, destination_reward in zip(
                source_rewards, self.destination.rewards, strict=True,
            )
        )


@dataclass(frozen=True, slots=True)
class WorldLootRewardCandidate:
    """A reward payload candidate, which may come from a catalog rather than a fake location."""

    candidate_id: str
    rewards: tuple[RewardPayload, ...]
    source_location: WorldLootLocation | None = None
    candidate_category: str | None = None
    canonical_identity: str | None = None
    identity_aliases: tuple[str, ...] = ()
    source_occurrence_ids: tuple[str, ...] = ()
    source_locations: tuple[WorldLootLocation, ...] = ()
    reward_categories: tuple[str | None, ...] = ()

    # Validate initialized settings
    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.rewards:
            raise ValueError("World Loot reward candidates need an identity and a non-empty payload.")
        if (self.candidate_category is None) != (self.canonical_identity is None):
            raise ValueError("Catalog World Loot candidates need both a category and canonical identity.")
        if self.source_occurrence_ids and len(self.source_occurrence_ids) != len(self.rewards):
            raise ValueError("World Loot candidate occurrence IDs must identify every reward in its payload.")
        if len({value.casefold() for value in self.source_occurrence_ids}) != len(self.source_occurrence_ids):
            raise ValueError("World Loot candidate occurrence IDs must be unique within a payload.")
        if self.reward_categories and len(self.reward_categories) != len(self.rewards):
            raise ValueError("Reward categories must describe every Article in a World Loot payload.")


@dataclass(frozen=True, slots=True)
class WorldLootCandidatePoolStats:
    """Audit counts for one canonical reward category over the movable pool."""

    candidate_category: str
    canonical_candidates: int
    already_present_count: int
    injected_count: int
    duplicate_sources_removed: int
    already_present_identities: tuple[str, ...] = ()
    injected_identities: tuple[str, ...] = ()
    reserved_sources_removed: int = 0
    unselected_for_capacity: int = 0
    unowned_candidates_skipped: int = 0


@dataclass(frozen=True, slots=True)
class LockedWorldLootLocation:
    """A location excluded from the V1 pool with its safety reason."""

    location: WorldLootLocation
    reason: str


@dataclass(frozen=True, slots=True)
class WorldLootRandomizationPlan:
    """Safe permutation and non-participating locations."""

    assignments: tuple[WorldLootAssignment, ...]
    locked_locations: tuple[LockedWorldLootLocation, ...]
    excluded_locations: tuple[WorldLootLocation, ...]
    disabled_locations: tuple[WorldLootLocation, ...]
    mirrored_locations: tuple[WorldLootLocation, ...] = ()
    candidate_pool_stats: tuple[WorldLootCandidatePoolStats, ...] = ()
    overlay_displaced_payloads: int = 0
    unmatched_locations: tuple[WorldLootLocation, ...] = ()
    unavailable_what_locations: tuple[WorldLootLocation, ...] = ()
    eligible_reward_source_count: int = 0
    source_occurrence_ids: tuple[str, ...] = ()
    source_article_paths: tuple[str, ...] = ()

    # Build candidate stats
    def candidate_stats(self, category: str) -> WorldLootCandidatePoolStats:
        return next(
            (stats for stats in self.candidate_pool_stats if stats.candidate_category == category),
            WorldLootCandidatePoolStats(category, 0, 0, 0, 0),
        )

    # Count gadget candidate count
    @property
    def gadget_candidate_count(self) -> int:
        return self.candidate_stats("defensive_gadget").canonical_candidates

    # Count gadget injected count
    @property
    def gadget_injected_count(self) -> int:
        return self.candidate_stats("defensive_gadget").injected_count

    # List gadget duplicate sources
    @property
    def gadget_duplicate_sources(self) -> int:
        return self.candidate_stats("defensive_gadget").duplicate_sources_removed

    # Read gadget displaced payloads
    @property
    def gadget_displaced_payloads(self) -> int | None:
        categories = {stats.candidate_category for stats in self.candidate_pool_stats}
        if "defensive_gadget" not in categories:
            return 0
        return self.overlay_displaced_payloads if categories == {"defensive_gadget"} else None

    # Count jail candidate count
    @property
    def jail_candidate_count(self) -> int:
        return self.candidate_stats("jail").canonical_candidates

    # Count jail injected count
    @property
    def jail_injected_count(self) -> int:
        return self.candidate_stats("jail").injected_count

    # Count booster candidate count
    @property
    def booster_candidate_count(self) -> int:
        return self.candidate_stats("booster").canonical_candidates

    # Count booster injected count
    @property
    def booster_injected_count(self) -> int:
        return self.candidate_stats("booster").injected_count

    # List booster duplicate sources
    @property
    def booster_duplicate_sources(self) -> int:
        return self.candidate_stats("booster").duplicate_sources_removed


# List world loot slot
def world_loot_slot(location: WorldLootLocation) -> WorldLootSlot:
    """Stable package/export identity for a location."""
    return (canonical_package_key(location.package_path)[0], location.export_index)


# Find world loot occurrence ID
def world_loot_occurrence_id(location: WorldLootLocation, reward_index: int) -> str:
    """Stable identity for one reward occurrence in one physical source slot."""
    if type(reward_index) is not int or reward_index < 0 or reward_index >= len(location.rewards):
        raise ValueError("World Loot reward index is outside its source payload.")
    package, _ = canonical_package_key(location.package_path)
    return f"world-loot:{package.casefold()}#{location.export_index}#reward-{reward_index}"


# Group RNG
def _group_rng(seed_value: int, reward_arity: int) -> StableRandom:
    key = f"cv2-world-loot-whole-payload-v1\0{seed_value}\0arity={reward_arity}".encode("utf-8")
    return StableRandom.from_material(key)


# Check hot spring
def _is_hot_spring(location: WorldLootLocation) -> bool:
    return bool(location.destination_id and location.destination_id.casefold().startswith("hot_spring_"))


# Build candidate occurrence ids
def _candidate_occurrence_ids(candidate: WorldLootRewardCandidate) -> tuple[str, ...]:
    if candidate.source_occurrence_ids:
        return candidate.source_occurrence_ids
    if candidate.source_location is not None:
        return tuple(
            world_loot_occurrence_id(candidate.source_location, index)
            for index in range(len(candidate.rewards))
        )
    return ()


# Build candidate source locations
def _candidate_source_locations(candidate: WorldLootRewardCandidate) -> tuple[WorldLootLocation, ...]:
    if candidate.source_locations:
        return candidate.source_locations
    return (candidate.source_location,) if candidate.source_location is not None else ()


# Check unique recipe reward
def _is_unique_recipe_reward(reward: RewardPayload) -> bool:
    """Return whether the Article is a one-time recipe unlock rather than repeatable loot."""
    return reward.article_path.casefold().startswith(_UNIQUE_RECIPE_ARTICLE_PREFIX)


# List intermix hot spring reward slots
def _intermix_hot_spring_reward_slots(
    assignments: list[WorldLootAssignment], seed_value: int,
) -> list[WorldLootAssignment]:
    """Let paired Hot Spring rewards exchange with individual World Loot sources.

    Hot Spring tasks keep their serialized reward slots. Their unique recipe
    unlocks move to individual singleton destinations, while repeated copies of
    a recipe unlock are replaced with ordinary singleton World Loot rewards.
    Two singleton source occurrences are paired into each base-game Hot Spring
    task. Other multi-reward destinations remain whole bundles.
    """
    hot_spring_destinations = sorted(
        (
            assignment.destination for assignment in assignments
            if _is_hot_spring(assignment.destination) and len(assignment.destination.rewards) > 1
        ),
        key=world_loot_slot,
    )
    if not hot_spring_destinations:
        return assignments

    pair_assignments = [
        assignment for assignment in assignments if len(assignment.destination.rewards) > 1
    ]
    hot_spring_source_assignments = [
        assignment for assignment in pair_assignments
        if assignment.source_candidate is not None
        and assignment.source_candidate.source_location is not None
        and _is_hot_spring(assignment.source_candidate.source_location)
    ]
    if len(hot_spring_source_assignments) != len(hot_spring_destinations):
        # For example, an old game build may expose the Hot Spring task but not
        # every original reward Article. Keep the existing arity-safe plan then.
        return assignments

    hot_spring_source_slots = {
        world_loot_slot(assignment.source_candidate.source_location)
        for assignment in hot_spring_source_assignments
        if assignment.source_candidate is not None and assignment.source_candidate.source_location is not None
    }
    non_hot_spring_destinations = [
        assignment.destination for assignment in pair_assignments
        if not _is_hot_spring(assignment.destination)
    ]
    non_hot_spring_sources = [
        assignment.source_candidate for assignment in pair_assignments
        if assignment.source_candidate is not None
        and assignment.source_candidate.source_location is not None
        and world_loot_slot(assignment.source_candidate.source_location) not in hot_spring_source_slots
    ]
    if len(non_hot_spring_destinations) != len(non_hot_spring_sources):
        return assignments

    singleton_assignments = [
        assignment for assignment in assignments
        if len(assignment.destination.rewards) == 1
        and assignment.source_candidate is not None
        and len(_candidate_occurrence_ids(assignment.source_candidate)) == 1
        and _candidate_source_locations(assignment.source_candidate)
    ]
    required_singletons = len(hot_spring_destinations) * 2
    if len(singleton_assignments) < required_singletons:
        return assignments

    spring_rng = StableRandom.from_material(
        f"cv2-world-loot-hot-spring-slot-intermix-v1\0{seed_value}"
    )
    selectable_singletons = sorted(singleton_assignments, key=lambda item: world_loot_slot(item.destination))
    spring_rng.shuffle(selectable_singletons)
    selected_singletons = selectable_singletons[:required_singletons]

    singleton_sources = [assignment.source_candidate for assignment in selected_singletons]
    if any(source is None or len(source.rewards) != 1 for source in singleton_sources):
        return assignments
    singleton_sources = [source for source in singleton_sources if source is not None]
    spring_rng.shuffle(singleton_sources)

    replacement_by_slot: dict[WorldLootSlot, WorldLootAssignment] = {}
    for spring_index, destination in enumerate(hot_spring_destinations):
        first, second = singleton_sources[spring_index * 2:spring_index * 2 + 2]
        first_ids = _candidate_occurrence_ids(first)
        second_ids = _candidate_occurrence_ids(second)
        if len(first_ids) != 1 or len(second_ids) != 1:
            return assignments
        source_locations = tuple(dict.fromkeys((
            *_candidate_source_locations(first), *_candidate_source_locations(second),
        )))
        paired_source = WorldLootRewardCandidate(
            candidate_id=f"hot-spring-singleton-pair:{destination.destination_id}",
            rewards=(first.rewards[0], second.rewards[0]),
            source_location=source_locations[0] if source_locations else None,
            source_occurrence_ids=(first_ids[0], second_ids[0]),
            source_locations=source_locations,
            reward_categories=(
                first.reward_categories[0] if first.reward_categories else first.candidate_category,
                second.reward_categories[0] if second.reward_categories else second.candidate_category,
            ),
        )
        replacement_by_slot[world_loot_slot(destination)] = WorldLootAssignment(
            destination, paired_source.source_location, paired_source,
        )

    hot_spring_rewards: list[WorldLootRewardCandidate] = []
    repeated_recipe_occurrences: list[tuple[WorldLootRewardCandidate, int, str]] = []
    seen_recipe_articles: set[str] = set()
    for assignment in sorted(
        hot_spring_source_assignments,
        key=lambda item: world_loot_slot(item.source_candidate.source_location),
    ):
        source = assignment.source_candidate
        if source is None or source.source_location is None:
            return assignments
        occurrence_ids = _candidate_occurrence_ids(source)
        if len(occurrence_ids) != len(source.rewards):
            return assignments
        for reward_index, (reward, occurrence_id) in enumerate(zip(source.rewards, occurrence_ids, strict=True)):
            if _is_unique_recipe_reward(reward):
                recipe_identity = reward.article_path.casefold()
                if recipe_identity in seen_recipe_articles:
                    repeated_recipe_occurrences.append((source, reward_index, occurrence_id))
                    continue
                seen_recipe_articles.add(recipe_identity)
            hot_spring_rewards.append(WorldLootRewardCandidate(
                candidate_id=f"hot-spring-recipe-slot:{source.candidate_id}:reward-{reward_index}",
                rewards=(reward,),
                source_location=source.source_location,
                source_occurrence_ids=(occurrence_id,),
            ))
    if len(hot_spring_rewards) + len(repeated_recipe_occurrences) != required_singletons:
        return assignments

    if repeated_recipe_occurrences:
        # The game catalog repeats some recipe Articles across several Forest
        # springs, although each Article unlocks only once. Keep every physical
        # grant slot, but fill redundant recipe slots with distinct ordinary
        # World Loot Articles so the unlock itself enters the pool once.
        fillers_by_article: dict[str, list[WorldLootRewardCandidate]] = defaultdict(list)
        for assignment in singleton_assignments:
            candidate = assignment.source_candidate
            if candidate is None or len(candidate.rewards) != 1:
                continue
            reward = candidate.rewards[0]
            if _is_unique_recipe_reward(reward):
                continue
            if candidate.candidate_category is not None or candidate.canonical_identity is not None:
                continue
            source_location = candidate.source_location
            if (
                source_location is None
                or source_location.destination_id is not None
                or source_location.destination_kind != "ARTICLE_BULK"
                or source_location.actor_category not in _SUPPORTED_CATEGORIES
            ):
                continue
            source_locations = _candidate_source_locations(candidate)
            if not source_locations:
                continue
            fillers_by_article[reward.article_path.casefold()].append(candidate)
        # Only duplicate ordinary pickups are safe fillers. Cloning an Article
        # that is unique in the World pool could duplicate a Booster, Jail, or
        # other unique identity owned by a separate randomizer subsystem.
        fillers = [
            sorted(candidates, key=lambda item: canonical_sort_key(item.candidate_id))[0]
            for candidates in fillers_by_article.values()
            if len(candidates) > 1
        ]
        filler_rng = StableRandom.from_material(
            f"cv2-world-loot-hot-spring-repeat-recipe-filler-v1\0{seed_value}"
        )
        filler_rng.shuffle(fillers)
        if len(fillers) < len(repeated_recipe_occurrences):
            # Without enough distinct safe filler Articles, keep the original
            # arity-safe plan instead of producing a partial rewrite.
            return assignments
        for (source, reward_index, occurrence_id), filler in zip(
            repeated_recipe_occurrences, fillers, strict=False,
        ):
            filler_locations = _candidate_source_locations(filler)
            if not filler_locations:
                return assignments
            filler_category = (
                filler.reward_categories[0]
                if filler.reward_categories else filler.candidate_category
            )
            hot_spring_rewards.append(WorldLootRewardCandidate(
                candidate_id=(
                    f"{_HOT_SPRING_REPEAT_RECIPE_FILLER_PREFIX}"
                    f"{source.candidate_id}:reward-{reward_index}"
                ),
                rewards=(filler.rewards[0],),
                source_location=filler_locations[0],
                source_occurrence_ids=(occurrence_id,),
                source_locations=filler_locations,
                reward_categories=(filler_category,),
            ))
    if len(hot_spring_rewards) != required_singletons:
        return assignments
    spring_rng.shuffle(hot_spring_rewards)
    for singleton_assignment, recipe_source in zip(selected_singletons, hot_spring_rewards, strict=True):
        replacement_by_slot[world_loot_slot(singleton_assignment.destination)] = WorldLootAssignment(
            singleton_assignment.destination,
            recipe_source.source_location,
            recipe_source,
        )

    reshuffled_pair_sources = _derangement(
        non_hot_spring_destinations,
        non_hot_spring_sources,
        StableRandom.from_material(
            f"cv2-world-loot-hot-spring-pair-rebalance-v1\0{seed_value}"
        ),
    )
    for destination, source in zip(non_hot_spring_destinations, reshuffled_pair_sources, strict=True):
        replacement_by_slot[world_loot_slot(destination)] = WorldLootAssignment(
            destination, source.source_location, source,
        )

    return [
        replacement_by_slot.get(world_loot_slot(assignment.destination), assignment)
        for assignment in assignments
    ]


# Build candidate slot
def _candidate_slot(candidate: WorldLootRewardCandidate) -> WorldLootSlot | None:
    if candidate.source_location is None:
        return None
    return world_loot_slot(candidate.source_location)


# Read derangement
def _derangement(
    locations: list[WorldLootLocation],
    candidates: list[WorldLootRewardCandidate],
    rng: StableRandom,
) -> list[WorldLootRewardCandidate]:
    """Produce a random permutation without fixed locations when possible."""
    if len(locations) != len(candidates):
        raise ValueError("World Loot reward candidates must match the destination slot count.")
    if len(locations) < 2:
        return candidates.copy()
    for _ in range(1000):
        shuffled = candidates.copy()
        rng.shuffle(shuffled)
        if all(
            world_loot_slot(destination) != _candidate_slot(source)
            for destination, source in zip(locations, shuffled, strict=True)
        ):
            return shuffled
    for shift in range(1, len(candidates) + 1):
        rotated = candidates[shift:] + candidates[:shift]
        if all(world_loot_slot(destination) != _candidate_slot(source)
               for destination, source in zip(locations, rotated, strict=True)):
            return rotated
    return candidates.copy()


# Return lock reason
def _lock_reason(
    location: WorldLootLocation,
    unsafe_packages: frozenset[str],
    supported_quest_grant_destinations: frozenset[str],
) -> str | None:
    if location.location_type == "RENEWABLE_INGREDIENT" or location.destination_kind == "RENEWABLE_INGREDIENT":
        return "RENEWABLE_INGREDIENT_DISABLED"
    if location.protected:
        return "; ".join(location.protection_reasons) or "catalog protection"
    if location.source_file.casefold() in unsafe_packages:
        return "unsafe package"
    if location.destination_kind == "RAW_ARTICLE_GRANT":
        if (
            location.destination_id not in {
                "jadwiga_servant_lightning_barrage_reward",
                "jadwiga_servant_booster_dex_reward",
                "jadwiga_servant_initial_parry_reward",
            }
            or location.actor_category != "DIRECT_ARTICLE_GRANT"
            or location.raw_article_layout is None
            or len(location.rewards) != 1
        ):
            return "unvalidated direct Article grant destination"
        return None
    if location.destination_kind == "QUEST_ARTICLE_GRANT":
        layouts = location.quest_grant_layouts
        if (
            location.destination_id not in supported_quest_grant_destinations
            or location.actor_category != "DIRECT_ARTICLE_GRANT"
            or len(layouts) != len(location.rewards)
            or (bool(location.quest_article_layouts)
                and tuple(layout.reward_index for layout in layouts) != tuple(range(len(location.rewards))))
            or location.raw_article_layout is not None
        ):
            return "unvalidated structured quest Article grant destination"
        return None
    if location.destination_kind == "BLUEPRINT_CDO_ARTICLE_BULK":
        if (
            location.destination_id != "world_loot_cdo:" + location.package_path.casefold()
            or location.actor_category != "GROUND_PICKUP"
            or location.save_id is not None
            or location.raw_article_layout is not None
            or location.quest_article_layout is not None
            or not location.parent_blueprint_package
            or not location.parent_blueprint_packages
            or location.parent_blueprint_packages[0].casefold() != location.parent_blueprint_package.casefold()
            or location.physical_instance_count < 1
        ):
            return "unvalidated Blueprint CDO reward destination"
        if len(location.rewards) != 1:
            return "unsupported Blueprint CDO reward arity"
        return None
    if location.destination_kind == "INSTANCE_ARTICLE_BULK_OVERRIDE":
        source = location.instance_override_source
        if (
            location.location_type not in {
                "NORMAL_WORLD_LOOT", "GROUND_PICKUP", "TREASURE_BOX",
                "HEALING_UPGRADE_GOLDEN_SEED", "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
            }
            or source is None
            or location.destination_id is None
            or location.physical_instance_count != 1
            or location.structural_class_package != source.class_package
            or location.actor_category not in {"GROUND_PICKUP", "TREASURE_BOX"}
            or location.protected or location.protection_reasons
            or len(location.rewards) != 1
        ):
            return "unvalidated per-instance ArticleBulk override destination"
        if location.actor_class != source.class_package.rsplit("/", 1)[-1] + "_C":
            return "per-instance ArticleBulk override actor class differs from its Blueprint source"
        return None
    if location.destination_kind != "ARTICLE_BULK":
        return "unsupported destination serializer"
    if location.raw_article_layout is not None or location.destination_id is not None:
        return "unexpected direct Article grant metadata"
    if location.actor_category not in _SUPPORTED_CATEGORIES:
        return "unsupported location category"
    actor_class = location.actor_class.casefold()
    if "withflagchange" in actor_class and not is_valid_scripted_reward_only_location(location):
        return "flag-changing pickup"
    if "withsmallscenario" in actor_class and not is_valid_scripted_reward_only_location(location):
        return "scenario-associated pickup"
    if location.protection_reasons:
        return "; ".join(location.protection_reasons)
    return None


# Build world loot randomization plan
def build_world_loot_randomization_plan(
    locations: Iterable[WorldLootLocation],
    seed: str | int | NormalizedSeed | None,
    *,
    enabled: bool = True,
    include_healing: bool = True,
    excluded_slots: Iterable[WorldLootSlot] = (),
    unsafe_packages: Iterable[str] = (),
    catalog_candidates: Sequence[WorldLootRewardCandidate] = (),
    available_reward_packages: Iterable[str] | None = None,
    reserved_reward_identities: Iterable[str] = (),
    supported_quest_grant_destinations: Iterable[str] = (),
    # Compatibility input for callers that have not yet moved to the shared catalog layer.
    gadget_candidates: Sequence[WorldLootRewardCandidate] = (),
) -> WorldLootRandomizationPlan:
    """Build a deterministic Article-identity permutation across pickup and chest locations.

    Destinations keep their original Count and bNotification fields. Different
    arities normally stay in separate compatibility groups. The audited paired
    Hot Springs are rebalanced afterward with two singleton sources apiece, so
    their serialized reward count stays unchanged while they can receive any
    individual World Loot Article. Category types are not separated within a group.
    """
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    excluded = set(excluded_slots)
    reserved = {value.casefold() for value in reserved_reward_identities}
    unsafe = frozenset(value.casefold() for value in unsafe_packages)
    available_rewards = None if available_reward_packages is None else frozenset(
        package_path_from_object_path(value).casefold() for value in available_reward_packages
    )

    # Check rewards available
    def rewards_available(rewards: Iterable[RewardPayload]) -> bool:
        return available_rewards is None or all(
            package_path_from_object_path(reward.article_path).casefold() in available_rewards
            for reward in rewards
        )
    supported_quest_destinations = frozenset({
        *SUPPORTED_QUEST_ARTICLE_GRANT_DESTINATIONS,
        *supported_quest_grant_destinations,
    })
    healing_location_types = {
        "HEALING_UPGRADE_GOLDEN_SEED",
        "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
    }
    all_locations = tuple(
        location for location in locations
        if include_healing or location.location_type not in healing_location_types
    )
    by_slot: dict[WorldLootSlot, WorldLootLocation] = {}
    for location in all_locations:
        if location.export_index < 1:
            raise ValueError("World Loot export indices must be positive Unreal 1-based values.")
        if not location.rewards:
            raise ValueError("World Loot locations must contain at least one complete reward tuple.")
        slot = world_loot_slot(location)
        if slot in by_slot:
            raise ValueError(f"Duplicate World Loot location slot {slot!r}.")
        by_slot[slot] = location
    unknown_exclusions = excluded - by_slot.keys()
    if unknown_exclusions:
        raise ValueError(f"Excluded World Loot locations are missing: {sorted(unknown_exclusions)!r}.")

    locked: list[LockedWorldLootLocation] = []
    explicitly_excluded: list[WorldLootLocation] = []
    disabled: list[WorldLootLocation] = []
    eligible: list[WorldLootLocation] = []
    reward_sources: list[WorldLootLocation] = []
    unavailable_what: list[WorldLootLocation] = []
    mirrored = tuple(
        sorted(
            (location for location in all_locations if location.mirror_of_package_path is not None),
            key=world_loot_slot,
        )
    )
    canonical_locations = tuple(
        location for location in all_locations if location.mirror_of_package_path is None
    )
    for location in sorted(canonical_locations, key=world_loot_slot):
        reason = _lock_reason(location, unsafe, supported_quest_destinations)
        if reason is not None:
            locked.append(LockedWorldLootLocation(location, reason))
        elif any(reward.article_path.casefold() in reserved for reward in location.rewards):
            locked.append(LockedWorldLootLocation(location, "EXTRACTOR_BOOSTER_RESERVED"))
        elif world_loot_slot(location) in excluded:
            explicitly_excluded.append(location)
        else:
            # WHERE availability controls whether the destination exists.
            # WHAT availability independently controls whether its vanilla
            # payload can enter the candidate pool.
            if rewards_available(location.rewards):
                reward_sources.append(location)
            else:
                unavailable_what.append(location)
            if not enabled:
                disabled.append(location)
            else:
                eligible.append(location)

    locked_reason_by_slot = {
        world_loot_slot(item.location): item.reason for item in locked
    }
    excluded_by_slot = {world_loot_slot(item) for item in explicitly_excluded}
    disabled_by_slot = {world_loot_slot(item) for item in disabled}
    for mirror in mirrored:
        if world_loot_slot(mirror) in excluded:
            explicitly_excluded.append(mirror)
            excluded_by_slot.add(world_loot_slot(mirror))
            continue
        assert mirror.mirror_of_package_path is not None
        assert mirror.mirror_of_export_index is not None
        canonical_slot = (
            canonical_package_key(mirror.mirror_of_package_path)[0],
            mirror.mirror_of_export_index,
        )
        if canonical_slot in locked_reason_by_slot:
            locked.append(LockedWorldLootLocation(mirror, locked_reason_by_slot[canonical_slot]))
        elif canonical_slot in excluded_by_slot:
            explicitly_excluded.append(mirror)
            excluded_by_slot.add(world_loot_slot(mirror))
        elif canonical_slot in disabled_by_slot:
            disabled.append(mirror)

    if not enabled:
        reward_sources.clear()
        unavailable_what.clear()

    by_arity: dict[int, list[WorldLootLocation]] = defaultdict(list)
    for location in eligible:
        by_arity[len(location.rewards)].append(location)

    source_by_arity: dict[int, list[WorldLootRewardCandidate]] = defaultdict(list)
    expected_source_by_occurrence: dict[str, str] = {}
    for location in reward_sources:
        candidate = WorldLootRewardCandidate(
            candidate_id=f"location:{location.package_path.casefold()}#{location.export_index}",
            rewards=location.rewards,
            source_location=location,
            source_occurrence_ids=tuple(
                world_loot_occurrence_id(location, reward_index)
                for reward_index in range(len(location.rewards))
            ),
        )
        source_by_arity[len(location.rewards)].append(candidate)
        for occurrence_id, reward in zip(candidate.source_occurrence_ids, candidate.rewards, strict=True):
            key = occurrence_id.casefold()
            if key in expected_source_by_occurrence:
                raise ValueError(f"Duplicate eligible World Loot source occurrence ID {occurrence_id!r}.")
            expected_source_by_occurrence[key] = reward.article_path.casefold()

    assignments: list[WorldLootAssignment] = []
    all_catalog_candidates = tuple(sorted(
        (
            candidate for candidate in (*catalog_candidates, *gadget_candidates)
            if rewards_available(candidate.rewards)
        ),
        key=lambda candidate: (
            canonical_sort_key(candidate.candidate_id),
            canonical_sort_key(candidate.canonical_identity or ""),
        ),
    ))
    candidate_ids = [candidate.candidate_id.casefold() for candidate in all_catalog_candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("World Loot candidate IDs must be unique, case-insensitively.")
    if not any(len(location.rewards) == 1 for location in eligible):
        # If availability filtering leaves no safe singleton slots, keep all vanilla
        # payloads unchanged and do not fail unrelated feature generation.
        all_catalog_candidates = ()
    original_category_counts: dict[str, int] = defaultdict(int)
    for candidate in all_catalog_candidates:
        if candidate.candidate_category is not None:
            original_category_counts[candidate.candidate_category] += 1
    alias_to_identity: dict[str, str] = {}
    candidate_by_identity: dict[str, WorldLootRewardCandidate] = {}
    category_counts: dict[str, int] = defaultdict(int)
    for candidate in all_catalog_candidates:
        if (
            len(candidate.rewards) != 1
            or candidate.candidate_category is None
            or candidate.canonical_identity is None
        ):
            raise ValueError("Canonical World Loot catalog candidates must be one-item payloads with an identity.")
        identity = candidate.canonical_identity.casefold()
        if identity in candidate_by_identity:
            raise ValueError("World Loot reward catalogs contain duplicate canonical identities.")
        candidate_by_identity[identity] = candidate
        category_counts[candidate.candidate_category] += 1
        for alias in (*candidate.identity_aliases, candidate.canonical_identity,
                      candidate.rewards[0].article_path):
            key = alias.casefold()
            previous = alias_to_identity.setdefault(key, identity)
            if previous != identity:
                raise ValueError(f"World Loot candidate alias {alias!r} maps to multiple canonical rewards.")

    pool_stats: list[WorldLootCandidatePoolStats] = []
    unmatched: list[WorldLootLocation] = []
    overlay_displaced_payloads = 0
    singleton_overlay_handled = False
    source_selection_rng = _source_selection_rng(normalized.internal_value)
    all_world_articles = {
        reward.article_path.casefold()
        for location in all_locations
        for reward in location.rewards
    }
    for arity in sorted(by_arity):
        destinations = sorted(by_arity[arity], key=world_loot_slot)
        sources = sorted(source_by_arity.get(arity, ()), key=lambda source: canonical_sort_key(source.candidate_id))
        if all_catalog_candidates and arity == 1:
            singleton_overlay_handled = True
            existing_by_identity: dict[str, list[WorldLootRewardCandidate]] = defaultdict(list)
            ordinary: list[WorldLootRewardCandidate] = []
            reserved_by_category: dict[str, int] = defaultdict(int)
            for source in sources:
                reward = source.rewards[0]
                key = reward.article_path.casefold()
                if key in reserved:
                    reserved_by_category["booster"] += 1
                    continue
                identity = alias_to_identity.get(key)
                if identity is None:
                    ordinary.append(source)
                    continue
                canonical = candidate_by_identity[identity]
                if (reward.count, reward.notification) != (
                    canonical.rewards[0].count, canonical.rewards[0].notification
                ):
                    raise ValueError(
                        f"Existing {canonical.candidate_category} World Loot payload no longer matches its catalog evidence."
                    )
                # Canonical metadata names an Article, not an occurrence. Keep
                # every physical slot carrying that Article in the source pool.
                existing_by_identity[identity].append(WorldLootRewardCandidate(
                    candidate_id=source.candidate_id,
                    rewards=source.rewards,
                    source_location=source.source_location,
                    candidate_category=canonical.candidate_category,
                    canonical_identity=canonical.canonical_identity,
                    identity_aliases=canonical.identity_aliases,
                    source_occurrence_ids=source.source_occurrence_ids,
                    source_locations=source.source_locations,
                    reward_categories=source.reward_categories,
                ))
            # A catalog entry classifies an existing source; it never creates
            # an occurrence. External rewards must be transferred by a joint
            # owner-aware planner, which this pool does not currently expose.
            missing: list[WorldLootRewardCandidate] = []
            unowned_by_category: dict[str, int] = defaultdict(int)
            for identity, candidate in candidate_by_identity.items():
                if identity in existing_by_identity:
                    continue
                article_key = candidate.rewards[0].article_path.casefold()
                # Protected World sources and sources owned by another system
                # remain vanilla. Even an attached occurrence ID cannot be
                # imported unless that owner removes it from its own pool.
                assert candidate.candidate_category is not None
                unowned_by_category[candidate.candidate_category] += 1
                if article_key in all_world_articles and candidate.source_occurrence_ids:
                    occurrence_ids = {value.casefold() for value in candidate.source_occurrence_ids}
                    if occurrence_ids & expected_source_by_occurrence.keys():
                        raise ValueError(
                            f"Catalog candidate {candidate.candidate_id!r} has an eligible physical source "
                            "which was not matched by its canonical Article aliases."
                        )
            pool_count = len(ordinary) + sum(len(items) for items in existing_by_identity.values()) + len(missing)
            if pool_count < len(destinations):
                shortfall = len(destinations) - pool_count
                unmatched.extend(source_selection_rng.sample(destinations, shortfall))
                unmatched_slots = {world_loot_slot(item) for item in unmatched}
                destinations = [item for item in destinations if world_loot_slot(item) not in unmatched_slots]
            displaced_count = pool_count - len(destinations)
            if displaced_count != 0:
                raise ValueError(
                    "World Loot source occurrence count does not match its movable destination count "
                    f"after catalog ownership checks ({pool_count} sources, {len(destinations)} destinations)."
                )
            sources = [
                *ordinary,
                *(source for identity in sorted(existing_by_identity) for source in existing_by_identity[identity]),
                *missing,
            ]
            overlay_displaced_payloads = 0
            present_by_category: dict[str, int] = defaultdict(int)
            present_identities_by_category: dict[str, list[str]] = defaultdict(list)
            injected_by_category: dict[str, list[str]] = defaultdict(list)
            for candidates in existing_by_identity.values():
                for candidate in candidates:
                    assert candidate.candidate_category is not None
                    assert candidate.canonical_identity is not None
                    present_by_category[candidate.candidate_category] += 1
                    present_identities_by_category[candidate.candidate_category].append(candidate.canonical_identity)
            for candidate in missing:
                assert candidate.candidate_category is not None and candidate.canonical_identity is not None
                injected_by_category[candidate.candidate_category].append(candidate.canonical_identity)
            for category in sorted(category_counts):
                pool_stats.append(WorldLootCandidatePoolStats(
                    candidate_category=category,
                    canonical_candidates=present_by_category[category] + len(injected_by_category[category]),
                    already_present_count=present_by_category[category],
                    injected_count=len(injected_by_category[category]),
                    duplicate_sources_removed=0,
                    already_present_identities=tuple(sorted(
                        present_identities_by_category[category], key=str.casefold,
                    )),
                    injected_identities=tuple(sorted(injected_by_category[category], key=str.casefold)),
                    reserved_sources_removed=reserved_by_category[category],
                    unselected_for_capacity=0,
                    unowned_candidates_skipped=unowned_by_category[category],
                ))
            if len(sources) != len(destinations):
                raise ValueError("Canonical reward overlay changed the World Loot candidate count.")
            if len({candidate.candidate_id for candidate in sources}) != len(sources):
                raise ValueError("Canonical reward overlay created duplicate World Loot candidate identities.")
        elif all_catalog_candidates and any(
            alias_to_identity.get(reward.article_path.casefold()) or reward.article_path.casefold() in reserved
            for location in destinations for reward in location.rewards
        ):
            raise ValueError("A reserved or canonical catalog reward was found in a multi-reward location; V1 cannot safely balance that bundle.")
        elif len(sources) > len(destinations):
            raise ValueError(
                f"World Loot has {len(sources)} source occurrences for only {len(destinations)} destinations "
                f"in reward-arity group {arity}; refusing to change reward multiplicity."
            )
        elif len(sources) < len(destinations):
            shortfall = len(destinations) - len(sources)
            unmatched.extend(source_selection_rng.sample(destinations, shortfall))
            unmatched_slots = {world_loot_slot(item) for item in unmatched}
            destinations = [item for item in destinations if world_loot_slot(item) not in unmatched_slots]

        shuffled_sources = _derangement(
            destinations,
            sources,
            _group_rng(normalized.internal_value, arity),
        )
        assignments.extend(
            WorldLootAssignment(destination, source.source_location, source)
            for destination, source in zip(destinations, shuffled_sources, strict=True)
        )

    if all_catalog_candidates and not singleton_overlay_handled:
        raise ValueError("Canonical World Loot candidates require at least one movable singleton destination.")

    assignments = _intermix_hot_spring_reward_slots(assignments, normalized.internal_value)
    assignments = _avoid_fixed_healing_reward_paths(assignments, normalized.internal_value)

    # Repeated recipe grants are represented by safe ordinary loot fillers in
    # the same source occurrence slots. Reflect that deliberate value change in
    # the occurrence audit while still requiring every slot exactly once.
    for assignment in assignments:
        candidate = assignment.source_candidate
        if candidate is None or not candidate.candidate_id.startswith(_HOT_SPRING_REPEAT_RECIPE_FILLER_PREFIX):
            continue
        for occurrence_id, reward in zip(
            candidate.source_occurrence_ids, candidate.rewards, strict=True,
        ):
            expected_source_by_occurrence[occurrence_id.casefold()] = reward.article_path.casefold()

    canonical_assignments = tuple(
        assignment for assignment in assignments
        if assignment.destination.mirror_of_package_path is None
    )
    assigned_occurrence_to_article: dict[str, str] = {}
    assigned_article_counts: dict[str, int] = defaultdict(int)
    for assignment in canonical_assignments:
        candidate = assignment.source_candidate
        if candidate is None:
            raise ValueError("World Loot assignment omitted its source occurrence.")
        occurrence_ids = candidate.source_occurrence_ids
        if not occurrence_ids and candidate.source_location is not None:
            occurrence_ids = tuple(
                world_loot_occurrence_id(candidate.source_location, reward_index)
                for reward_index in range(len(candidate.rewards))
            )
        if len(occurrence_ids) != len(candidate.rewards):
            raise ValueError(
                f"World Loot source {candidate.candidate_id!r} has no stable ID for every reward occurrence."
            )
        for occurrence_id, reward in zip(occurrence_ids, candidate.rewards, strict=True):
            key = occurrence_id.casefold()
            if key in assigned_occurrence_to_article:
                raise ValueError(f"Duplicate World Loot source occurrence ID {occurrence_id!r}.")
            assigned_occurrence_to_article[key] = reward.article_path.casefold()
            assigned_article_counts[reward.article_path.casefold()] += 1
    if assigned_occurrence_to_article != expected_source_by_occurrence:
        missing_occurrences = sorted(expected_source_by_occurrence.keys() - assigned_occurrence_to_article.keys())
        unexpected_occurrences = sorted(assigned_occurrence_to_article.keys() - expected_source_by_occurrence.keys())
        raise ValueError(
            "World Loot source occurrence ownership changed during pool assembly "
            f"(missing={missing_occurrences[:3]}, unexpected={unexpected_occurrences[:3]})."
        )
    expected_article_counts: dict[str, int] = defaultdict(int)
    for article_path in expected_source_by_occurrence.values():
        expected_article_counts[article_path] += 1
    if dict(expected_article_counts) != dict(assigned_article_counts):
        raise ValueError("World Loot Article multiset changed while mapping source occurrences to destinations.")

    canonical_assignment_by_slot = {
        world_loot_slot(assignment.destination): assignment for assignment in assignments
    }
    for mirror in mirrored:
        assert mirror.mirror_of_package_path is not None
        assert mirror.mirror_of_export_index is not None
        if world_loot_slot(mirror) in excluded_by_slot:
            continue
        canonical = canonical_assignment_by_slot.get((
            canonical_package_key(mirror.mirror_of_package_path)[0],
            mirror.mirror_of_export_index,
        ))
        if canonical is not None:
            assignments.append(WorldLootAssignment(
                destination=mirror,
                source=canonical.source,
                source_candidate=canonical.source_candidate,
            ))
    assignments.sort(key=lambda assignment: world_loot_slot(assignment.destination))

    return WorldLootRandomizationPlan(
        assignments=tuple(assignments),
        locked_locations=tuple(locked),
        excluded_locations=tuple(explicitly_excluded),
        disabled_locations=tuple(disabled),
        mirrored_locations=mirrored,
        candidate_pool_stats=tuple(pool_stats),
        overlay_displaced_payloads=overlay_displaced_payloads,
        unmatched_locations=tuple(sorted(set(unmatched), key=world_loot_slot)),
        unavailable_what_locations=tuple(unavailable_what),
        eligible_reward_source_count=len(reward_sources),
        source_occurrence_ids=tuple(sorted(assigned_occurrence_to_article)),
        source_article_paths=tuple(sorted(
            article for article, count in expected_article_counts.items() for _ in range(count)
        )),
    )


# Resolve avoid fixed healing reward paths
def _avoid_fixed_healing_reward_paths(
    assignments: list[WorldLootAssignment], seed_value: int,
) -> list[WorldLootAssignment]:
    """Avoid silent Article-level no-ops at healing locations when a safe swap exists.

    Duplicate vanilla Nectar/Bead candidates can make a correct source-location
    derangement put the same Article back at a healing pickup. A deterministic
    swap with another singleton assignment preserves the full reward multiset.
    """
    result = assignments.copy()
    singleton_indices = [
        index for index, assignment in enumerate(result)
        if len(assignment.destination.rewards) == 1
    ]
    healing_indices = [
        index for index in singleton_indices
        if result[index].destination.location_type in {
            "HEALING_UPGRADE_GOLDEN_SEED",
            "HEALING_UPGRADE_GOLDEN_BLOOD_BEAD",
        }
    ]
    for index in healing_indices:
        healing = result[index]
        original_path = healing.destination.rewards[0].article_path.casefold()
        if healing.assigned_rewards[0].article_path.casefold() != original_path:
            continue
        if healing.source_candidate is None:
            continue
        healing_slot = world_loot_slot(healing.destination)
        partners = [
            other_index for other_index in singleton_indices
            if other_index != index and result[other_index].source_candidate is not None
        ]
        partners.sort(key=lambda other_index: (
            result[other_index].destination.instance_override_source is not None,
            world_loot_slot(result[other_index].destination),
        ))
        seed_material = (
            f"cv2-world-loot-healing-noop-v1\0{seed_value}\0"
            f"{healing_slot[0]}#{healing_slot[1]}"
        ).encode("utf-8")
        partner_rng = StableRandom.from_material(seed_material)
        partner_rng.shuffle(partners)
        for partner_index in partners:
            partner = result[partner_index]
            partner_slot = world_loot_slot(partner.destination)
            healing_source = healing.source_candidate
            partner_source = partner.source_candidate
            assert partner_source is not None
            healing_after = partner_source.rewards[0].article_path.casefold()
            partner_after = healing_source.rewards[0].article_path.casefold()
            if healing_after == original_path:
                continue
            if partner_after == partner.destination.rewards[0].article_path.casefold():
                continue
            if _candidate_slot(partner_source) == healing_slot:
                continue
            if _candidate_slot(healing_source) == partner_slot:
                continue
            result[index] = WorldLootAssignment(
                healing.destination, partner_source.source_location, partner_source,
            )
            result[partner_index] = WorldLootAssignment(
                partner.destination, healing_source.source_location, healing_source,
            )
            break
    return result


# Catalog overlay RNG
def _catalog_overlay_rng(seed_value: int, *, legacy_gadget_domain: bool = False) -> StableRandom:
    domain = (
        "cv2-world-loot-defensive-gadget-overlay-v1"
        if legacy_gadget_domain else "cv2-world-loot-canonical-reward-overlay-v1"
    )
    key = f"{domain}\0{seed_value}".encode("utf-8")
    return StableRandom.from_material(key)


# Create source selection RNG
def _source_selection_rng(seed_value: int) -> StableRandom:
    key = f"cv2-world-loot-source-selection-v1\0{seed_value}".encode("utf-8")
    return StableRandom.from_material(key)
