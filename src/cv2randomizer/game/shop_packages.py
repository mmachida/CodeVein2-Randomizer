"""Strict readers for cooked shop package headers and FName maps.

The current table fixtures use unversioned export properties. This module reads
only the package summary and serialized FName map; decoding DataTable rows
requires the matching usmap and is intentionally handled by a later layer.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .catalogs import ShopProduct

_PACKAGE_TAG = 0x9E2A83C1
_SUPPORTED_LEGACY_VERSION = -8
_MAX_NAME_COUNT = 100_000
_MAX_STRING_BYTES = 1_048_576
SHOP_TABLE_NAMES = {
    "NORMAL": "DT_ShopLineup",
    "DLC": "DT_ShopLineup_DLC",
    "LUXURY": "DT_ShopLineup_Luxury",
    "TRADE": "DT_ShopLineup_Trade",
    "TRADE_DLC": "DT_ShopLineup_Trade_DLC",
}
_SHOP_PACKAGE_DIRECTORY = "/Game/Blueprint/GameSystem/Shop/"


class ShopPackageError(ValueError):
    """Raised when a cooked shop package cannot be parsed or validated safely."""


@dataclass(frozen=True, slots=True)
class CookedShopNameMap:
    """Package summary fields and its serialized FName table."""

    package_name: str
    package_flags: int
    total_header_size: int
    name_offset: int
    names: tuple[str, ...]
    is_unversioned: bool

    # Check whether the item exists
    def contains(self, name: str) -> bool:
        return name in self.names

    # Resolve the requested asset
    def resolve(self, comparison_index: int, number: int = 0) -> str:
        """Resolve an Unreal FName, including its numeric suffix convention."""
        if isinstance(comparison_index, bool) or not isinstance(comparison_index, int):
            raise ShopPackageError("FName comparison index must be an integer.")
        if comparison_index < 0 or comparison_index >= len(self.names):
            raise ShopPackageError(f"FName index {comparison_index} is outside the package name map.")
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ShopPackageError("FName number must be a non-negative integer.")
        base_name = self.names[comparison_index]
        return base_name if number == 0 else f"{base_name}_{number - 1}"


@dataclass(frozen=True, slots=True)
class ShopCatalogReferenceSummary:
    """Counts of catalog identities verified against one package FName map."""

    family: str
    package_name: str
    product_slots: int
    unique_rows: int
    unique_items: int
    name_count: int


class _Reader:
    # Initialize this object
    def __init__(self, data: bytes, source: str) -> None:
        self.data = data
        self.source = source
        self.position = 0

    # Read serialized package data
    def read(self, count: int, label: str) -> bytes:
        if count < 0 or self.position + count > len(self.data):
            raise ShopPackageError(
                f"{self.source}: truncated package while reading {label} at offset {self.position}."
            )
        value = self.data[self.position : self.position + count]
        self.position += count
        return value

    # Unpack serialized package data
    def unpack(self, fmt: str, label: str) -> tuple[int, ...]:
        size = struct.calcsize(fmt)
        raw = self.read(size, label)
        return struct.unpack(fmt, raw)

    # Read I32
    def i32(self, label: str) -> int:
        return self.unpack("<i", label)[0]

    # Parse U32
    def u32(self, label: str) -> int:
        return self.unpack("<I", label)[0]

    # Encode an Unreal FString
    def fstring(self, label: str) -> str:
        length = self.i32(f"{label} length")
        if length == 0:
            return ""
        if abs(length) > _MAX_STRING_BYTES:
            raise ShopPackageError(f"{self.source}: unreasonable {label} length {length}.")

        if length > 0:
            raw = self.read(length, label)
            if not raw.endswith(b"\0"):
                raise ShopPackageError(f"{self.source}: {label} has no ANSI terminator.")
            try:
                return raw[:-1].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ShopPackageError(f"{self.source}: {label} is not valid UTF-8.") from error

        byte_count = abs(length) * 2
        raw = self.read(byte_count, label)
        if not raw.endswith(b"\0\0"):
            raise ShopPackageError(f"{self.source}: {label} has no UTF-16 terminator.")
        try:
            return raw[:-2].decode("utf-16-le")
        except UnicodeDecodeError as error:
            raise ShopPackageError(f"{self.source}: {label} is not valid UTF-16LE.") from error


# Parse cooked name map
def parse_cooked_name_map(
    data: bytes,
    *,
    source: str = "<memory>",
    expected_package_name: str | None = None,
) -> CookedShopNameMap:
    """Read the UE5 cooked package summary and FName map from a .uasset."""
    if len(data) < 36:
        raise ShopPackageError(f"{source}: package is too small to contain a summary.")

    reader = _Reader(data, source)
    tag = reader.u32("package tag")
    if tag != _PACKAGE_TAG:
        raise ShopPackageError(f"{source}: invalid Unreal package tag {tag:#010x}.")

    legacy_version = reader.i32("legacy file version")
    if legacy_version != _SUPPORTED_LEGACY_VERSION:
        raise ShopPackageError(
            f"{source}: unsupported legacy file version {legacy_version}; expected -8."
        )

    _legacy_ue3_version = reader.i32("legacy UE3 version")
    file_version_ue4 = reader.i32("UE4 file version")
    file_version_ue5 = reader.i32("UE5 file version")
    licensee_version = reader.i32("licensee file version")
    if file_version_ue4 != 0 or file_version_ue5 != 0 or licensee_version != 0:
        raise ShopPackageError(
            f"{source}: versioned package summaries are not supported by this focused reader."
        )

    custom_version_count = reader.i32("custom version count")
    if not 0 <= custom_version_count <= 128:
        raise ShopPackageError(f"{source}: invalid custom version count {custom_version_count}.")
    for index in range(custom_version_count):
        reader.read(20, f"custom version {index}")

    total_header_size = reader.i32("total header size")
    package_name = reader.fstring("package name")
    package_flags = reader.u32("package flags")
    name_count = reader.i32("name count")
    name_offset = reader.i32("name offset")

    if total_header_size != len(data):
        raise ShopPackageError(
            f"{source}: total header size {total_header_size} does not match "
            f"the .uasset length {len(data)}."
        )
    if not package_name.startswith("/Game/"):
        raise ShopPackageError(f"{source}: unexpected virtual package name {package_name!r}.")
    if expected_package_name is not None and package_name != expected_package_name:
        raise ShopPackageError(
            f"{source}: package is {package_name!r}, expected {expected_package_name!r}."
        )
    if not 0 < name_count <= _MAX_NAME_COUNT:
        raise ShopPackageError(f"{source}: unreasonable name count {name_count}.")
    if name_offset < reader.position or name_offset >= total_header_size:
        raise ShopPackageError(f"{source}: name map offset {name_offset} is outside the package.")

    name_reader = _Reader(data, source)
    name_reader.position = name_offset
    names: list[str] = []
    for index in range(name_count):
        name = name_reader.fstring(f"name map entry {index}")
        name_reader.read(4, f"name map hash {index}")
        names.append(name)
    if name_reader.position > total_header_size:
        raise ShopPackageError(f"{source}: name map extends past the package header.")

    return CookedShopNameMap(
        package_name=package_name,
        package_flags=package_flags,
        total_header_size=total_header_size,
        name_offset=name_offset,
        names=tuple(names),
        is_unversioned=True,
    )


# Read cooked name map
def read_cooked_name_map(
    asset_path: Path,
    *,
    expected_package_name: str | None = None,
) -> CookedShopNameMap:
    """Read a shop .uasset from a caller-selected path."""
    try:
        data = asset_path.read_bytes()
    except OSError as error:
        raise ShopPackageError(f"Could not read {asset_path}: {error}") from error
    return parse_cooked_name_map(
        data,
        source=asset_path.name,
        expected_package_name=expected_package_name,
    )


# Validate shop catalog references
def validate_shop_catalog_references(
    name_map: CookedShopNameMap,
    family: str,
    products: Iterable[ShopProduct],
) -> ShopCatalogReferenceSummary:
    """Ensure each catalog item, object, and row name exists in a package."""
    normalized_family = family.strip().upper()
    if normalized_family not in SHOP_TABLE_NAMES:
        raise ShopPackageError(f"Unknown shop table family {family!r}.")

    expected_name = _SHOP_PACKAGE_DIRECTORY + SHOP_TABLE_NAMES[normalized_family]
    if name_map.package_name != expected_name:
        raise ShopPackageError(
            f"Shop family {normalized_family} maps to {expected_name!r}, "
            f"not {name_map.package_name!r}."
        )

    entries = tuple(products)
    if not entries:
        raise ShopPackageError(f"Shop catalog family {normalized_family} is empty.")
    if any(product.family != normalized_family for product in entries):
        raise ShopPackageError(f"Mixed shop families supplied for {normalized_family}.")

    names = set(name_map.names)
    missing_paths = sorted(
        {product.original_product_path for product in entries if product.original_product_path not in names}
    )
    missing_objects = sorted(
        {product.item_object for product in entries if not product.item_object or product.item_object not in names}
    )
    missing_rows = sorted({product.shop_row for product in entries if product.shop_row not in names})
    if missing_paths or missing_objects or missing_rows:
        details = []
        if missing_paths:
            details.append(f"{len(missing_paths)} article paths")
        if missing_objects:
            details.append(f"{len(missing_objects)} item object names")
        if missing_rows:
            details.append(f"{len(missing_rows)} shop row names")
        raise ShopPackageError(
            f"{normalized_family} FName map is missing catalog references: {', '.join(details)}."
        )

    return ShopCatalogReferenceSummary(
        family=normalized_family,
        package_name=name_map.package_name,
        product_slots=len(entries),
        unique_rows=len({product.shop_row for product in entries}),
        unique_items=len({product.original_product_path for product in entries}),
        name_count=len(name_map.names),
    )
