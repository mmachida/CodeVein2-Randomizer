"""Expose unallocated canonical Booster identities to the shared World Loot pool."""

from __future__ import annotations

from collections.abc import Iterable

from cv2randomizer.game.booster_reward_pool import BoosterRewardCandidate
from cv2randomizer.game.extractor_catalog import ExtractorRecipeSlot
from cv2randomizer.randomizer.extractor import ExtractorPlan
from cv2randomizer.randomizer.shops import ShopRandomizationPlan
from cv2randomizer.randomizer.source_occurrences import exchange_planned_reward_occurrence
from cv2randomizer.randomizer.world_loot import WorldLootRandomizationPlan


# Exchange extractor world sources
def exchange_extractor_world_sources(
    world_plan: WorldLootRandomizationPlan,
    extractor_plan: ExtractorPlan,
) -> WorldLootRandomizationPlan:
    """Move selected World Booster occurrences into recipes without duplication.

    Every World Booster selected for a recipe is exchanged for one vanilla
    recipe output that was not selected. This keeps the active World source
    occurrence count and the combined Booster Article multiset unchanged.
    """
    selected_paths = {
        item.assigned_reward.article_path.casefold()
        for item in extractor_plan.assignments
    }
    selected_world_sources: list[tuple[str, str, str]] = []
    for assignment in extractor_plan.assignments:
        reward = assignment.assigned_reward
        origin = next(
            (value for value in reward.origins if value.startswith("world-loot-occurrence:")),
            None,
        )
        if origin is not None:
            selected_world_sources.append((
                reward.article_path,
                origin.removeprefix("world-loot-occurrence:"),
                reward.content_class,
            ))

    unused_recipe_sources: dict[str, list[ExtractorRecipeSlot]] = {}
    for assignment in extractor_plan.assignments:
        slot = assignment.destination
        if slot.original_article_path.casefold() in selected_paths:
            continue
        group = "ALL" if extractor_plan.mix_postgame_dlc_with_base else slot.content_class
        unused_recipe_sources.setdefault(group, []).append(slot)
    selected_world_by_group: dict[str, list[tuple[str, str, str]]] = {}
    for item in selected_world_sources:
        group = "ALL" if extractor_plan.mix_postgame_dlc_with_base else item[2]
        selected_world_by_group.setdefault(group, []).append(item)
    if {key: len(value) for key, value in selected_world_by_group.items()} != {
        key: len(value) for key, value in unused_recipe_sources.items() if value
    }:
        raise ValueError(
            "Extractor Booster ownership transfer is unbalanced by content class: each selected World source "
            "must replace one unused vanilla recipe output from the same pool."
        )
    pairings: list[tuple[tuple[str, str, str], ExtractorRecipeSlot]] = []
    for group in sorted(unused_recipe_sources, key=str.casefold):
        recipe_slots = sorted(unused_recipe_sources[group], key=lambda slot: (
            slot.virtual_package.casefold(), slot.row_index, slot.row_name.casefold(),
        ))
        world_sources = sorted(selected_world_by_group.get(group, ()), key=lambda item: (
            item[1].casefold(), item[0].casefold(),
        ))
        pairings.extend(zip(world_sources, recipe_slots, strict=True))

    empty_shops = ShopRandomizationPlan((), (), (), ())
    updated = world_plan
    for (world_article, occurrence_id, _content_class), recipe_source in pairings:
        if world_article.casefold() == recipe_source.original_article_path.casefold():
            raise ValueError("Extractor transfer would return the same Booster identity to World Loot.")
        updated, empty_shops = exchange_planned_reward_occurrence(
            updated,
            empty_shops,
            source_occurrence_id=occurrence_id,
            replacement_article_path=recipe_source.original_article_path,
            replacement_occurrence_id=(
                f"extractor-recipe-return:{recipe_source.virtual_package.casefold()}#{recipe_source.row_index}"
            ),
            replacement_content_class=recipe_source.content_class,
        )
    return updated

