"""Build a no-duplication Booster pool from recipes and physical World sources."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from cv2randomizer.game.extractor_catalog import ExtractorCatalog
from cv2randomizer.game.content_availability import package_path_from_object_path
from cv2randomizer.randomizer.world_loot import WorldLootRandomizationPlan


@dataclass(frozen=True, slots=True)
class BoosterRewardCandidate:
    article_path: str
    content_class: str
    origins: tuple[str, ...]


# Build extractor booster pool
def build_extractor_booster_pool(
    *,
    extractor: ExtractorCatalog,
    world_plan: WorldLootRandomizationPlan,
    available_reward_packages: Iterable[str] | None = None,
) -> tuple[BoosterRewardCandidate, ...]:
    """Build crafting sources from recipe outputs and active World occurrences.

    Mastery rewards are deliberately outside this pool: their Article and
    completion StoryFlag form one identity and are randomized only among the
    102 mastery rows. A World Booster that is moved into crafting must have its
    physical source occurrence replaced by an unused recipe output; callers do
    that with :func:`exchange_extractor_world_sources`.
    """
    available = None if available_reward_packages is None else frozenset(
        package_path_from_object_path(value).casefold() for value in available_reward_packages
    )
    candidates: list[BoosterRewardCandidate] = []
    for slot in extractor.slots:
        if available is not None and package_path_from_object_path(slot.original_article_path).casefold() not in available:
            continue
        candidates.append(BoosterRewardCandidate(
            slot.original_article_path,
            slot.content_class,
            (f"extractor-recipe:{slot.virtual_package.casefold()}#{slot.row_index}",),
        ))

    seen_occurrences: set[str] = set()
    for assignment in world_plan.assignments:
        if assignment.destination.mirror_of_package_path is not None:
            continue
        source = assignment.source_candidate
        if source is None:
            raise ValueError("World plan has an assignment without a source occurrence.")
        for index, reward in enumerate(source.rewards):
            if not reward.article_path.casefold().startswith("/game/blueprint/article/booster/"):
                continue
            if available is not None and package_path_from_object_path(reward.article_path).casefold() not in available:
                continue
            if index >= len(source.source_occurrence_ids):
                raise ValueError("World Booster source has no stable occurrence identity.")
            occurrence_id = source.source_occurrence_ids[index]
            occurrence_key = occurrence_id.casefold()
            if occurrence_key in seen_occurrences:
                raise ValueError(f"World Booster source occurrence {occurrence_id!r} is duplicated.")
            seen_occurrences.add(occurrence_key)
            candidates.append(BoosterRewardCandidate(
                reward.article_path,
                reward.content_class,
                (f"world-loot-occurrence:{occurrence_id}",),
            ))

    identities = [item.article_path.casefold() for item in candidates]
    if len(identities) != len(set(identities)):
        duplicates = sorted({path for path in identities if identities.count(path) > 1})
        raise ValueError(
            "Extractor and active World Booster sources contain repeated unique Article identities: "
            + ", ".join(duplicates[:5])
        )
    return tuple(sorted(candidates, key=lambda item: item.article_path.casefold()))
