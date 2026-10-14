"""Stable seed normalization and random-number generator construction."""

import hashlib
import re
import secrets
from dataclasses import dataclass

from .determinism import StableRandom

_INTEGER_SEED = re.compile(r"[+-]?[0-9]+\Z")
_SEED_DOMAIN = b"code-vein-2-randomizer\0seed-v1\0"


@dataclass(frozen=True, slots=True)
class NormalizedSeed:
    """Display form and stable internal value for one chosen seed."""

    display_value: str
    internal_value: int
    generated: bool = False

    # Make RNG
    def make_rng(self) -> StableRandom:
        """Create a private, versioned RNG stream for this normalized seed."""
        material = b"normalized-seed\0" + self.internal_value.to_bytes(32, "big")
        return StableRandom.from_material(material)


# Normalize seed
def normalize_seed(seed: str | int | None) -> NormalizedSeed:
    """Normalize integer/text input; an empty value gets one new random seed."""
    if seed is None:
        seed = ""
    if isinstance(seed, bool):
        raise TypeError("A boolean is not a valid seed.")
    if isinstance(seed, int):
        display_value = str(seed)
        kind = b"integer\0"
        canonical_value = display_value
        generated = False
    elif isinstance(seed, str):
        if not seed.strip():
            generated_value = str(secrets.randbits(63))
            normalized = normalize_seed(generated_value)
            return NormalizedSeed(
                normalized.display_value,
                normalized.internal_value,
                generated=True,
            )
        display_value = seed
        stripped_value = seed.strip()
        if _INTEGER_SEED.fullmatch(stripped_value):
            kind = b"integer\0"
            canonical_value = str(int(stripped_value, 10))
        else:
            kind = b"text\0"
            canonical_value = seed
        generated = False
    else:
        raise TypeError("A seed must be text, an integer, or None.")

    digest = hashlib.sha256(
        _SEED_DOMAIN + kind + canonical_value.encode("utf-8")
    ).digest()
    return NormalizedSeed(
        display_value=display_value,
        internal_value=int.from_bytes(digest, byteorder="big", signed=False),
        generated=generated,
    )


# Create a seed-based random stream
def create_rng(seed: str | int | NormalizedSeed | None) -> StableRandom:
    """Create an RNG from an existing normalized value or user seed."""
    normalized = seed if isinstance(seed, NormalizedSeed) else normalize_seed(seed)
    return normalized.make_rng()
