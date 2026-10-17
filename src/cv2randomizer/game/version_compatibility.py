"""Recognize helper errors caused by unsupported game data layouts."""

from __future__ import annotations


_LAYOUT_MISMATCH_MARKERS = (
    "invaliddataexception",
    "source fingerprint changed",
    "fingerprint differs",
    "fingerprint mismatch",
    "source hashes differ",
    "differs from the versioned catalog",
    "differs from the catalog",
    "does not match the catalog",
    "does not match its catalog",
    "do not match all catalogued",
    "do not match the blood code catalog",
    "source shape mismatch",
    "classified row mismatch",
    "slot count changed",
    "row layout changed",
    "required blueprint schemas could not be loaded",
    "parsed as rawexport",
    "catalog material class does not match source actor class",
    "catalog material class does not match the source actor class",
    "not the expected preserved task",
    "does not match the installed package",
)


def is_game_data_layout_mismatch(error: BaseException) -> bool:
    """Recognize source data that differs from its catalogued layout."""
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if any(marker in str(current).casefold() for marker in _LAYOUT_MISMATCH_MARKERS):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False
