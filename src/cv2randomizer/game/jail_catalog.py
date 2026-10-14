"""Canonical player Jail rewards and their researched vanilla source metadata."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.catalogs import (
    SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS,
    WorldLootLocation,
)
from cv2randomizer.game.content_availability import PackageInventory


class JailCatalogError(ValueError):
    """Raised when the Jail catalog is incomplete, stale, or unsafe to use."""


@dataclass(frozen=True, slots=True)
class JailVanillaSource:
    source_type: str
    movable_world_loot: bool
    package_path: str | None = None
    source_file: str | None = None
    export_index: int | None = None
    export_name: str | None = None
    reward_index: int | None = None
    count: int | None = None
    notification: bool | None = None
    save_id: str | None = None
    protected: bool | None = None
    protection_reasons: tuple[str, ...] = ()
    scripted_reward_mode: str | None = None
    asset_path: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class JailReward:
    object: str
    asset_path: str
    content_class: str
    aliases: tuple[str, ...]
    world_reward_eligible: bool
    vanilla_source: JailVanillaSource


@dataclass(frozen=True, slots=True)
class JailCatalog:
    revision: str
    items: tuple[JailReward, ...]

    # Resolve the requested asset
    def resolve(self, value: str) -> JailReward | None:
        normalized = value.casefold().rstrip("/")
        basename = normalized.rsplit("/", 1)[-1]
        matches = tuple(
            item for item in self.items
            if normalized in {
                item.object.casefold(), item.asset_path.casefold(),
                *(alias.casefold() for alias in item.aliases),
            }
            or basename in {
                item.object.casefold(),
                *(alias.casefold() for alias in item.aliases),
            }
        )
        return matches[0] if len(matches) == 1 else None

    # Count base count
    @property
    def base_count(self) -> int:
        return sum(item.content_class == "BASE" for item in self.items)

    # Count DLC count
    @property
    def dlc_count(self) -> int:
        return sum(item.content_class == "DLC" for item in self.items)

    # Count world source count
    @property
    def world_source_count(self) -> int:
        return sum(item.vanilla_source.source_type != "INITIAL_INVENTORY" for item in self.items)

    # Count movable world source count
    @property
    def movable_world_source_count(self) -> int:
        return sum(item.vanilla_source.movable_world_loot for item in self.items)

    # List protected world sources
    @property
    def protected_world_sources(self) -> tuple[JailReward, ...]:
        return tuple(
            item for item in self.items
            if item.vanilla_source.source_type != "INITIAL_INVENTORY"
            and item.vanilla_source.protected is True
        )

    # List available items
    def available_items(self, inventory: PackageInventory) -> tuple[JailReward, ...]:
        """Keep only Jail identities and source occurrences present in this build."""
        return tuple(
            item for item in self.items
            if inventory.contains(item.asset_path)
            and (
                item.vanilla_source.source_type == "INITIAL_INVENTORY"
                or (
                    item.vanilla_source.package_path is not None
                    and inventory.contains(item.vanilla_source.package_path)
                )
            )
        )

    # Validate world sources
    def validate_world_sources(self, locations: tuple[WorldLootLocation, ...]) -> None:
        """Bind researched Jail slots to the authoritative current World Loot catalog."""
        by_slot: dict[tuple[str, int], list[WorldLootLocation]] = {}
        for location in locations:
            by_slot.setdefault((location.package_path.casefold(), location.export_index), []).append(location)

        # A catalog source identifies one logical Jail occurrence. Map variants
        # explicitly marked as mirrors serialize that same occurrence and do
        # not count as additional sources. Any unlinked copy of a unique Jail
        # must fail closed instead of silently entering the permutation twice.
        canonical_occurrences: dict[str, list[tuple[WorldLootLocation, int]]] = {
            item.asset_path.casefold(): [] for item in self.items
        }
        for location in locations:
            if location.mirror_of_package_path is not None:
                continue
            for reward_index, reward in enumerate(location.rewards):
                matches = canonical_occurrences.get(reward.article_path.casefold())
                if matches is not None:
                    matches.append((location, reward_index))

        expected_world_occurrences = {
            item.asset_path.casefold(): (
                None if item.vanilla_source.source_type == "INITIAL_INVENTORY"
                else (
                    item.vanilla_source.package_path.casefold(),
                    item.vanilla_source.export_index,
                    item.vanilla_source.reward_index,
                )
            )
            for item in self.items
        }
        for item in self.items:
            found = canonical_occurrences[item.asset_path.casefold()]
            expected = expected_world_occurrences[item.asset_path.casefold()]
            expected_count = 0 if expected is None else 1
            if len(found) != expected_count:
                details = ", ".join(
                    f"{location.package_path}#{location.export_index}[{reward_index}]"
                    for location, reward_index in found
                ) or "none"
                raise JailCatalogError(
                    f"Unique Jail {item.object} must have exactly {expected_count} canonical World Loot source(s); "
                    f"found {len(found)}: {details}. Linked map mirrors are not additional sources."
                )
            if expected is not None:
                actual = found[0]
                actual_slot = (
                    actual[0].package_path.casefold(),
                    actual[0].export_index,
                    actual[1],
                )
                if actual_slot != expected:
                    raise JailCatalogError(
                        f"The unique Jail {item.object} occurs outside its audited World Loot source slot."
                    )

        # A mirror is valid only when it points directly to the canonical
        # source and carries the same Jail payload at the same reward index.
        for location in locations:
            if location.mirror_of_package_path is None:
                continue
            for reward_index, reward in enumerate(location.rewards):
                item = next(
                    (entry for entry in self.items if entry.asset_path.casefold() == reward.article_path.casefold()),
                    None,
                )
                if item is None:
                    continue
                source = item.vanilla_source
                expected_mirror_source = (
                    None if source.source_type == "INITIAL_INVENTORY" else (
                        source.package_path.casefold(), source.export_index,
                    )
                )
                actual_mirror_source = (
                    location.mirror_of_package_path.casefold(),
                    location.mirror_of_export_index,
                )
                if (
                    expected_mirror_source is None
                    or actual_mirror_source != expected_mirror_source
                ):
                    raise JailCatalogError(
                        f"World Loot Jail mirror for {item.object} does not point to its audited canonical source."
                    )
                canonical = by_slot.get(expected_mirror_source, [])
                if len(canonical) != 1 or reward_index >= len(canonical[0].rewards):
                    raise JailCatalogError(
                        f"World Loot Jail mirror for {item.object} has no matching canonical reward payload."
                    )
                canonical_reward = canonical[0].rewards[reward_index]
                if (
                    canonical_reward.article_path.casefold() != reward.article_path.casefold()
                    or canonical_reward.count != reward.count
                    or canonical_reward.notification != reward.notification
                ):
                    raise JailCatalogError(
                        f"World Loot Jail mirror for {item.object} differs from its canonical reward payload."
                    )

        for item in self.items:
            source = item.vanilla_source
            if source.source_type == "INITIAL_INVENTORY":
                continue
            assert source.package_path is not None
            assert source.export_index is not None
            slot = (source.package_path.casefold(), source.export_index)
            matches = by_slot.get(slot, [])
            if len(matches) != 1:
                raise JailCatalogError(
                    f"Vanilla Jail source {item.object} must match exactly one current World Loot slot."
                )
            location = matches[0]
            assert source.source_file is not None
            assert source.source_type
            assert source.reward_index is not None
            if location.source_file.casefold() != source.source_file.casefold():
                raise JailCatalogError(f"Vanilla Jail source filename changed for {item.object}.")
            if location.export_name.casefold() != source.export_name.casefold():
                raise JailCatalogError(f"Vanilla Jail export identity changed for {item.object}.")
            if location.save_id is None or location.save_id.casefold() != (source.save_id or "").casefold():
                raise JailCatalogError(f"Vanilla Jail UniqueSaveID changed for {item.object}.")
            if location.actor_category != source.source_type:
                raise JailCatalogError(f"Vanilla Jail source category changed for {item.object}.")
            if source.reward_index >= len(location.rewards):
                raise JailCatalogError(f"Vanilla Jail reward index is out of range for {item.object}.")
            reward = location.rewards[source.reward_index]
            if reward.article_path.casefold() != item.asset_path.casefold():
                raise JailCatalogError(f"Vanilla Jail reward path changed for {item.object}.")
            if (reward.count, reward.notification) != (source.count, source.notification):
                raise JailCatalogError(f"Vanilla Jail payload evidence changed for {item.object}.")
            if location.protected != source.protected:
                raise JailCatalogError(
                    f"Protection metadata for {item.object} disagrees with the authoritative World Loot catalog."
                )
            if source.movable_world_loot == location.protected:
                raise JailCatalogError(f"Movability metadata for {item.object} disagrees with World Loot protection.")
            if tuple(value.casefold() for value in location.protection_reasons) != tuple(
                value.casefold() for value in source.protection_reasons
            ):
                raise JailCatalogError(f"Protection reasons changed for vanilla Jail source {item.object}.")
            if location.scripted_reward_mode != source.scripted_reward_mode:
                raise JailCatalogError(f"Scripted reward handling changed for vanilla Jail source {item.object}.")


class JailCatalogStore:
    """Load and structurally validate the metadata-only player Jail catalog."""

    # Initialize this object
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_path("jails", "current")

    # Load and validate catalog data
    def load(self) -> JailCatalog:
        path = self.data_dir / "jails.json"
        try:
            document: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise JailCatalogError(f"Could not read Jail catalog: {error}") from error
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise JailCatalogError("Jail catalog schema version is unknown.")
        if document.get("catalog") != "jails":
            raise JailCatalogError("Jail catalog identifier is invalid.")
        revision = _string(document.get("revision"), "Jail catalog revision")
        expected_counts = document.get("expected_counts")
        if expected_counts != {"total": 12, "BASE": 6, "DLC": 6}:
            raise JailCatalogError("Jail catalog must declare 12 entries (6 BASE / 6 DLC).")
        raw_items = document.get("items")
        if not isinstance(raw_items, list):
            raise JailCatalogError("Jail catalog items must be an array.")

        items: list[JailReward] = []
        objects: set[str] = set()
        paths: set[str] = set()
        aliases: set[str] = set()
        source_slots: set[tuple[str, int]] = set()
        save_ids: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise JailCatalogError("Each Jail catalog item must be an object.")
            item = JailReward(
                object=_string(raw.get("object"), "Jail object"),
                asset_path=_asset_path(raw.get("asset_path")),
                content_class=_string(raw.get("content_class"), "Jail content class").upper(),
                aliases=_string_array(raw.get("aliases"), "Jail aliases"),
                world_reward_eligible=_boolean(raw.get("world_reward_eligible"), "Jail world eligibility"),
                vanilla_source=_parse_source(
                    raw.get("vanilla_source"),
                    item_object=_string(raw.get("object"), "Jail object"),
                ),
            )
            key = item.object.casefold()
            path_key = item.asset_path.casefold()
            if not item.object.startswith("DA_Jail_") or item.object.startswith("DA_Buddy_Jail_"):
                raise JailCatalogError(f"Non-player Jail asset {item.object!r} cannot enter the catalog.")
            if item.object == "DA_Jail_Death":
                raise JailCatalogError("Death is a UI label for DA_Jail_Reaper, not a separate Jail asset.")
            if key in objects or path_key in paths:
                raise JailCatalogError("Jail object names and canonical asset paths must be unique.")
            if item.asset_path.rsplit("/", 1)[-1].casefold() != key:
                raise JailCatalogError(f"Canonical Jail path does not match {item.object}.")
            if item.content_class not in {"BASE", "DLC"}:
                raise JailCatalogError(f"Invalid content class for {item.object}.")
            expected_parent = (
                "/Game/Blueprint/Article/Jail/DLC" if item.content_class == "DLC"
                else "/Game/Blueprint/Article/Jail"
            )
            if item.asset_path.rsplit("/", 1)[0] != expected_parent:
                raise JailCatalogError(f"Canonical path does not match the content class for {item.object}.")
            if not item.world_reward_eligible:
                raise JailCatalogError(f"All catalogued player Jails must be World Loot eligible: {item.object}.")
            objects.add(key)
            paths.add(path_key)
            for alias in item.aliases:
                alias_key = alias.casefold()
                if alias_key in aliases or alias_key in objects or not alias.startswith("DA_Jail_"):
                    raise JailCatalogError(f"Duplicate or invalid Jail alias {alias!r}.")
                aliases.add(alias_key)
            source = item.vanilla_source
            if source.source_type != "INITIAL_INVENTORY":
                assert source.package_path is not None and source.export_index is not None
                slot = (source.package_path.casefold(), source.export_index)
                if slot in source_slots:
                    raise JailCatalogError("Vanilla Jail World Loot slots must be unique.")
                source_slots.add(slot)
                assert source.save_id is not None
                if source.save_id.casefold() in save_ids:
                    raise JailCatalogError("Vanilla Jail save IDs must be unique.")
                save_ids.add(source.save_id.casefold())
            items.append(item)

        counts = Counter(item.content_class for item in items)
        if len(items) != 12 or dict(counts) != {"BASE": 6, "DLC": 6}:
            raise JailCatalogError(f"Expected 12 Jails (6 BASE / 6 DLC), got {dict(counts)}.")
        if aliases & objects:
            raise JailCatalogError("Jail aliases must not collide with canonical object names.")
        if "da_jail_reaper" not in objects or "da_jail_death" in objects:
            raise JailCatalogError("The canonical Reaper Jail must exist and DA_Jail_Death must not.")
        world_sources = [item for item in items if item.vanilla_source.source_type != "INITIAL_INVENTORY"]
        starting_sources = [item for item in items if item.vanilla_source.source_type == "INITIAL_INVENTORY"]
        if len(world_sources) != 11 or len(starting_sources) != 1:
            raise JailCatalogError("Jail sources must include exactly 11 World Loot slots and one Starting Jail.")
        if starting_sources[0].object != "DA_Jail_Ogre":
            raise JailCatalogError("DA_Jail_Ogre must remain the sole Initial Inventory Jail source.")
        future = document.get("future")
        starting = future.get("starting_jail_randomizer") if isinstance(future, dict) else None
        if not isinstance(starting, dict) or starting.get("implemented_in_this_phase") is not True:
            raise JailCatalogError("Starting Jail randomization must be declared implemented in the current phase.")
        return JailCatalog(revision, tuple(sorted(items, key=lambda item: item.asset_path.casefold())))


# Parse source
def _parse_source(value: Any, *, item_object: str) -> JailVanillaSource:
    if not isinstance(value, dict):
        raise JailCatalogError("Jail vanilla_source must be an object.")
    source_type = _string(value.get("type"), "Jail source type")
    movable = _boolean(value.get("movable_world_loot"), "Jail source movability")
    if source_type == "INITIAL_INVENTORY":
        if movable or value.get("asset_path") != "/Game/Blueprint/Article/DA_InitialInventory_Player":
            raise JailCatalogError("Initial Inventory source metadata is invalid.")
        return JailVanillaSource(
            source_type=source_type,
            movable_world_loot=False,
            asset_path=_game_package_path(value.get("asset_path")),
            note=_string(value.get("note"), "Initial Inventory source note"),
        )
    if source_type not in {"GROUND_PICKUP", "TREASURE_BOX", "WORLD_LOOT_UNRESOLVED"}:
        raise JailCatalogError(f"Unsupported vanilla Jail source type {source_type!r}.")
    protected = _boolean(value.get("protected"), "Jail source protection")
    reasons = _string_array(value.get("protection_reasons"), "Jail source protection reasons")
    scripted_reward_mode_raw = value.get("scripted_reward_mode")
    scripted_reward_mode = (
        _string(scripted_reward_mode_raw, "Jail scripted reward mode")
        if scripted_reward_mode_raw is not None else None
    )
    if movable == protected or protected != bool(reasons):
        raise JailCatalogError("Jail movable/protected source metadata is inconsistent.")
    if movable and source_type not in {"GROUND_PICKUP", "TREASURE_BOX"}:
        raise JailCatalogError("Only supported World Loot categories can be movable Jail sources.")
    if not movable and not protected:
        raise JailCatalogError("A non-movable Jail World Loot source must remain protected.")
    package_path = _string(value.get("package_path"), "Jail source package")
    if not package_path.startswith("/Game/Map/MainWorld/MainWorld/_Generated_/") or "\\" in package_path:
        raise JailCatalogError("Jail source package is outside the generated map tree.")
    source_file = _string(value.get("source_file"), "Jail source file")
    if not source_file.endswith(".umap") or "/" in source_file or "\\" in source_file:
        raise JailCatalogError("Jail source file must be a map filename.")
    export_index = _integer(value.get("export_index"), "Jail source export index", minimum=1)
    export_name = _string(value.get("export_name"), "Jail source export name")
    reward_index = _integer(value.get("reward_index"), "Jail source reward index", minimum=0)
    count = _integer(value.get("count"), "Jail source Count", minimum=1)
    notification = _boolean(value.get("notification"), "Jail source notification")
    save_id = _string(value.get("save_id"), "Jail source UniqueSaveID")
    if count != 1 or notification is not True:
        raise JailCatalogError("Vanilla Jail sources must confirm Count=1 and bNotification=true.")
    if scripted_reward_mode is not None:
        if (
            scripted_reward_mode != SCRIPTED_REWARD_MODE_ARTICLE_ONLY_PRESERVE_FLAGS
            or item_object != "DA_Jail_Stinger"
            or not movable
            or protected
            or reasons
            or source_type != "GROUND_PICKUP"
            or package_path != "/Game/Map/MainWorld/MainWorld/_Generated_/61Y5LAV87NWVD213F35X2VK87"
            or source_file != "61Y5LAV87NWVD213F35X2VK87.umap"
            or export_index != 272
            or export_name != "BP_PickupItem_WithFlagChange_C_UAID_A8A159A4A14B9D8902_1822678819_3eb3acb6c4c82637"
            or reward_index != 0
            or save_id != "5500DB0E-438B-2632-00A8-A5B994AAFAA2"
        ):
            raise JailCatalogError("Scripted reward-only handling must match the researched Sunken City Stinger source.")
    return JailVanillaSource(
        source_type=source_type,
        movable_world_loot=movable,
        package_path=package_path,
        source_file=source_file,
        export_index=export_index,
        export_name=export_name,
        reward_index=reward_index,
        count=count,
        notification=notification,
        save_id=save_id,
        protected=protected,
        protection_reasons=reasons,
        scripted_reward_mode=scripted_reward_mode,
    )


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JailCatalogError(f"{label} must be a non-empty string.")
    return value


# Parse string array
def _string_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise JailCatalogError(f"{label} must be an array of non-empty strings.")
    return tuple(value)


# Parse a boolean value
def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise JailCatalogError(f"{label} must be a boolean.")
    return value


# Parse an integer value
def _integer(value: Any, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise JailCatalogError(f"{label} must be an integer >= {minimum}.")
    return value


# Resolve asset path
def _asset_path(value: Any) -> str:
    path = _string(value, "Jail asset path")
    if (
        not path.startswith("/Game/Blueprint/Article/Jail/")
        or "\\" in path
        or path.endswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/")[1:])
    ):
        raise JailCatalogError(f"Invalid canonical Jail asset path {path!r}.")
    return path


# Resolve game package path
def _game_package_path(value: Any) -> str:
    path = _string(value, "game asset path")
    if (
        not path.startswith("/Game/")
        or "\\" in path
        or path.endswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/")[1:])
    ):
        raise JailCatalogError(f"Invalid game asset path {path!r}.")
    return path
