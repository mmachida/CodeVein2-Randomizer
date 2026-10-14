"""Small player-facing Spoiler.txt writer."""

from __future__ import annotations

import os
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable
from collections.abc import Mapping

from cv2randomizer.core.determinism import canonical_sort_key
from cv2randomizer.game.item_display_names import spoiler_item_display_name


@dataclass(frozen=True, slots=True)
class SpoilerEntry:
    system: str
    vanilla: str
    randomized: str
    destination: str = ""
    randomized_label: str = ""
    stable_sort_key: str = ""
    vanilla_label: str = ""
    summary_count: int = 1

    # Validate initialized settings
    def __post_init__(self) -> None:
        if not self.system.strip() or not self.vanilla.strip() or not self.randomized.strip():
            raise ValueError("Spoiler entries require a system and both item identities.")
        if type(self.summary_count) is not int or self.summary_count < 1:
            raise ValueError("Spoiler summary counts must be positive integers.")


# Summarize spoiler assignment summary
def spoiler_assignment_summary(entries: Iterable[SpoilerEntry]) -> tuple[str, ...]:
    """Summarize all planned slots, including unchanged assignments omitted below."""
    counts: Counter[str] = Counter()
    unchanged: Counter[str] = Counter()
    for entry in entries:
        counts[entry.system] += entry.summary_count
        if entry.vanilla.casefold() == entry.randomized.casefold():
            unchanged[entry.system] += entry.summary_count

    return (
        f"Total Randomized: {sum(counts.values()):,} / "
        f"Unchanged: {sum(unchanged.values()):,} (same item after randomized)",
    )


# Read item label
def item_label(value: str) -> str:
    """Turn an internal Article/class identity into a short item name."""
    label = value.split("::", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if "." in label:
        label = label.rsplit(".", 1)[-1]
    return label or value


# Write spoiler
def write_spoiler(
    path: Path,
    entries: Iterable[SpoilerEntry],
    *,
    seed: str | int,
    metadata: Mapping[str, str] | None = None,
    randomizer_settings: Iterable[str] = (),
    created_date: str | None = None,
    summary_lines: Iterable[str] = (),
) -> Path:
    """Atomically write changed destinations and their vanilla/new item identities."""
    seed_text = str(seed)
    if not seed_text.strip() or "\n" in seed_text or "\r" in seed_text:
        raise ValueError("Spoiler seed must be a non-empty single line.")
    grouped: dict[str, list[tuple[str, str]]] = {}
    for entry in entries:
        if entry.vanilla.casefold() == entry.randomized.casefold():
            continue
        vanilla_label = spoiler_item_display_name(entry.vanilla_label or item_label(entry.vanilla))
        randomized_label = spoiler_item_display_name(entry.randomized_label or item_label(entry.randomized))
        line = (
            f"{entry.destination}: {vanilla_label} -> {randomized_label}"
            if entry.destination else
            f"{vanilla_label} -> {randomized_label}"
        )
        grouped.setdefault(entry.system, []).append((entry.stable_sort_key or line, line))
    chunks = [
        f"=== {system} ===\n" + "\n".join(
            line for _sort_key, line in sorted(
                lines,
                key=lambda item: (canonical_sort_key(item[0]), canonical_sort_key(item[1])),
            )
        )
        for system, lines in sorted(grouped.items(), key=lambda item: canonical_sort_key(item[0])) if lines
    ]
    body = "\n\n".join(chunks) if chunks else "No randomized replacements."
    metadata_values = dict(metadata or {})
    for key, value in metadata_values.items():
        if not key.strip() or any(character in key + value for character in "\r\n"):
            raise ValueError("Spoiler metadata keys and values must be non-empty single-line text.")
    ordered_metadata_keys = [
        key for key in ("App Version", "Game Version") if key in metadata_values
    ]
    remaining_metadata_keys = sorted(
        set(metadata_values) - set(ordered_metadata_keys),
        key=canonical_sort_key,
    )
    metadata_lines = [
        f"{key}: {metadata_values[key]}"
        for key in (*ordered_metadata_keys, *remaining_metadata_keys)
    ]
    summary = list(summary_lines)
    if created_date is not None:
        summary.insert(0, f"Created: {created_date}")
    if any(not line.strip() or "\n" in line or "\r" in line for line in summary):
        raise ValueError("Spoiler summary lines must be non-empty single-line text.")
    setting_lines = tuple(randomizer_settings)
    if any(not line.strip() or "\n" in line or "\r" in line for line in setting_lines):
        raise ValueError("Randomizer settings must be non-empty single-line text.")
    header_lines = [*summary, *metadata_lines]
    if setting_lines:
        header_lines.extend(("Randomizer Settings:", f"Seed: {seed_text}", *setting_lines))
    else:
        header_lines.append(f"Seed: {seed_text}")
    header = "\n".join(header_lines)
    content = f"{header}\n\n{body}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
