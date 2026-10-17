"""Field-preserving binary patcher for cooked shop DataTables."""

from __future__ import annotations

import re
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..game.shop_tables import ShopReplenishGroup, ShopTableParseResult, ShopTableProduct
from ..randomizer.shops import ArticleRewardIdentity, ShopAssignment, ShopSlot, shop_slot_lock_reason


class ShopPatchError(ValueError):
    """Raised when a shop table patch would violate a verified invariant."""


_BUDDY_ROW = re.compile(r"Character\.Type\.Buddy\.([A-Za-z0-9_]+)\Z", re.IGNORECASE)
_BUDDY_AFFINITY_FORMULA = re.compile(
    r"Friendship\.([A-Za-z0-9_]+)\.Present \+ "
    r"Friendship\.([A-Za-z0-9_]+)\.Adventure \+ "
    r"Friendship\.([A-Za-z0-9_]+)\.Episode >=\s*([0-9]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ShopTablePatchResult:
    """Patched export and its successfully reparsed form."""

    data: bytes
    parsed: ShopTableParseResult
    changed_slots: tuple[ShopSlot, ...]


# List slot
def _slot(product: ShopTableProduct) -> ShopSlot:
    return (product.family.upper(), product.shop_row, product.product_index)


# List compatible families
def _compatible_families(destination: str, source: str) -> bool:
    allowed = {
        "NORMAL": {"NORMAL", "DLC"},
        "DLC": {"NORMAL", "DLC"},
        "TRADE": {"TRADE", "TRADE_DLC"},
        "TRADE_DLC": {"TRADE", "TRADE_DLC"},
        "LUXURY": {"LUXURY"},
    }
    return source in allowed.get(destination, set())


# Verify bytes
def _verify_bytes(data: bytes, offset: int | None, expected: bytes | None, label: str) -> None:
    if offset is None or expected is None:
        raise ShopPatchError(f"{label} is absent or zero-masked; this slot is not patchable in V1.")
    if offset < 0 or offset + len(expected) > len(data):
        raise ShopPatchError(f"{label} points outside the serialized table.")
    if data[offset : offset + len(expected)] != expected:
        raise ShopPatchError(f"{label} no longer matches the parsed source bytes.")


# Validate article and price layout
def _validate_article_and_price_layout(product: ShopTableProduct) -> None:
    if (
        len(product.article_path_bytes) != 20
        or len(product.price_bytes) != 4
        or product.price_offset != product.article_path_offset + 20
    ):
        raise ShopPatchError(
            f"{_slot(product)!r} does not match the verified ArticleDataPath/Price layout."
        )


# Patch shop table
def patch_shop_table(
    data: bytes,
    parsed: ShopTableParseResult,
    assignments: Iterable[ShopAssignment],
    *,
    reparse: Callable[[bytes], ShopTableParseResult],
    allow_cross_family: bool = False,
    source_data_by_family: dict[str, bytes] | None = None,
    encoded_article_path_bytes: dict[ShopSlot, bytes] | None = None,
    unlock_npc_trader_shops: bool = False,
) -> ShopTablePatchResult:
    """Copy Article identity while retaining every destination-local shop field.

    Only explicitly proven finite slots may appear as sources or destinations;
    infinite, protected, and unresolved slots are rejected even if a stale plan
    attempts to include them.
    """
    planned = tuple(assignments)
    if type(allow_cross_family) is not bool:
        raise ShopPatchError("Cross-family shop payload switch must be a boolean.")
    if type(unlock_npc_trader_shops) is not bool:
        raise ShopPatchError("NPC Trader unlock switch must be a boolean.")
    if parsed.family not in {"NORMAL", "DLC", "LUXURY", "TRADE", "TRADE_DLC"}:
        raise ShopPatchError(f"Unsupported parsed shop family {parsed.family!r}.")
    if len(data) < 4:
        raise ShopPatchError("Serialized shop export is too short.")

    original_by_slot = {_slot(product): product for product in parsed.products}
    if len(original_by_slot) != len(parsed.products):
        raise ShopPatchError("Parsed table contains duplicate product slots.")
    destinations: list[ShopSlot] = []
    sources: list[object] = []
    stock_override_bytes: dict[ShopSlot, bytes] = {}
    for assignment in planned:
        destination = assignment.destination
        source = assignment.source
        destination_key = _slot(destination)
        if destination.family.upper() != parsed.family:
            raise ShopPatchError("Shop destination belongs to another table family.")
        if original_by_slot.get(destination_key) != destination:
            raise ShopPatchError(f"Destination {destination_key!r} is stale or belongs to another table.")
        _verify_bytes(data, destination.article_path_offset, destination.article_path_bytes, "Destination ArticleDataPath")
        _validate_article_and_price_layout(destination)
        _verify_bytes(data, destination.price_offset, destination.price_bytes, "Destination Price")
        destination_reason = shop_slot_lock_reason(destination)
        if destination_reason is not None:
            raise ShopPatchError(f"Shop destination {destination_key!r} is locked: {destination_reason}.")
        if assignment.stock_override is not None:
            if type(assignment.stock_override) is not int or assignment.stock_override < 1:
                raise ShopPatchError("Shop stock overrides must be positive integers.")
            try:
                replacement_stock = struct.pack("<i", assignment.stock_override)
            except struct.error as error:
                raise ShopPatchError("Shop stock override does not fit the serialized integer field.") from error
            if destination.stock_offset is None or destination.stock_bytes is None or len(destination.stock_bytes) != 4:
                raise ShopPatchError("Destination Stock is absent or does not match the verified four-byte layout.")
            _verify_bytes(data, destination.stock_offset, destination.stock_bytes, "Destination Stock")
            stock_override_bytes[destination_key] = replacement_stock
        if assignment.article_only:
            if not isinstance(source, ArticleRewardIdentity):
                raise ShopPatchError("Article-only Shop assignments require a canonical Article identity.")
            if not source.article_path.startswith("/Game/") or not source.item_object:
                raise ShopPatchError("Article-only Shop source has an invalid canonical identity.")
            if source.source_product is not None:
                origin = source.source_product
                origin_reason = shop_slot_lock_reason(origin)
                if origin_reason is not None:
                    raise ShopPatchError(f"Shop source {_slot(origin)!r} is locked: {origin_reason}.")
                if origin.article_path != source.article_path or origin.item_object != source.item_object:
                    raise ShopPatchError("Article-only Shop source no longer matches its validated product identity.")
                source_data = data if origin.family.upper() == parsed.family else (
                    source_data_by_family.get(origin.family.upper()) if source_data_by_family else None
                )
                if source_data is None:
                    raise ShopPatchError("Article-only Shop source table bytes were not supplied for integrity checking.")
                _verify_bytes(source_data, origin.article_path_offset, origin.article_path_bytes, "Source ArticleDataPath")
            source_key = ("ARTICLE", source.source_id)
            encoded_default = b""
        else:
            if not isinstance(source, ShopTableProduct):
                raise ShopPatchError("Shop-only assignments require a validated Shop source product.")
            source_key = _slot(source)
            if source.family.upper() != parsed.family and not allow_cross_family:
                raise ShopPatchError("Shop payloads cannot move across table families unless global mixing is enabled.")
            if not _compatible_families(parsed.family, source.family.upper()):
                raise ShopPatchError(f"Shop payloads cannot move from {source.family} into {parsed.family}.")
            if source.family.upper() == parsed.family and original_by_slot.get(source_key) != source:
                raise ShopPatchError(f"Source {source_key!r} is stale or belongs to another table.")
            if source.family.upper() != parsed.family and not source_data_by_family:
                raise ShopPatchError("Cross-family shop source bytes were not supplied for integrity checking.")
            source_reason = shop_slot_lock_reason(source)
            if source_reason is not None:
                raise ShopPatchError(f"Shop source {source_key!r} is locked: {source_reason}.")
            source_data = data if source.family.upper() == parsed.family else source_data_by_family.get(source.family.upper())
            if source_data is None:
                raise ShopPatchError(f"Cross-family shop source table {source.family} is absent from the integrity inputs.")
            _verify_bytes(source_data, source.article_path_offset, source.article_path_bytes, "Source ArticleDataPath")
            encoded_default = source.article_path_bytes
        encoded = (encoded_article_path_bytes or {}).get(destination_key, encoded_default)
        if len(encoded) != 20:
            raise ShopPatchError(f"{destination_key!r} encoded ArticleDataPath is not 20 bytes.")
        destinations.append(destination_key)
        sources.append(source_key)

    if len(set(destinations)) != len(destinations):
        raise ShopPatchError("The patch plan targets a destination more than once.")
    if len(set(sources)) != len(sources):
        raise ShopPatchError("Shop assignments reuse one source payload more than once.")
    payload_assignments = tuple(item for item in planned if not item.article_only)
    payload_sources = {
        _slot(item.source) for item in payload_assignments
        if isinstance(item.source, ShopTableProduct)
    }
    payload_destinations = {_slot(item.destination) for item in payload_assignments}
    if payload_assignments and not allow_cross_family and payload_sources != payload_destinations:
        raise ShopPatchError("Shop assignments must be a permutation of the same source/destination slots.")

    output = bytearray(data)
    for assignment in planned:
        destination = assignment.destination
        source = assignment.source
        path_start = destination.article_path_offset
        path_bytes = (encoded_article_path_bytes or {}).get(
            _slot(destination), source.article_path_bytes if isinstance(source, ShopTableProduct) else b"",
        )
        output[path_start : path_start + 20] = path_bytes
        replacement_stock = stock_override_bytes.get(_slot(destination))
        if replacement_stock is not None:
            assert destination.stock_offset is not None
            output[destination.stock_offset : destination.stock_offset + 4] = replacement_stock

    if unlock_npc_trader_shops and parsed.family in {"TRADE", "TRADE_DLC"}:
        for row in parsed.rows:
            if _BUDDY_ROW.fullmatch(row.name) is None:
                continue
            for group in row.groups:
                replacement = _unlocked_affinity_formula_bytes(row.name, group)
                if replacement is None:
                    continue
                assert group.evaluation_formula_offset is not None
                start = group.evaluation_formula_offset
                output[start : start + len(replacement)] = replacement

    patched_data = bytes(output)
    reparsed = reparse(patched_data)
    if reparsed.family != parsed.family:
        raise ShopPatchError("Reparsed output changed the shop family.")
    validate_shop_replenish_groups(
        parsed, reparsed, unlock_npc_trader_shops=unlock_npc_trader_shops,
    )
    after_by_slot = {_slot(product): product for product in reparsed.products}
    if set(after_by_slot) != set(original_by_slot):
        raise ShopPatchError("Reparsed output changed the shop slot identities.")

    assignment_by_destination = {_slot(item.destination): item for item in planned}
    for key, before in original_by_slot.items():
        after = after_by_slot[key]
        assignment = assignment_by_destination.get(key)
        expected_stock = (
            assignment.stock_override
            if assignment is not None and assignment.stock_override is not None
            else before.stock
        )
        expected_stock_bytes = (
            struct.pack("<i", expected_stock)
            if assignment is not None and assignment.stock_override is not None
            else before.stock_bytes
        )
        stock_matches = (
            after.uses_stock == before.uses_stock
            and after.uses_stock_bytes == before.uses_stock_bytes
            and after.stock == expected_stock
            and after.stock_bytes == expected_stock_bytes
            and after.evaluation_raw == before.evaluation_raw
        )
        if assignment is None:
            if (
                after.article_path_bytes != before.article_path_bytes
                or after.price_bytes != before.price_bytes
                or not stock_matches
                or after.evaluation_raw != before.evaluation_raw
            ):
                raise ShopPatchError(f"Unassigned slot {key!r} changed during serialization.")
            continue
        source = assignment.source
        path_bytes = (encoded_article_path_bytes or {}).get(
            key, source.article_path_bytes if isinstance(source, ShopTableProduct) else b"",
        )
        if assignment.article_only:
            assert isinstance(source, ArticleRewardIdentity)
            matches = (
                after.article_path == source.article_path
                and after.item_object == source.item_object
                and after.article_path_number == source.article_path_number
                and after.item_object_number == source.item_object_number
                and after.article_path_bytes == path_bytes
                and after.price == before.price
                and after.price_bytes == before.price_bytes
                and stock_matches
                and after.evaluation_raw == before.evaluation_raw
            )
        else:
            assert isinstance(source, ShopTableProduct)
            matches = (
                after.article_path == source.article_path
                and after.item_object == source.item_object
                and after.article_path_number == source.article_path_number
                and after.item_object_number == source.item_object_number
                and after.article_path_bytes == path_bytes
                and after.price == before.price
                and after.price_bytes == before.price_bytes
                and stock_matches
                and after.evaluation_raw == before.evaluation_raw
            )
        if not matches:
            raise ShopPatchError(f"Reparsed destination {key!r} does not match the requested payload.")

    return ShopTablePatchResult(
        data=patched_data,
        parsed=reparsed,
        changed_slots=tuple(
            sorted({
                key for key, item in assignment_by_destination.items()
                if item.article_only or item.destination != item.source
                or item.stock_override is not None and item.stock_override != item.destination.stock
            })
        ),
    )


# Validate shop replenish groups
def validate_shop_replenish_groups(
    before: ShopTableParseResult,
    after: ShopTableParseResult,
    *,
    unlock_npc_trader_shops: bool = False,
) -> None:
    """Verify only explicitly requested NPC Trader affinity fields may change."""
    if type(unlock_npc_trader_shops) is not bool:
        raise ShopPatchError("NPC Trader unlock switch must be a boolean.")
    if tuple(row.name for row in before.rows) != tuple(row.name for row in after.rows):
        raise ShopPatchError("Reparsed output changed Shop row identities or order.")
    for old_row, new_row in zip(before.rows, after.rows, strict=True):
        if len(old_row.groups) != len(new_row.groups):
            raise ShopPatchError(f"Reparsed output changed ReplenishInfo group count in {old_row.name}.")
        for old_group, new_group in zip(old_row.groups, new_row.groups, strict=True):
            old_slots = tuple(product.product_index for product in old_group.products)
            new_slots = tuple(product.product_index for product in new_group.products)
            if old_slots != new_slots:
                raise ShopPatchError(f"Reparsed output moved product slots between ReplenishInfo groups in {old_row.name}.")
            expected_formula = old_group.evaluation_formula
            expected_raw = old_group.evaluation_raw
            if (
                unlock_npc_trader_shops
                and before.family in {"TRADE", "TRADE_DLC"}
                and _BUDDY_ROW.fullmatch(old_row.name) is not None
            ):
                replacement = _unlocked_affinity_formula_bytes(old_row.name, old_group)
                if replacement is not None:
                    if old_group.evaluation_formula_offset is None or old_group.evaluation_flag_offset is None:
                        raise ShopPatchError(f"NPC Trader formula offsets are missing in {old_row.name}.")
                    relative = old_group.evaluation_formula_offset - old_group.evaluation_flag_offset
                    if relative < 0 or relative + len(replacement) > len(expected_raw):
                        raise ShopPatchError(f"NPC Trader formula span escapes its Flag struct in {old_row.name}.")
                    expected_raw = (
                        expected_raw[:relative] + replacement
                        + expected_raw[relative + len(replacement):]
                    )
                    expected_formula = _decode_formula_fstring(replacement)
            if (
                expected_formula != new_group.evaluation_formula
                or expected_raw != new_group.evaluation_raw
            ):
                raise ShopPatchError(f"Reparsed output changed an unlock/evaluation formula in {old_row.name}.")


# Count unlocked affinity formula bytes
def _unlocked_affinity_formula_bytes(
    row_name: str,
    group: ShopReplenishGroup,
) -> bytes | None:
    formula = group.evaluation_formula
    if not formula:
        return None
    row_match = _BUDDY_ROW.fullmatch(row_name)
    formula_match = _BUDDY_AFFINITY_FORMULA.match(formula)
    if row_match is None:
        return None
    if formula_match is None:
        if re.search(rf"Friendship\.{re.escape(row_match.group(1))}\.", formula, re.IGNORECASE):
            raise ShopPatchError(
                f"Unrecognized NPC Trader affinity formula in {row_name}; refusing a partial unlock."
            )
        # Story/episode conditions in a few Buddy rows are independent of
        # affinity and deliberately remain intact.
        return None
    if (
        len({value.casefold() for value in formula_match.groups()[:3]}) != 1
        or formula_match.group(1).casefold() != row_match.group(1).casefold()
    ):
        raise ShopPatchError(
            f"Unrecognized NPC Trader affinity formula in {row_name}; refusing a partial unlock."
        )
    if group.evaluation_formula_offset is None or group.evaluation_formula_bytes is None:
        raise ShopPatchError(f"NPC Trader affinity formula bytes are unavailable in {row_name}.")
    raw = group.evaluation_formula_bytes
    if _decode_formula_fstring(raw) != formula:
        raise ShopPatchError(f"NPC Trader affinity formula bytes do not match parsed text in {row_name}.")
    threshold = formula_match.group(4)
    threshold_start, threshold_end = formula_match.span(4)
    unlocked_formula = (
        formula[:threshold_start] + ("0" * len(threshold)) + formula[threshold_end:]
    )
    encoded = _encode_formula_fstring_like(raw, unlocked_formula)
    if len(encoded) != len(raw):
        raise ShopPatchError(f"NPC Trader unlock formula changed serialized width in {row_name}.")
    return encoded


# Decode formula FString
def _decode_formula_fstring(raw: bytes) -> str:
    if len(raw) < 4:
        raise ShopPatchError("Serialized Shop formula FString is truncated.")
    length = struct.unpack_from("<i", raw)[0]
    if length > 0 and len(raw) == 4 + length:
        if not raw.endswith(b"\0"):
            raise ShopPatchError("Serialized Shop formula FString has no ANSI terminator.")
        try:
            return raw[4:-1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ShopPatchError("Serialized Shop formula FString is not valid UTF-8.") from error
    if length < 0 and len(raw) == 4 + (-length * 2):
        if not raw.endswith(b"\0\0"):
            raise ShopPatchError("Serialized Shop formula FString has no UTF-16 terminator.")
        try:
            return raw[4:-2].decode("utf-16-le")
        except UnicodeDecodeError as error:
            raise ShopPatchError("Serialized Shop formula FString is not valid UTF-16LE.") from error
    if length == 0 and len(raw) == 4:
        return ""
    raise ShopPatchError("Serialized Shop formula FString length is inconsistent.")


# Encode formula FString like
def _encode_formula_fstring_like(raw: bytes, value: str) -> bytes:
    length = struct.unpack_from("<i", raw)[0]
    if length > 0:
        return struct.pack("<i", length) + value.encode("utf-8") + b"\0"
    if length < 0:
        return struct.pack("<i", length) + value.encode("utf-16-le") + b"\0\0"
    if value:
        raise ShopPatchError("Cannot populate a zero-length Shop formula FString in place.")
    return raw
