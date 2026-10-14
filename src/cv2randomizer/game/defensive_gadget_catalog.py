"""Canonical Defensive Gadget rewards and structurally confirmed direct grants."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.catalogs import RewardPayload

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTENT_COUNTS = {"BASE": 16, "DLC": 1}
_GADGET_PREFIX = "/Game/Blueprint/Article/Gadget/"


class DefensiveGadgetCatalogError(ValueError):
    """Raised when the Defensive Gadget reward pool is stale or malformed."""


@dataclass(frozen=True, slots=True)
class DefensiveGadget:
    object: str
    asset_path: str
    content_class: str
    aliases: tuple[str, ...]
    world_reward_eligible: bool
    npc_reward_eligible: bool

    # Read the reward entry
    @property
    def reward(self) -> RewardPayload:
        return RewardPayload(self.asset_path, 1, True, self.content_class)


@dataclass(frozen=True, slots=True)
class DefensiveGadgetGrantSlot:
    slot_id: str
    family: str
    content_class: str
    package_path: str
    export_name: str
    export_index: int
    bulk_index: int
    reward_index: int
    property_path: str
    original_asset_path: str
    count: int
    notification: bool
    source_sha256: dict[str, str]


@dataclass(frozen=True, slots=True)
class PendingDefensiveGadgetGrant:
    slot_id: str
    family: str
    content_class: str
    source_description: str
    status: str


@dataclass(frozen=True, slots=True)
class DefensiveGadgetCatalog:
    revision: str
    items: tuple[DefensiveGadget, ...]
    grants: tuple[DefensiveGadgetGrantSlot, ...]
    pending_grants: tuple[PendingDefensiveGadgetGrant, ...]
    world_reward_count: int
    world_reward_notification: bool
    world_reward_evidence_count: int

    # Resolve the requested asset
    def resolve(self, value: str) -> DefensiveGadget | None:
        normalized = value.casefold().rstrip("/")
        basename = normalized.rsplit("/", 1)[-1]
        for item in self.items:
            names = {item.object.casefold(), item.asset_path.casefold()}
            names.update(alias.casefold() for alias in item.aliases)
            if normalized in names or basename in names:
                return item
        return None

    # List candidates
    def candidates(self, *, mix_postgame_dlc_with_base: bool) -> tuple[DefensiveGadget, ...]:
        return tuple(
            item for item in self.items
            if item.npc_reward_eligible
            and (mix_postgame_dlc_with_base or item.content_class == "BASE")
        )


class DefensiveGadgetCatalogStore:
    """Load and checksum the game-version-bound reward pool."""

    # Initialize this object
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_path("reward_pools", "current")

    # Load and validate catalog data
    def load(self, revision: str) -> DefensiveGadgetCatalog:
        if not isinstance(revision, str) or not revision.strip():
            raise DefensiveGadgetCatalogError("Defensive Gadget catalog revision must be non-empty.")
        manifest_path = self.data_dir / "manifest.json"
        catalog_path = self.data_dir / "defensive_gadgets.json"
        try:
            manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = catalog_path.read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise DefensiveGadgetCatalogError(f"Could not read Defensive Gadget catalog: {error}") from error
        if not isinstance(manifest, dict) or manifest.get("schema") != "cv2-reward-pool-v1":
            raise DefensiveGadgetCatalogError("Defensive Gadget reward pool manifest has an unknown schema.")
        if manifest.get("catalog_revision") != revision:
            raise DefensiveGadgetCatalogError(
                f"Defensive Gadget pool is bound to {manifest.get('catalog_revision')!r}, not {revision!r}."
            )
        metadata = manifest.get("files", {}).get(catalog_path.name)
        if not isinstance(metadata, dict) or metadata.get("count") != 17:
            raise DefensiveGadgetCatalogError("Reward pool manifest must declare 17 canonical gadgets.")
        expected_hash = metadata.get("sha256")
        if not isinstance(expected_hash, str) or hashlib.sha256(payload).hexdigest() != expected_hash.casefold():
            raise DefensiveGadgetCatalogError("Defensive Gadget catalog hash does not match its manifest.")
        try:
            document: Any = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DefensiveGadgetCatalogError(f"Defensive Gadget catalog is invalid JSON: {error}") from error
        if not isinstance(document, dict) or document.get("schema") != "cv2-defensive-gadgets-v1":
            raise DefensiveGadgetCatalogError("Defensive Gadget catalog schema is unknown.")
        if document.get("catalog_revision") != revision:
            raise DefensiveGadgetCatalogError("Defensive Gadget catalog revision does not match the game catalog.")
        raw_items = document.get("items")
        if not isinstance(raw_items, list):
            raise DefensiveGadgetCatalogError("Defensive Gadget catalog does not contain an items array.")
        items: list[DefensiveGadget] = []
        item_names: set[str] = set()
        paths: set[str] = set()
        aliases: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise DefensiveGadgetCatalogError("Defensive Gadget item must be an object.")
            item = DefensiveGadget(
                object=_string(raw.get("object"), "gadget object"),
                asset_path=_package(raw.get("asset_path")),
                content_class=_string(raw.get("content_class"), "content class").upper(),
                aliases=_string_array(raw.get("aliases"), "gadget aliases"),
                world_reward_eligible=_boolean(raw.get("world_reward_eligible"), "world reward eligibility"),
                npc_reward_eligible=_boolean(raw.get("npc_reward_eligible"), "NPC reward eligibility"),
            )
            if item.content_class not in _CONTENT_COUNTS or not item.object.startswith("DA_DefGadget_"):
                raise DefensiveGadgetCatalogError(f"Invalid Defensive Gadget identity {item.object!r}.")
            if item.asset_path.rsplit("/", 1)[-1] != item.object:
                raise DefensiveGadgetCatalogError(f"Canonical path does not match {item.object}.")
            if item.object.casefold() in item_names or item.asset_path.casefold() in paths:
                raise DefensiveGadgetCatalogError("Defensive Gadget object names and canonical paths must be unique.")
            item_names.add(item.object.casefold())
            paths.add(item.asset_path.casefold())
            for alias in item.aliases:
                key = alias.casefold()
                if not alias.startswith("DA_DefGadget_") or key in aliases or key in item_names:
                    raise DefensiveGadgetCatalogError(f"Duplicate or malformed Defensive Gadget alias {alias!r}.")
                aliases.add(key)
            items.append(item)
        counts = Counter(item.content_class for item in items)
        if aliases & item_names:
            raise DefensiveGadgetCatalogError("Defensive Gadget aliases must not collide with canonical object names.")
        if len(items) != 17 or dict(counts) != _CONTENT_COUNTS:
            raise DefensiveGadgetCatalogError(f"Expected 17 gadgets (16 BASE / 1 DLC), got {dict(counts)}.")
        if not all(item.world_reward_eligible and item.npc_reward_eligible for item in items):
            raise DefensiveGadgetCatalogError("All 17 catalogued Defensive Gadgets must be eligible reward candidates.")
        expected_counts = document.get("expected_counts")
        if expected_counts != {"total": 17, "BASE": 16, "DLC": 1}:
            raise DefensiveGadgetCatalogError("Defensive Gadget catalog expected counts are invalid.")
        payload_info = document.get("world_reward_payload")
        if not isinstance(payload_info, dict):
            raise DefensiveGadgetCatalogError("World reward payload evidence is missing.")
        reward_count = _integer(payload_info.get("count"), "World reward Count", minimum=1)
        reward_notification = _boolean(payload_info.get("notification"), "World reward notification")
        evidence_count = _integer(payload_info.get("existing_instances_inspected"), "reward evidence count", minimum=1)
        if reward_count != 1 or reward_notification is not True:
            raise DefensiveGadgetCatalogError("Current gadget payload evidence no longer supports Count=1 and notification=true.")

        raw_grants = document.get("confirmed_npc_grants")
        if not isinstance(raw_grants, list):
            raise DefensiveGadgetCatalogError("Defensive Gadget catalog needs a confirmed_npc_grants array.")
        grants: list[DefensiveGadgetGrantSlot] = []
        for raw in raw_grants:
            if not isinstance(raw, dict):
                raise DefensiveGadgetCatalogError("Confirmed grant must be an object.")
            hashes = raw.get("source_sha256")
            if not isinstance(hashes, dict) or set(hashes) != {".uasset", ".uexp"} or any(
                not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in hashes.values()
            ):
                raise DefensiveGadgetCatalogError("Confirmed grant source package fingerprints are incomplete.")
            grant = DefensiveGadgetGrantSlot(
                slot_id=_string(raw.get("slot_id"), "grant slot id"),
                family=_string(raw.get("family"), "grant family"),
                content_class=_string(raw.get("content_class"), "grant content class").upper(),
                package_path=_quest_package(raw.get("package_path")),
                export_name=_string(raw.get("export_name"), "grant export name"),
                export_index=_integer(raw.get("export_index"), "grant export index", minimum=1),
                bulk_index=_integer(raw.get("bulk_index"), "grant ArticleBulkInfo index", minimum=0),
                reward_index=_integer(raw.get("reward_index"), "grant reward index", minimum=0),
                property_path=_string(raw.get("property_path"), "grant property path"),
                original_asset_path=_package(raw.get("original_asset_path")),
                count=_integer(raw.get("count"), "grant Count", minimum=1),
                notification=_boolean(raw.get("notification"), "grant notification"),
                source_sha256={key: str(value).casefold() for key, value in hashes.items()},
            )
            if grant.content_class not in _CONTENT_COUNTS or grant.count != 1 or not grant.notification:
                raise DefensiveGadgetCatalogError(f"Invalid or unsafe grant fields in {grant.slot_id}.")
            if grant.original_asset_path.casefold() not in paths:
                raise DefensiveGadgetCatalogError(f"Vanilla grant {grant.slot_id} is not a canonical catalog item.")
            grants.append(grant)
        if len({grant.slot_id.casefold() for grant in grants}) != len(grants):
            raise DefensiveGadgetCatalogError("Confirmed grant slot IDs must be unique.")

        raw_pending = document.get("pending_npc_grants")
        if not isinstance(raw_pending, list):
            raise DefensiveGadgetCatalogError("Defensive Gadget catalog needs a pending_npc_grants array.")
        pending: list[PendingDefensiveGadgetGrant] = []
        for raw in raw_pending:
            if not isinstance(raw, dict):
                raise DefensiveGadgetCatalogError("Pending grant must be an object.")
            pending.append(PendingDefensiveGadgetGrant(
                slot_id=_string(raw.get("slot_id"), "pending grant id"),
                family=_string(raw.get("family"), "pending grant family"),
                content_class=_string(raw.get("content_class"), "pending grant content class").upper(),
                source_description=_string(raw.get("source_description"), "pending grant source"),
                status=_string(raw.get("status"), "pending grant status"),
            ))
        if len({grant.slot_id.casefold() for grant in pending}) != len(pending):
            raise DefensiveGadgetCatalogError("Pending grant slot IDs must be unique.")
        if {grant.slot_id.casefold() for grant in grants} & {grant.slot_id.casefold() for grant in pending}:
            raise DefensiveGadgetCatalogError("Confirmed and pending grant slot IDs must be distinct.")
        if any(grant.content_class not in _CONTENT_COUNTS for grant in pending):
            raise DefensiveGadgetCatalogError("Pending grant content class is invalid.")
        return DefensiveGadgetCatalog(
            revision, tuple(items), tuple(grants), tuple(pending), reward_count,
            reward_notification, evidence_count,
        )


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DefensiveGadgetCatalogError(f"{label} must be a non-empty string.")
    return value


# Parse string array
def _string_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise DefensiveGadgetCatalogError(f"{label} must be an array of non-empty strings.")
    return tuple(value)


# Parse a boolean value
def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise DefensiveGadgetCatalogError(f"{label} must be a boolean.")
    return value


# Parse an integer value
def _integer(value: Any, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise DefensiveGadgetCatalogError(f"{label} must be an integer >= {minimum}.")
    return value


# Resolve a package asset path
def _package(value: Any) -> str:
    package = _string(value, "asset package path")
    if not package.startswith(_GADGET_PREFIX) or "\\" in package or package.endswith("/"):
        raise DefensiveGadgetCatalogError(f"Invalid Defensive Gadget package path {package!r}.")
    if any(part in {"", ".", ".."} for part in package.split("/")[1:]):
        raise DefensiveGadgetCatalogError(f"Unsafe Defensive Gadget package path {package!r}.")
    return package


# List quest package
def _quest_package(value: Any) -> str:
    package = _string(value, "quest package path")
    if not package.startswith("/Game/Quest/") or "\\" in package or package.endswith("/"):
        raise DefensiveGadgetCatalogError(f"Invalid Defensive Gadget quest package {package!r}.")
    if any(part in {"", ".", ".."} for part in package.split("/")[1:]):
        raise DefensiveGadgetCatalogError(f"Unsafe Defensive Gadget quest package {package!r}.")
    return package
