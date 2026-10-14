"""Canonical generic Article rewards reusable by compatible randomizer pools."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.catalogs import RewardPayload


_IRIS_REWARDS = {
    "iris_chara_episode_sample": (
        "DA_Booster_CharaEpisodeIrisSample_1",
        "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIrisSample_1",
        ("iris_character_episode_initial_reward",),
        1,
    ),
    "iris_chara_episode_reward_1": (
        "DA_Booster_CharaEpisodeIris_1",
        "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIris_1",
        ("iris_reward_1",),
        2,
    ),
    "iris_chara_episode_reward_2": (
        "DA_Booster_CharaEpisodeIris_2",
        "/Game/Blueprint/Article/Booster/Other/DA_Booster_CharaEpisodeIris_2",
        ("iris_reward_2",),
        2,
    ),
}


class CanonicalArticleRewardCatalogError(ValueError):
    """Raised when canonical reusable Article reward metadata is invalid."""


@dataclass(frozen=True, slots=True)
class CanonicalArticleReward:
    reward_id: str
    object: str
    asset_path: str
    reward_category: str
    count: int
    notification: bool
    world_reward_eligible: bool
    source_location_ids: tuple[str, ...]
    physical_source_occurrences: int
    content_class: str = "UNKNOWN"

    # Read the reward entry
    @property
    def reward(self) -> RewardPayload:
        return RewardPayload(self.asset_path, self.count, self.notification, self.content_class)


@dataclass(frozen=True, slots=True)
class CanonicalArticleRewardCatalog:
    revision: str
    items: tuple[CanonicalArticleReward, ...]

    # List world reward items
    @property
    def world_reward_items(self) -> tuple[CanonicalArticleReward, ...]:
        return tuple(item for item in self.items if item.world_reward_eligible)


class CanonicalArticleRewardCatalogStore:
    """Load the versioned WHAT catalog independently from reward locations."""

    # Initialize this object
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_path("reward_pools", "current")

    # Load and validate catalog data
    def load(self, revision: str) -> CanonicalArticleRewardCatalog:
        if not isinstance(revision, str) or not revision.strip():
            raise CanonicalArticleRewardCatalogError("Canonical Article reward revision must be non-empty.")
        manifest_path = self.data_dir / "manifest.json"
        catalog_path = self.data_dir / "canonical_article_rewards.json"
        try:
            manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = catalog_path.read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CanonicalArticleRewardCatalogError(f"Could not read canonical Article reward catalog: {error}.") from error
        if not isinstance(manifest, dict) or manifest.get("schema") != "cv2-reward-pool-v1":
            raise CanonicalArticleRewardCatalogError("Canonical Article reward pool manifest has an unknown schema.")
        if manifest.get("catalog_revision") != revision:
            raise CanonicalArticleRewardCatalogError("Canonical Article reward pool revision does not match the game catalog.")
        metadata = manifest.get("files", {}).get(catalog_path.name)
        if not isinstance(metadata, dict) or metadata.get("count") != len(_IRIS_REWARDS):
            raise CanonicalArticleRewardCatalogError("Canonical Article reward manifest must declare exactly three Iris rewards.")
        if metadata.get("sha256") != hashlib.sha256(payload).hexdigest():
            raise CanonicalArticleRewardCatalogError("Canonical Article reward catalog hash does not match its manifest.")
        try:
            document: Any = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise CanonicalArticleRewardCatalogError(f"Canonical Article reward JSON is invalid: {error}.") from error
        if (
            not isinstance(document, dict)
            or document.get("schema") != "cv2-canonical-article-rewards-v1"
            or document.get("catalog_revision") != revision
            or document.get("expected_counts") != {"total": 3, "BOOSTER": 3}
        ):
            raise CanonicalArticleRewardCatalogError("Canonical Article reward metadata schema or expected counts are invalid.")
        raw_items = document.get("items")
        if not isinstance(raw_items, list) or len(raw_items) != 3:
            raise CanonicalArticleRewardCatalogError("Canonical Article reward catalog must contain three unique Iris identities.")
        items: list[CanonicalArticleReward] = []
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        seen_source_ids: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise CanonicalArticleRewardCatalogError("Canonical Article reward entries must be objects.")
            reward_id = _string(raw.get("reward_id"), "reward_id")
            object_name = _string(raw.get("object"), "object")
            asset_path = _string(raw.get("asset_path"), "asset_path")
            reward_category = _string(raw.get("reward_category"), "reward_category").upper()
            count = _integer(raw.get("count"), "count", minimum=1)
            notification = _boolean(raw.get("notification"), "notification")
            eligible = _boolean(raw.get("world_reward_eligible"), "world_reward_eligible")
            source_ids = _string_array(raw.get("source_location_ids"), "source_location_ids")
            physical_occurrences = _integer(raw.get("physical_source_occurrences"), "physical_source_occurrences", minimum=1)
            content_class = str(raw.get("content_class", "UNKNOWN")).upper()
            if content_class not in {"BASE", "DLC", "AFTER_ENDING", "UNKNOWN"}:
                raise CanonicalArticleRewardCatalogError(
                    f"Canonical Article reward {reward_id!r} has an unsupported content class."
                )
            expected = _IRIS_REWARDS.get(reward_id)
            if (
                expected is None
                or (object_name, asset_path, source_ids, physical_occurrences) != expected
                or reward_category != "BOOSTER"
                or not asset_path.startswith("/Game/Blueprint/Article/")
                or asset_path.rsplit("/", 1)[-1] != object_name
                or count != 1
                or notification is not True
                or eligible is not True
            ):
                raise CanonicalArticleRewardCatalogError(f"Canonical Article reward {reward_id!r} differs from confirmed Iris metadata.")
            if reward_id.casefold() in seen_ids or asset_path.casefold() in seen_paths:
                raise CanonicalArticleRewardCatalogError("Canonical Article reward IDs and paths must be unique.")
            if any(source_id.casefold() in seen_source_ids for source_id in source_ids):
                raise CanonicalArticleRewardCatalogError("A logical source slot cannot create duplicate canonical reward identities.")
            seen_ids.add(reward_id.casefold())
            seen_paths.add(asset_path.casefold())
            seen_source_ids.update(source_id.casefold() for source_id in source_ids)
            items.append(CanonicalArticleReward(
                reward_id, object_name, asset_path, reward_category, count, notification,
                eligible, source_ids, physical_occurrences,
                content_class,
            ))
        if seen_ids != {value.casefold() for value in _IRIS_REWARDS}:
            raise CanonicalArticleRewardCatalogError("Canonical Article reward catalog does not contain exactly the three Iris identities.")
        items.sort(key=lambda item: item.reward_id.casefold())
        return CanonicalArticleRewardCatalog(revision, tuple(items))


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalArticleRewardCatalogError(f"{label} must be a non-empty string.")
    return value


# Parse an integer value
def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CanonicalArticleRewardCatalogError(f"{label} must be an integer >= {minimum}.")
    return value


# Parse a boolean value
def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise CanonicalArticleRewardCatalogError(f"{label} must be a boolean.")
    return value


# Parse string array
def _string_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CanonicalArticleRewardCatalogError(f"{label} must be a non-empty array.")
    return tuple(_string(item, label) for item in value)
