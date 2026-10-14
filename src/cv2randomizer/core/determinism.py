"""Cross-runtime deterministic ordering and random-number primitives.

The algorithms in this module are part of the randomizer save/seed contract.
Changing them requires incrementing ``RANDOMIZER_ALGORITHM_VERSION``.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import MutableSequence, Sequence
from typing import TypeVar

RANDOMIZER_ALGORITHM_VERSION = 3
_DOMAIN_PREFIX = f"cv2-randomizer-prng\0algorithm={RANDOMIZER_ALGORITHM_VERSION}\0".encode("ascii")
_MASK_64 = (1 << 64) - 1
_RANGE_64 = 1 << 64
_SPLITMIX_GAMMA = 0x9E3779B97F4A7C15

T = TypeVar("T")


# Build canonical sort key
def canonical_sort_key(value: str) -> tuple[str, str]:
    """Return a locale-independent Unicode ordinal key with an explicit case policy."""
    normalized = unicodedata.normalize("NFC", value)
    return normalized.casefold(), normalized


# Build canonical package key
def canonical_package_key(value: str) -> tuple[str, str]:
    """Sort a virtual package identity independently of host path conventions."""
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    return normalized.casefold(), normalized


# Build canonical package identity
def canonical_package_identity(value: str) -> str:
    """Return the case-insensitive NFC form used for package identity comparisons."""
    return unicodedata.normalize("NFC", value.replace("\\", "/")).casefold()


class StableRandom:
    """A fixed SplitMix64 stream with versioned SHA-256 domain derivation.

    ``shuffle`` is descending Fisher-Yates; ``sample`` is the first ``k``
    entries of a partial Fisher-Yates permutation. ``randbelow`` uses
    rejection sampling, so modulo bias and runtime-library behavior are avoided.
    """

    __slots__ = ("_state",)

    # Initialize this object
    def __init__(self, state: int) -> None:
        if isinstance(state, bool) or not isinstance(state, int):
            raise TypeError("StableRandom state must be an integer.")
        self._state = state & _MASK_64

    # Seed the random stream from material
    @classmethod
    def from_material(cls, material: bytes | str) -> "StableRandom":
        """Derive an independent stable stream from explicit bytes or UTF-8 text."""
        encoded = material.encode("utf-8") if isinstance(material, str) else material
        if not isinstance(encoded, bytes):
            raise TypeError("StableRandom material must be bytes or text.")
        digest = hashlib.sha256(_DOMAIN_PREFIX + encoded).digest()
        return cls(int.from_bytes(digest[:8], "big"))

    # Generate the next 64-bit value
    def _next_u64(self) -> int:
        self._state = (self._state + _SPLITMIX_GAMMA) & _MASK_64
        value = self._state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK_64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK_64
        return (value ^ (value >> 31)) & _MASK_64

    # Choose a number below the bound
    def randbelow(self, bound: int) -> int:
        """Choose uniformly from ``range(bound)`` using fixed-width rejection sampling."""
        if isinstance(bound, bool) or not isinstance(bound, int) or bound <= 0:
            raise ValueError("StableRandom bound must be a positive integer.")
        if bound > _RANGE_64:
            raise ValueError("StableRandom supports bounds up to 2**64.")
        limit = _RANGE_64 - (_RANGE_64 % bound)
        while True:
            value = self._next_u64()
            if value < limit:
                return value % bound

    # Choose one item from options
    def choice(self, population: Sequence[T]) -> T:
        if not population:
            raise IndexError("Cannot choose from an empty sequence.")
        return population[self.randbelow(len(population))]

    # Shuffle items in place
    def shuffle(self, values: MutableSequence[T]) -> None:
        for index in range(len(values) - 1, 0, -1):
            other = self.randbelow(index + 1)
            values[index], values[other] = values[other], values[index]

    # Choose several items without replacement
    def sample(self, population: Sequence[T], count: int) -> list[T]:
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError("Sample count must be an integer.")
        if count < 0 or count > len(population):
            raise ValueError("Sample larger than population or is negative.")
        selected = list(population)
        for index in range(count):
            other = index + self.randbelow(len(selected) - index)
            selected[index], selected[other] = selected[other], selected[index]
        return selected[:count]
