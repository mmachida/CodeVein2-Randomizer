"""Explicit class labels for planner-only fixture scenarios.

Runtime-catalog tests use unmodified catalogs so unresolved classifications stay
UNKNOWN as research metadata. These helpers exercise planner mechanics with
deliberately BASE-classified inputs when a test needs a class boundary.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from cv2randomizer.game.catalogs import WorldLootLocation


# Read classified world fixture
def classified_world_fixture(
    locations: Iterable[WorldLootLocation],
    *,
    where_class: str = "BASE",
    what_class: str = "BASE",
) -> tuple[WorldLootLocation, ...]:
    """Fill only UNKNOWN fixture labels with explicit test classes."""
    return tuple(
        replace(
            location,
            content_class=(where_class if location.content_class == "UNKNOWN" else location.content_class),
            rewards=tuple(
                replace(
                    reward,
                    content_class=(what_class if reward.content_class == "UNKNOWN" else reward.content_class),
                )
                for reward in location.rewards
            ),
        )
        for location in locations
    )
