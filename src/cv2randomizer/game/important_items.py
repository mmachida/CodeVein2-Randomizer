"""Validated Key and quest/flag Important Article exclusions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.catalog_integrity import canonical_text_sha256

class ImportantItemExclusionError(ValueError):
    """Raised when canonical Important Item exclusion metadata is unsafe."""


@dataclass(frozen=True, slots=True)
class ImportantItemExclusions:
    """Exact canonical Article identities that must remain vanilla."""

    revision: str
    keys: tuple[str, ...]
    quest_flag_exclusions: tuple[str, ...]

    # List canonical key identities
    @property
    def key_identities(self) -> frozenset[str]:
        return frozenset(path.casefold() for path in self.keys)

    # Find quest flag identities
    @property
    def quest_flag_identities(self) -> frozenset[str]:
        return frozenset(path.casefold() for path in self.quest_flag_exclusions)


class ImportantItemExclusionStore:
    """Read exclusion identities from the existing reward-pool authority."""

    # Initialize this object
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_path("reward_pools", "current")

    # Load and validate catalog data
    def load(self, revision: str) -> ImportantItemExclusions:
        manifest_path = self.data_dir / "manifest.json"
        catalog_path = self.data_dir / "important_item_exclusions.json"
        try:
            manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = catalog_path.read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ImportantItemExclusionError(f"Could not read Important Item exclusion metadata: {error}.") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != "cv2-reward-pool-v1"
            or manifest.get("catalog_revision") != revision
        ):
            raise ImportantItemExclusionError("Important Item exclusion manifest schema or revision is invalid.")
        files = manifest.get("files")
        metadata = files.get(catalog_path.name) if isinstance(files, dict) else None
        if not isinstance(metadata, dict) or metadata.get("sha256") != canonical_text_sha256(payload):
            raise ImportantItemExclusionError("Important Item exclusion file hash does not match its manifest.")
        try:
            document: Any = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ImportantItemExclusionError(f"Important Item exclusion JSON is invalid: {error}.") from error
        if (
            not isinstance(document, dict)
            or document.get("schema") != "cv2-important-item-exclusions-v1"
            or document.get("catalog_revision") != revision
        ):
            raise ImportantItemExclusionError("Important Item exclusion document schema or revision is invalid.")
        keys = self._paths(document.get("keys"), "keys")
        quest_flags = self._paths(document.get("quest_flag_exclusions"), "quest_flag_exclusions")
        combined = [*keys, *quest_flags]
        if len({path.casefold() for path in combined}) != len(combined):
            raise ImportantItemExclusionError("Important Item exclusion identities must be unique and non-overlapping.")
        if any(
            "/item/important/" not in path.casefold()
            and not path.rsplit("/", 1)[-1].casefold().startswith("da_item_important_")
            and not path.rsplit("/", 1)[-1].casefold().startswith("da_important_")
            for path in quest_flags
        ):
            raise ImportantItemExclusionError(
                "Quest/flag exclusions must be Important Item Articles or explicitly named DA_Important assets."
            )
        declared_count = metadata.get("count")
        declared_counts = metadata.get("counts")
        if declared_count != len(combined) or declared_counts != {
            "keys": len(keys), "quest_flag_exclusions": len(quest_flags),
        }:
            raise ImportantItemExclusionError("Important Item exclusion counts do not match the manifest.")
        return ImportantItemExclusions(revision, keys, quest_flags)

    # Resolve paths
    @staticmethod
    def _paths(value: Any, label: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not value:
            raise ImportantItemExclusionError(f"Important Item {label} must be a non-empty array.")
        paths: list[str] = []
        for path in value:
            if (
                not isinstance(path, str)
                or not path.startswith("/Game/")
                or path.endswith("/")
                or "." in path.rsplit("/", 1)[-1]
                or not path.rsplit("/", 1)[-1]
            ):
                raise ImportantItemExclusionError(f"Invalid canonical Article path in {label}: {path!r}.")
            paths.append(path)
        if len({path.casefold() for path in paths}) != len(paths):
            raise ImportantItemExclusionError(f"Important Item {label} contains duplicate paths.")
        return tuple(paths)
