"""Fast local display-name lookup for Spoiler.txt."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Mapping

from cv2randomizer.core.resources import application_data_path


_LOGGER = logging.getLogger(__name__)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_COUNT_SUFFIX = re.compile(r"\s+x(\d+)\s*$", re.IGNORECASE)
_ITEM_PREFIXES = ("DA_Item_", "DA_")
_SPOILER_CATEGORY_LABELS = frozenset({"Boosters", "Defensive Gadgets", "Jails"})


# Resolve spoiler item display name
def spoiler_item_display_name(value: str) -> str:
    """Resolve a known English name, otherwise render a readable asset label.

    The bundled JSON is loaded once per process. Generate never extracts game
    assets, consults the network, or rebuilds the name catalog.
    """
    text = value.strip()
    if not text:
        return text
    if " + " in text:
        return " + ".join(spoiler_item_display_name(part) for part in text.split(" + "))

    count_match = _COUNT_SUFFIX.search(text)
    count_suffix = count_match.group(0).strip() if count_match else ""
    if count_match:
        text = text[:count_match.start()].rstrip()

    leaf = _asset_leaf(text)
    names = _load_item_display_names()
    display_name = names.get(leaf.casefold())
    if display_name is None:
        # Shop FNames can carry a numeric instance suffix beyond the Article
        # asset's own numeric tier suffix (for example, `..._04_1`).
        candidate = leaf
        while display_name is None:
            match = re.search(r"_\d+$", candidate)
            if match is None:
                break
            candidate = candidate[:match.start()]
            display_name = names.get(candidate.casefold())
    display_name = display_name or _friendly_asset_name(leaf)
    if count_suffix:
        count = int(count_match.group(1))
        if count > 1:
            display_name = f"{display_name} x{count}"
    return display_name


# Find spoiler item category
def spoiler_item_category(value: str) -> str | None:
    """Return a precomputed spoiler section for known cross-pool item types.

    The small lookup is loaded once per process, so Spoiler.txt generation does
    not need to scan inventory reports or extract any game assets.
    """
    text = value.strip()
    if not text:
        return None
    if " + " in text:
        categories = {spoiler_item_category(part) for part in text.split(" + ")}
        return categories.pop() if len(categories) == 1 and None not in categories else None

    leaf = _asset_leaf(text)
    categories = _load_item_spoiler_categories()
    category = categories.get(leaf.casefold())
    candidate = leaf
    while category is None:
        match = re.search(r"_\d+$", candidate)
        if match is None:
            break
        candidate = candidate[:match.start()]
        category = categories.get(candidate.casefold())
    if category is not None:
        return category

    folded_path = text.casefold()
    folded_leaf = leaf.casefold()
    if folded_leaf.startswith("da_booster_") or "/article/booster/" in folded_path:
        return "Boosters"
    if folded_leaf.startswith("da_defgadget_") or "/article/gadget/defence/" in folded_path:
        return "Defensive Gadgets"
    if folded_leaf.startswith("da_jail_") or "/article/jail/" in folded_path:
        return "Jails"
    return category


# Resolve asset leaf
def _asset_leaf(value: str) -> str:
    identity = value.split("::", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if "." in identity:
        identity = identity.rsplit(".", 1)[-1]
    return identity.strip()


# Resolve friendly asset name
def _friendly_asset_name(value: str) -> str:
    """Remove Unreal's technical prefix and add word boundaries as a fallback."""
    name = value
    for prefix in _ITEM_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name = re.sub(r"^EnMat", "Enemy Material ", name)
    name = re.sub(r"^Mat(?=[A-Z])", "Material ", name)
    name = _CAMEL_BOUNDARY.sub(" ", name).replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return value
    return " ".join(
        token.upper() if token.isupper() or token.isdigit() else token[0].upper() + token[1:]
        for token in name.split(" ")
    )


# Load item display names
@lru_cache(maxsize=1)
def _load_item_display_names() -> Mapping[str, str]:
    path = application_data_path("spoiler_item_names.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        # Names are presentation-only; a missing file should not prevent a safe
        # randomizer build from completing.
        _LOGGER.warning("Could not read spoiler item names at %s: %s", path, error)
        return {}

    if not isinstance(document, dict) or document.get("schema_version") != 1:
        _LOGGER.warning("Ignoring spoiler item names with an unsupported schema: %s", path)
        return {}
    items = document.get("items")
    if not isinstance(items, dict):
        _LOGGER.warning("Ignoring spoiler item names without an item mapping: %s", path)
        return {}

    names: dict[str, str] = {}
    for asset_name, display_name in items.items():
        if (
            isinstance(asset_name, str)
            and asset_name.strip()
            and isinstance(display_name, str)
            and display_name.strip()
            and not any(char in asset_name + display_name for char in "\r\n")
        ):
            names[asset_name.casefold()] = display_name.strip()
    return names


# Load item spoiler categories
@lru_cache(maxsize=1)
def _load_item_spoiler_categories() -> Mapping[str, str]:
    path = application_data_path("spoiler_item_categories.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _LOGGER.warning("Could not read spoiler item categories at %s: %s", path, error)
        return {}

    if not isinstance(document, dict) or document.get("schema_version") != 1:
        _LOGGER.warning("Ignoring spoiler item categories with an unsupported schema: %s", path)
        return {}
    items = document.get("items")
    if not isinstance(items, dict):
        _LOGGER.warning("Ignoring spoiler item categories without an item mapping: %s", path)
        return {}

    categories: dict[str, str] = {}
    for asset_name, category in items.items():
        if (
            isinstance(asset_name, str)
            and asset_name.strip()
            and isinstance(category, str)
            and category in _SPOILER_CATEGORY_LABELS
            and not any(char in asset_name + category for char in "\r\n")
        ):
            categories[asset_name.casefold()] = category
    return categories
