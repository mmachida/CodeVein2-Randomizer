"""Version-bound metadata for the 102 Blood Code mastery Booster reward slots."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.catalog_integrity import canonical_text_sha256


BOOSTER_TABLE_PACKAGE = "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/DT_GainBoosterSmallScenario"
BOOSTER_ROW_STRUCT_PACKAGE = "/Game/Cinematics/CinematicScene/Scenario/SmallScenario/Blueprint/Tool/STR_GainItemSmallScenario"
CONTENT_CLASS_COUNTS = {"BASE": 57, "DLC": 18, "AFTER_ENDING": 27}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FLAG_RE = re.compile(r"(?<![A-Za-z0-9_])(Booster_[A-Za-z0-9_]+)\s*==\s*false")
_MASTERY_RE = re.compile(r"\bBloodCodeMastery\.([A-Za-z0-9_]+)\s*>=\s*(Rank[0-9]+_Master)\b")


class BoosterCatalogError(ValueError):
    """Raised when the mastery Booster catalog is absent, stale, or unsafe."""


@dataclass(frozen=True, slots=True)
class BoosterRewardIdentity:
    story_flag: str
    article_path: str

    # Return reward ID
    @property
    def reward_id(self) -> str:
        return f"{self.story_flag}|{self.article_path}"


@dataclass(frozen=True, slots=True)
class BoosterMasterySlot:
    row_name: str
    row_index: int
    content_class: str
    evaluation_formula_original: str
    mastery_owner: str
    mastery_rank: str
    blood_code_article_path: str
    blood_code_article_asset: str
    booster_story_flag: str
    booster_article_path: str
    booster_article_asset: str
    count: int
    b_notification: bool

    # Read original reward
    @property
    def original_reward(self) -> BoosterRewardIdentity:
        return BoosterRewardIdentity(self.booster_story_flag, self.booster_article_path)


@dataclass(frozen=True, slots=True)
class BoosterCatalog:
    revision: str
    slots: tuple[BoosterMasterySlot, ...]
    source_package_sha256: dict[str, str]


class BoosterCatalogStore:
    """Load and check the exact version-bound Booster row inventory."""

    # Initialize this object
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_path("boosters", "current")

    # Load and validate catalog data
    def load(self, revision: str) -> BoosterCatalog:
        if not isinstance(revision, str) or not revision.strip():
            raise BoosterCatalogError("Booster catalog revision must be a non-empty identifier.")
        manifest_path = self.data_dir / "manifest.json"
        catalog_path = self.data_dir / "mastery_boosters.json"
        try:
            manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = catalog_path.read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BoosterCatalogError(f"Could not read Booster catalog files: {error}") from error
        if not isinstance(manifest, dict) or manifest.get("schema") != "cv2-booster-catalog-v1":
            raise BoosterCatalogError("Booster catalog manifest has an unknown schema.")
        if manifest.get("catalog_revision") != revision:
            raise BoosterCatalogError(f"Booster catalog is bound to revision {manifest.get('catalog_revision')!r}, not {revision!r}.")
        if manifest.get("source_package") != BOOSTER_TABLE_PACKAGE:
            raise BoosterCatalogError("Booster catalog is bound to an unexpected source DataTable.")
        metadata = manifest.get("files", {}).get(catalog_path.name)
        if not isinstance(metadata, dict) or metadata.get("count") != 102:
            raise BoosterCatalogError("Booster catalog manifest does not declare 102 rows.")
        if not isinstance(metadata.get("sha256"), str) or canonical_text_sha256(payload) != metadata["sha256"].casefold():
            raise BoosterCatalogError("Booster row JSON hash does not match its manifest.")
        try:
            document: Any = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BoosterCatalogError(f"Could not parse Booster row JSON: {error}") from error
        if not isinstance(document, dict) or document.get("schema") != "cv2-booster-mastery-v1" or document.get("catalog_revision") != revision:
            raise BoosterCatalogError("Booster row catalog schema or revision is invalid.")
        raw_slots = document.get("slots")
        if not isinstance(raw_slots, list):
            raise BoosterCatalogError("Booster catalog does not contain a slot list.")
        slots: list[BoosterMasterySlot] = []
        seen_rows: set[str] = set()
        seen_flags: set[str] = set()
        for raw in raw_slots:
            if not isinstance(raw, dict):
                raise BoosterCatalogError("Booster catalog contains a non-object slot.")
            try:
                slot = BoosterMasterySlot(
                    row_name=_string(raw.get("row_name"), "row_name"),
                    row_index=_integer(raw.get("row_index"), "row_index", minimum=0),
                    content_class=_string(raw.get("content_class"), "content_class").upper(),
                    evaluation_formula_original=_string(raw.get("evaluation_formula_original"), "evaluation_formula_original"),
                    mastery_owner=_string(raw.get("mastery_owner"), "mastery_owner"),
                    mastery_rank=_string(raw.get("mastery_rank"), "mastery_rank"),
                    blood_code_article_path=_package(raw.get("blood_code_article_path")),
                    blood_code_article_asset=_string(raw.get("blood_code_article_asset"), "blood_code_article_asset"),
                    booster_story_flag=_string(raw.get("booster_story_flag"), "booster_story_flag"),
                    booster_article_path=_package(raw.get("booster_article_path")),
                    booster_article_asset=_string(raw.get("booster_article_asset"), "booster_article_asset"),
                    count=_integer(raw.get("count"), "count", minimum=1),
                    b_notification=_boolean(raw.get("b_notification"), "b_notification"),
                )
            except ValueError as error:
                raise BoosterCatalogError(f"Invalid Booster slot: {error}") from error
            if slot.content_class not in {*CONTENT_CLASS_COUNTS, "UNKNOWN"}:
                raise BoosterCatalogError(f"Invalid content class for {slot.row_name}.")
            if slot.row_index in {other.row_index for other in slots} or slot.row_name.casefold() in seen_rows:
                raise BoosterCatalogError(f"Duplicate Booster row identity {slot.row_name!r}.")
            if slot.booster_story_flag.casefold() in seen_flags:
                raise BoosterCatalogError(f"Duplicate Booster StoryFlag {slot.booster_story_flag!r}.")
            flags = _FLAG_RE.findall(slot.evaluation_formula_original)
            mastery = _MASTERY_RE.findall(slot.evaluation_formula_original)
            if flags != [slot.booster_story_flag] or mastery != [(slot.mastery_owner, slot.mastery_rank)]:
                raise BoosterCatalogError(f"Formula fields do not match the cataloged slot {slot.row_name}.")
            if slot.blood_code_article_path.rsplit("/", 1)[-1] != slot.blood_code_article_asset:
                raise BoosterCatalogError(f"Blood Code ArticleDataPath is malformed for {slot.row_name}.")
            if slot.booster_article_path.rsplit("/", 1)[-1] != slot.booster_article_asset:
                raise BoosterCatalogError(f"Booster ArticleDataPath is malformed for {slot.row_name}.")
            if not slot.booster_article_asset.startswith("DA_Booster_"):
                raise BoosterCatalogError(f"Slot {slot.row_name} does not grant a DA_Booster asset.")
            seen_rows.add(slot.row_name.casefold())
            seen_flags.add(slot.booster_story_flag.casefold())
            slots.append(slot)
        slots.sort(key=lambda slot: slot.row_index)
        counts = Counter(slot.content_class for slot in slots)
        if len(slots) != 102 or [slot.row_index for slot in slots] != list(range(102)):
            raise BoosterCatalogError(f"Booster catalog shape differs from V1: rows={len(slots)}, classes={dict(counts)}.")
        source_hashes = manifest.get("source_package_sha256")
        if not isinstance(source_hashes, dict) or set(source_hashes) != {".uasset", ".uexp"}:
            raise BoosterCatalogError("Booster source package fingerprints are incomplete.")
        if any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in source_hashes.values()):
            raise BoosterCatalogError("Booster source package fingerprint is invalid.")
        return BoosterCatalog(revision, tuple(slots), source_hashes)


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")
    return value


# Resolve a package asset path
def _package(value: Any) -> str:
    result = _string(value, "package")
    if not result.startswith("/Game/") or "\\" in result or result.endswith("/") or "." in result.rsplit("/", 1)[-1]:
        raise ValueError(f"invalid /Game package path {result!r}.")
    if any(part in {".", ".."} for part in result.split("/")):
        raise ValueError(f"unsafe package path {result!r}.")
    return result


# Parse an integer value
def _integer(value: Any, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}.")
    return value


# Parse a boolean value
def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean.")
    return value
