"""Canonical metadata and source validation for player Bequeathed Formae."""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from cv2randomizer.core.resources import application_data_path
from cv2randomizer.game.catalogs import ShopProduct, WorldLootLocation
from cv2randomizer.game.content_availability import PackageInventory


_EXPECTED_COUNTS = {"total": 10, "BASE": 8, "DLC": 2, "WORLD": 1, "SHOP": 7, "DIRECT_QUEST": 2}
_SOURCE_COUNTS = {"WORLD": 1, "SHOP": 7, "DIRECT_QUEST": 2}
_OBJECT = re.compile(r"DA_[A-Za-z0-9_]+\Z")
_GROUP = re.compile(r"BEQUEATHED_[A-Z0-9_]+\Z")


class BequeathedFormaCatalogError(ValueError):
    """Raised when Forma research metadata is invalid or out of date."""


@dataclass(frozen=True, slots=True)
class FormaWorldSource:
    actor_category: str
    actor_class: str
    package_path: str
    export_index: int
    reward_index: int
    count: int
    notification: bool
    save_id: str
    protected: bool


@dataclass(frozen=True, slots=True)
class FormaShopSource:
    family: str
    shop_row: str
    product_index: int
    price: int
    stock: int
    uses_stock: bool
    protected: bool


@dataclass(frozen=True, slots=True)
class FormaDirectGrant:
    quest_asset: str
    relative_file: str
    ui_export_number: int
    json_export_index: int
    count: int
    notification: bool

    # Export index
    @property
    def export_index(self) -> int:
        """Return the one-based export index expected by UAssetAPI."""
        return self.json_export_index + 1


@dataclass(frozen=True, slots=True)
class BequeathedForma:
    object: str
    game_name: str
    asset_path: str
    content_class: str
    source_class: str
    world_reward_eligible: bool
    shop_reward_eligible: bool
    world_source: FormaWorldSource | None = None
    shop_source: FormaShopSource | None = None
    logical_reward_group: str | None = None
    direct_grants: tuple[FormaDirectGrant, ...] = ()
    world_loot_destination_id: str | None = None


@dataclass(frozen=True, slots=True)
class BequeathedFormaCatalog:
    revision: str
    items: tuple[BequeathedForma, ...]

    # List available items
    def available_items(self, inventory: PackageInventory) -> tuple[BequeathedForma, ...]:
        """Keep installed Forma identities and whichever direct grant routes exist."""
        available: list[BequeathedForma] = []
        for item in self.items:
            if not inventory.contains(item.asset_path):
                continue
            if item.source_class != "DIRECT_QUEST":
                available.append(item)
                continue
            grants = tuple(
                grant for grant in item.direct_grants
                if inventory.contains(grant.quest_asset)
            )
            if grants:
                available.append(replace(item, direct_grants=grants))
        return tuple(available)

    # List world sources
    @property
    def world_sources(self) -> tuple[BequeathedForma, ...]:
        return tuple(item for item in self.items if item.source_class == "WORLD")

    # List shop sources
    @property
    def shop_sources(self) -> tuple[BequeathedForma, ...]:
        return tuple(item for item in self.items if item.source_class == "SHOP")

    # List direct rewards
    @property
    def direct_rewards(self) -> tuple[BequeathedForma, ...]:
        return tuple(item for item in self.items if item.source_class == "DIRECT_QUEST")

    # Separate direct rewards
    @property
    def separate_direct_rewards(self) -> tuple[BequeathedForma, ...]:
        """Direct rewards still handled by the isolated Forma-only pool."""
        return tuple(
            item for item in self.direct_rewards
            if item.world_loot_destination_id is None
        )

    # List world loot direct rewards
    @property
    def world_loot_direct_rewards(self) -> tuple[BequeathedForma, ...]:
        """Direct quest grants explicitly moved into the shared World Loot pool."""
        return tuple(
            item for item in self.direct_rewards
            if item.world_loot_destination_id is not None
        )

    # Count direct physical grant count
    @property
    def direct_physical_grant_count(self) -> int:
        return sum(len(item.direct_grants) for item in self.direct_rewards)

    # Index records by object
    def by_object(self, object_name: str) -> BequeathedForma | None:
        return next((item for item in self.items if item.object.casefold() == object_name.casefold()), None)

    # Validate sources
    def validate_sources(
        self,
        world_locations: tuple[WorldLootLocation, ...],
        shop_products: tuple[ShopProduct, ...],
    ) -> None:
        """Ensure canonical vanilla World/Shop entries still match authoritative catalogs."""
        world_by_slot: dict[tuple[str, int], list[WorldLootLocation]] = {}
        for location in world_locations:
            world_by_slot.setdefault((location.package_path.casefold(), location.export_index), []).append(location)
        for item in self.world_sources:
            source = item.world_source
            assert source is not None
            occurrences = [
                (location.package_path.casefold(), location.export_index, reward_index)
                for location in world_locations
                if location.mirror_of_package_path is None
                for reward_index, reward in enumerate(location.rewards)
                if reward.article_path.casefold() == item.asset_path.casefold()
            ]
            if occurrences != [(source.package_path.casefold(), source.export_index, source.reward_index)]:
                raise BequeathedFormaCatalogError(
                    f"World catalog must contain exactly the researched vanilla source for {item.object}."
                )
            slot = (source.package_path.casefold(), source.export_index)
            matches = world_by_slot.get(slot, [])
            if len(matches) != 1:
                raise BequeathedFormaCatalogError(
                    f"World source for {item.object} must match exactly one current World Loot location."
                )
            location = matches[0]
            if (
                location.actor_category.casefold() != source.actor_category.casefold()
                or location.actor_class.casefold() != source.actor_class.casefold()
                or location.save_id is None
                or location.save_id.casefold() != source.save_id.casefold()
                or location.protected != source.protected
            ):
                raise BequeathedFormaCatalogError(f"World source identity or protection changed for {item.object}.")
            if source.reward_index >= len(location.rewards):
                raise BequeathedFormaCatalogError(f"World reward index is out of range for {item.object}.")
            reward = location.rewards[source.reward_index]
            if (
                reward.article_path.casefold() != item.asset_path.casefold()
                or reward.count != source.count
                or reward.notification is not source.notification
            ):
                raise BequeathedFormaCatalogError(f"World reward payload changed for {item.object}.")

        for item in self.shop_sources:
            source = item.shop_source
            assert source is not None
            occurrences = [
                product for product in shop_products
                if product.original_product_path.casefold() == item.asset_path.casefold()
            ]
            if len(occurrences) != 1:
                raise BequeathedFormaCatalogError(
                    f"Shop catalog must contain exactly the researched vanilla source for {item.object}."
                )
            matches = [
                product for product in shop_products
                if product.family.casefold() == source.family.casefold()
                and product.shop_row.casefold() == source.shop_row.casefold()
                and product.product_index == source.product_index
            ]
            if len(matches) != 1:
                raise BequeathedFormaCatalogError(
                    f"Shop source for {item.object} must match exactly one current Shop product."
                )
            product = matches[0]
            if (
                product.original_product_path.casefold() != item.asset_path.casefold()
                or product.price != source.price
                or product.stock != source.stock
                or product.uses_stock is not source.uses_stock
                or product.protected is not source.protected
            ):
                raise BequeathedFormaCatalogError(f"Shop source payload changed for {item.object}.")

        for item in self.world_loot_direct_rewards:
            if not item.direct_grants or item.world_loot_destination_id is None:
                raise BequeathedFormaCatalogError(
                    f"World Loot direct Forma {item.object} must identify a primary grant and any alternative grant paths."
                )
            grant = item.direct_grants[0]
            matches = [
                location for location in world_locations
                if location.destination_id == item.world_loot_destination_id
            ]
            if len(matches) != 1:
                raise BequeathedFormaCatalogError(
                    f"World Loot catalog must contain exactly one destination for {item.object}."
                )
            location = matches[0]
            layout = location.quest_article_layout
            if (
                location.destination_kind != "QUEST_ARTICLE_GRANT"
                or location.actor_category != "DIRECT_ARTICLE_GRANT"
                or location.package_path.casefold() != grant.quest_asset.casefold()
                or location.source_file.casefold() != grant.relative_file.casefold()
                or location.export_index != grant.export_index
                or layout is None
                or len(location.rewards) != 1
                or location.rewards[0].article_path.casefold() != item.asset_path.casefold()
                or location.rewards[0].count != grant.count
                or location.rewards[0].notification is not grant.notification
                or layout.expected_article_path.casefold() != item.asset_path.casefold()
                or layout.count != grant.count
                or layout.notification is not grant.notification
            ):
                raise BequeathedFormaCatalogError(
                    f"World Loot destination identity or reward payload changed for {item.object}."
                )


class BequeathedFormaCatalogStore:
    """Load the supplied metadata catalog without bundling any game assets."""

    # Initialize this object
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or application_data_path("bFormae", "current")

    # Load and validate catalog data
    def load(self) -> BequeathedFormaCatalog:
        path = self.data_dir / "bequeathed_formae.json"
        try:
            document: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BequeathedFormaCatalogError(f"Could not read Bequeathed Forma catalog: {error}") from error
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise BequeathedFormaCatalogError("Bequeathed Forma catalog schema version is unknown.")
        if document.get("catalog") != "bequeathed_formae":
            raise BequeathedFormaCatalogError("Bequeathed Forma catalog identifier is invalid.")
        revision = _string(document.get("revision"), "catalog revision")
        if document.get("expected_counts") != _EXPECTED_COUNTS:
            raise BequeathedFormaCatalogError("Bequeathed Forma catalog expected counts are invalid.")
        raw_items = document.get("items")
        if not isinstance(raw_items, list):
            raise BequeathedFormaCatalogError("Bequeathed Forma items must be an array.")

        items: list[BequeathedForma] = []
        objects: set[str] = set()
        paths: set[str] = set()
        grant_slots: set[tuple[str, int]] = set()
        groups: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise BequeathedFormaCatalogError("Each Bequeathed Forma must be an object.")
            object_name = _string(raw.get("object"), "object")
            asset_path = _asset_path(raw.get("asset_path"))
            content_class = _string(raw.get("content_class"), "content_class").upper()
            source_class = _string(raw.get("source_class"), "source_class").upper()
            if not _OBJECT.fullmatch(object_name) or object_name.startswith("DA_Buddy_"):
                raise BequeathedFormaCatalogError(f"Non-player or malformed Forma {object_name!r} cannot enter the catalog.")
            if asset_path.rsplit("/", 1)[-1].casefold() != object_name.casefold():
                raise BequeathedFormaCatalogError(f"Canonical path does not match {object_name}.")
            if not asset_path.startswith("/Game/Blueprint/Article/Gadget/"):
                raise BequeathedFormaCatalogError(f"{object_name} is outside the player Gadget asset tree.")
            if content_class not in {"BASE", "DLC"}:
                raise BequeathedFormaCatalogError(f"Invalid content class for {object_name}.")
            if source_class not in _SOURCE_COUNTS:
                raise BequeathedFormaCatalogError(f"Invalid source class for {object_name}.")
            world_eligible = _boolean(raw.get("world_reward_eligible"), "world_reward_eligible")
            shop_eligible = _boolean(raw.get("shop_reward_eligible"), "shop_reward_eligible")
            if not world_eligible or not shop_eligible:
                raise BequeathedFormaCatalogError(f"All canonical Formae must retain future World/Shop eligibility: {object_name}.")
            source_raw = raw.get("vanilla_source")
            world_source: FormaWorldSource | None = None
            shop_source: FormaShopSource | None = None
            logical_group: str | None = None
            direct_grants: tuple[FormaDirectGrant, ...] = ()
            world_loot_destination_id: str | None = None
            if source_class == "WORLD":
                source_raw = _object(source_raw, "world vanilla_source")
                world_source = FormaWorldSource(
                    actor_category=_string(source_raw.get("type"), "world type"),
                    actor_class=_string(source_raw.get("actor_class"), "world actor_class"),
                    package_path=_package(source_raw.get("package_path"), "/Game/Map/"),
                    export_index=_integer(source_raw.get("export_index"), "world export_index", minimum=1),
                    reward_index=_integer(source_raw.get("reward_index"), "world reward_index", minimum=0),
                    count=_integer(source_raw.get("count"), "world count", minimum=1),
                    notification=_boolean(source_raw.get("notification"), "world notification"),
                    save_id=_uuid(source_raw.get("save_id")),
                    protected=_boolean(source_raw.get("protected"), "world protected"),
                )
                if world_source.protected:
                    raise BequeathedFormaCatalogError("Canonical Forma World source must be movable.")
            elif source_class == "SHOP":
                source_raw = _object(source_raw, "shop vanilla_source")
                shop_source = FormaShopSource(
                    family=_string(source_raw.get("family"), "shop family").upper(),
                    shop_row=_string(source_raw.get("shop_row"), "shop_row"),
                    product_index=_integer(source_raw.get("product_index"), "product_index", minimum=0),
                    price=_integer(source_raw.get("price"), "price", minimum=0),
                    stock=_integer(source_raw.get("stock"), "stock", minimum=0),
                    uses_stock=_boolean(source_raw.get("uses_stock"), "uses_stock"),
                    protected=_boolean(source_raw.get("protected"), "shop protected"),
                )
                if shop_source.protected:
                    raise BequeathedFormaCatalogError("Canonical Forma Shop source must be movable.")
            else:
                if source_raw is not None:
                    raise BequeathedFormaCatalogError(f"Direct reward {object_name} must not contain a vanilla_source object.")
                logical_group = _string(raw.get("logical_reward_group"), "logical_reward_group")
                raw_world_destination = raw.get("world_loot_destination_id")
                world_loot_destination_id = (
                    None if raw_world_destination is None
                    else _string(raw_world_destination, "world_loot_destination_id")
                )
                if not _GROUP.fullmatch(logical_group) or logical_group in groups:
                    raise BequeathedFormaCatalogError(f"Invalid or duplicate direct logical reward group {logical_group!r}.")
                groups.add(logical_group)
                raw_grants = raw.get("direct_grants")
                if not isinstance(raw_grants, list) or not raw_grants:
                    raise BequeathedFormaCatalogError(f"Direct reward {object_name} must have at least one physical grant.")
                parsed_grants: list[FormaDirectGrant] = []
                for grant_raw in raw_grants:
                    grant = _object(grant_raw, "direct grant")
                    quest_asset = _package(grant.get("quest_asset"), "/Game/Quest/")
                    relative_file = _relative_quest_file(grant.get("relative_file"))
                    expected_file = quest_asset.removeprefix("/Game/") + ".uasset"
                    if relative_file.casefold() != expected_file.casefold():
                        raise BequeathedFormaCatalogError(f"Quest asset and relative file disagree for {object_name}.")
                    ui_export_number = _integer(grant.get("ui_export_number"), "ui_export_number", minimum=1)
                    json_export_index = _integer(grant.get("json_export_index"), "json_export_index", minimum=0)
                    if ui_export_number != json_export_index + 1:
                        raise BequeathedFormaCatalogError(f"UI/JSON export numbering disagrees for {object_name}.")
                    grant_slot = (quest_asset.casefold(), json_export_index)
                    if grant_slot in grant_slots:
                        raise BequeathedFormaCatalogError("Direct physical quest grant locations must be unique.")
                    grant_slots.add(grant_slot)
                    parsed_grants.append(FormaDirectGrant(
                        quest_asset=quest_asset,
                        relative_file=relative_file,
                        ui_export_number=ui_export_number,
                        json_export_index=json_export_index,
                        count=_integer(grant.get("count"), "direct grant count", minimum=1),
                        notification=_boolean(grant.get("notification"), "direct grant notification"),
                    ))
                direct_grants = tuple(parsed_grants)

            key = object_name.casefold()
            path_key = asset_path.casefold()
            if key in objects or path_key in paths:
                raise BequeathedFormaCatalogError("Canonical object names and asset paths must be unique.")
            objects.add(key)
            paths.add(path_key)
            items.append(BequeathedForma(
                object_name,
                _string(raw.get("game_name"), "game_name"),
                asset_path,
                content_class,
                source_class,
                world_eligible,
                shop_eligible,
                world_source,
                shop_source,
                logical_group,
                direct_grants,
                world_loot_destination_id,
            ))

        classes = Counter(item.content_class for item in items)
        sources = Counter(item.source_class for item in items)
        if len(items) != 10 or dict(classes) != {"BASE": 8, "DLC": 2} or dict(sources) != _SOURCE_COUNTS:
            raise BequeathedFormaCatalogError(
                f"Bequeathed Forma inventory differs from V1 (content={dict(classes)}, sources={dict(sources)})."
            )
        direct_by_object = {
            item.object: item for item in items if item.source_class == "DIRECT_QUEST"
        }
        expected_direct_groups = {
            "DA_LouAxe": ("BEQUEATHED_LOU_AXE", 1),
            "DA_ExecutionSword": ("BEQUEATHED_EXECUTIONER", 2),
        }
        if set(direct_by_object) != set(expected_direct_groups) or len(groups) != 2:
            raise BequeathedFormaCatalogError(
                "Direct rewards must be Lou Axe and Executioner, represented by two logical groups."
            )
        for object_name, (logical_group, physical_count) in expected_direct_groups.items():
            item = direct_by_object[object_name]
            if item.logical_reward_group != logical_group or len(item.direct_grants) != physical_count:
                raise BequeathedFormaCatalogError(
                    f"{object_name} must map to {logical_group} with {physical_count} physical quest grant(s)."
                )
            expected_world_destination = {
                "DA_LouAxe": "bequeathed_lou_axe_magmell_quest",
                "DA_ExecutionSword": "bequeathed_executioner_lyle_quest",
            }[object_name]
            if item.world_loot_destination_id != expected_world_destination:
                raise BequeathedFormaCatalogError(
                    f"{object_name} has an unexpected direct reward pool assignment."
                )
            if any(grant.count != 1 or grant.notification is not True for grant in item.direct_grants):
                raise BequeathedFormaCatalogError(
                    f"{object_name} direct grants must retain Count=1 and bNotification=true."
                )
        if sum(len(item.direct_grants) for item in items) != 3:
            raise BequeathedFormaCatalogError("Exactly three physical direct quest grant locations are expected.")
        items.sort(key=lambda item: (item.source_class, item.object.casefold()))
        return BequeathedFormaCatalog(revision, tuple(items))


# Resolve asset path
def _asset_path(value: Any) -> str:
    return _package(value, "/Game/Blueprint/Article/Gadget/")


# Resolve a package asset path
def _package(value: Any, prefix: str) -> str:
    result = _string(value, "package path")
    if not result.startswith(prefix) or "\\" in result or result.endswith("/") or "." in result.rsplit("/", 1)[-1]:
        raise BequeathedFormaCatalogError(f"Invalid package path {result!r}.")
    if any(part in {".", ".."} for part in result.split("/")):
        raise BequeathedFormaCatalogError(f"Unsafe package path {result!r}.")
    return result


# Resolve the quest file path
def _relative_quest_file(value: Any) -> str:
    result = _string(value, "relative_file").replace("\\", "/")
    path = PurePosixPath(result)
    if path.is_absolute() or ".." in path.parts or path.suffix.casefold() != ".uasset" or not result.startswith("Quest/"):
        raise BequeathedFormaCatalogError(f"Invalid relative quest package path {result!r}.")
    return path.as_posix()


# Parse a UUID value
def _uuid(value: Any) -> str:
    result = _string(value, "save_id")
    try:
        return str(uuid.UUID(result))
    except ValueError as error:
        raise BequeathedFormaCatalogError("World source save_id must be a UUID.") from error


# Validate a text value
def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BequeathedFormaCatalogError(f"{label} must be a non-empty string.")
    return value


# Read a JSON object
def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BequeathedFormaCatalogError(f"{label} must be an object.")
    return value


# Parse an integer value
def _integer(value: Any, label: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise BequeathedFormaCatalogError(f"{label} must be an integer >= {minimum}.")
    return value


# Parse a boolean value
def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise BequeathedFormaCatalogError(f"{label} must be a boolean.")
    return value
