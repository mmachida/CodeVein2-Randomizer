"""Typed reservations for canonical rewards allocated by early planners."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable


@dataclass(frozen=True, slots=True, order=True)
class ReservedRewardIdentity:
    """A canonical full asset identity scoped to its reward category."""

    category: str
    asset_path: str

    # Validate initialized settings
    def __post_init__(self) -> None:
        category = self.category.strip().casefold()
        path = self.asset_path.strip()
        if not category:
            raise ValueError("Reserved reward category must not be empty.")
        if not path.startswith("/Game/") or "\\" in path or path.endswith("/"):
            raise ValueError("Reserved reward identity must be a canonical /Game asset path.")
        if any(part in {"", ".", ".."} for part in path.split("/")[1:]):
            raise ValueError("Reserved reward identity contains an unsafe path component.")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "asset_path", path)


class RewardReservationSet:
    """One shared allocation ledger used before flexible reward planners."""

    # Initialize this object
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ReservedRewardIdentity] = {}

    # Read reserve
    def reserve(self, category: str, asset_path: str) -> None:
        self.reserve_many(category, (asset_path,))

    # Read reserve many
    def reserve_many(self, category: str, asset_paths: Iterable[str]) -> None:
        pending = tuple(ReservedRewardIdentity(category, path) for path in asset_paths)
        keys = tuple((item.category, item.asset_path.casefold()) for item in pending)
        if len(keys) != len(set(keys)):
            raise ValueError("A reservation batch contains the same canonical reward more than once.")
        if any(key in self._items for key in keys):
            raise ValueError("A canonical reward was already reserved in the same category.")
        self._items.update(zip(keys, pending, strict=True))

    # Check whether the item exists
    def contains(self, category: str, asset_path: str) -> bool:
        key = (category.strip().casefold(), asset_path.casefold())
        return key in self._items

    # Read identities
    @property
    def identities(self) -> tuple[ReservedRewardIdentity, ...]:
        return tuple(sorted(self._items.values()))

    # Resolve asset paths
    @property
    def asset_paths(self) -> tuple[str, ...]:
        """Full paths for existing World/Shop planners, which match full paths."""
        return tuple(item.asset_path for item in self.identities)

    # Read paths
    def paths_for(self, category: str) -> tuple[str, ...]:
        """Return reserved full paths for one reward category."""
        normalized = category.strip().casefold()
        if not normalized:
            raise ValueError("Reserved reward category must not be empty.")
        return tuple(item.asset_path for item in self.identities if item.category == normalized)


__all__ = ["ReservedRewardIdentity", "RewardReservationSet"]
