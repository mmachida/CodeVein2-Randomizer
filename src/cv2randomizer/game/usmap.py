"""Focused reader for Unreal Engine .usmap type mappings.

The shop assets use unversioned properties. This module reads the schema from
their local mapping file; it does not decode package exports or decompress
compressed mapping variants.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_MAGIC = 0x30C4
_MAX_MAPPING_BYTES = 128 * 1024 * 1024
_MAX_TABLE_ENTRIES = 2_000_000

_PROPERTY_NAMES = (
    "ByteProperty",
    "BoolProperty",
    "IntProperty",
    "FloatProperty",
    "ObjectProperty",
    "NameProperty",
    "DelegateProperty",
    "DoubleProperty",
    "ArrayProperty",
    "StructProperty",
    "StrProperty",
    "TextProperty",
    "InterfaceProperty",
    "MulticastDelegateProperty",
    "WeakObjectProperty",
    "LazyObjectProperty",
    "AssetObjectProperty",
    "SoftObjectProperty",
    "UInt64Property",
    "UInt32Property",
    "UInt16Property",
    "Int64Property",
    "Int16Property",
    "Int8Property",
    "MapProperty",
    "SetProperty",
    "EnumProperty",
    "FieldPathProperty",
    "OptionalProperty",
    "Utf8StrProperty",
    "AnsiStrProperty",
    "ClassProperty",
    "MulticastInlineDelegateProperty",
    "SoftClassProperty",
    "VerseStringProperty",
    "VerseDynamicProperty",
    "VerseFunctionProperty",
)


class UsmapError(ValueError):
    """Raised when a .usmap is unsupported, malformed, or truncated."""


@dataclass(frozen=True, slots=True)
class UsmapPropertyType:
    """A property type and its nested mapping references, when applicable."""

    code: int
    struct_name: str | None = None
    enum_name: str | None = None
    inner_type: UsmapPropertyType | None = None
    key_type: UsmapPropertyType | None = None
    value_type: UsmapPropertyType | None = None

    # Resolve name
    @property
    def name(self) -> str:
        if self.code >= len(_PROPERTY_NAMES):
            return f"UnknownPropertyType{self.code}"
        return _PROPERTY_NAMES[self.code]


@dataclass(frozen=True, slots=True)
class UsmapProperty:
    """One serializable property in a mapped struct."""

    schema_index: int
    name: str
    array_dim: int
    type: UsmapPropertyType


@dataclass(frozen=True, slots=True)
class UsmapStruct:
    """A mapped Unreal struct or class."""

    name: str
    super_name: str | None
    property_count: int
    properties: tuple[UsmapProperty, ...]

    # Read property
    def property(self, name: str) -> UsmapProperty | None:
        """Look up a serializable property without case sensitivity."""
        folded = name.casefold()
        return next((item for item in self.properties if item.name.casefold() == folded), None)


@dataclass(frozen=True, slots=True)
class UsmapMappings:
    """The struct mappings required to interpret unversioned properties."""

    version: int
    name_count: int
    enum_count: int
    struct_count: int
    structs: Mapping[str, UsmapStruct]
    ignored_trailing_bytes: int

    # Read struct
    def struct(self, name: str) -> UsmapStruct | None:
        """Look up a struct or class mapping without case sensitivity."""
        return self.structs.get(name.casefold())


class _Reader:
    # Initialize this object
    def __init__(self, data: bytes, source: str) -> None:
        self.data = data
        self.source = source
        self.position = 0

    # Read remaining
    @property
    def remaining(self) -> int:
        return len(self.data) - self.position

    # Read serialized package data
    def read(self, count: int, label: str) -> bytes:
        if count < 0 or count > self.remaining:
            raise UsmapError(
                f"{self.source}: truncated while reading {label} at offset {self.position}."
            )
        result = self.data[self.position : self.position + count]
        self.position += count
        return result

    # List value
    def value(self, format_string: str, label: str) -> int:
        size = struct.calcsize(format_string)
        return struct.unpack(format_string, self.read(size, label))[0]

    # Resolve name index
    def name_index(self, names: list[str], label: str, *, nullable: bool = False) -> str | None:
        index = self.value("<i", label)
        if nullable and index == -1:
            return None
        if not 0 <= index < len(names):
            raise UsmapError(f"{self.source}: invalid {label} index {index}.")
        return names[index]


# Parse USMAP
def parse_usmap(data: bytes, *, source: str = "<memory>") -> UsmapMappings:
    """Parse an uncompressed .usmap's names, enums, and struct schemas."""
    if not 0 < len(data) <= _MAX_MAPPING_BYTES:
        raise UsmapError(f"{source}: mapping size is outside the supported range.")

    header = _Reader(data, source)
    magic = header.value("<H", "magic")
    if magic != _MAGIC:
        raise UsmapError(f"{source}: invalid .usmap magic {magic:#06x}.")
    version = header.value("<B", "format version")
    if version > 5:
        raise UsmapError(f"{source}: unsupported .usmap format version {version}.")

    if version >= 1:
        has_versioning = header.value("<B", "versioning flag")
        if has_versioning not in (0, 1):
            raise UsmapError(f"{source}: invalid .usmap versioning flag {has_versioning}.")
        if has_versioning:
            raise UsmapError(f"{source}: versioned .usmap headers are not supported.")

    compression_method = header.value("<i", "compression method")
    compressed_size = header.value("<I", "compressed size")
    uncompressed_size = header.value("<I", "uncompressed size")
    if compression_method != 0:
        raise UsmapError(
            f"{source}: compression method {compression_method} is unsupported; expected uncompressed data."
        )
    if compressed_size != uncompressed_size or compressed_size != header.remaining:
        raise UsmapError(f"{source}: uncompressed data size does not match the file length.")
    if uncompressed_size > _MAX_MAPPING_BYTES:
        raise UsmapError(f"{source}: uncompressed mapping is unreasonably large.")

    payload = _Reader(header.read(uncompressed_size, "mapping payload"), source)
    name_count = payload.value("<I", "name count")
    if not 0 < name_count <= _MAX_TABLE_ENTRIES:
        raise UsmapError(f"{source}: unreasonable name count {name_count}.")
    names: list[str] = []
    for index in range(name_count):
        length_format = "<H" if version >= 2 else "<B"
        length = payload.value(length_format, f"name {index} length")
        try:
            names.append(payload.read(length, f"name {index}").decode("utf-8"))
        except UnicodeDecodeError as error:
            raise UsmapError(f"{source}: name {index} is not valid UTF-8.") from error

    enum_count = payload.value("<I", "enum count")
    if enum_count > _MAX_TABLE_ENTRIES:
        raise UsmapError(f"{source}: unreasonable enum count {enum_count}.")
    for enum_index in range(enum_count):
        payload.name_index(names, f"enum {enum_index} name")
        member_count = payload.value("<H" if version >= 3 else "<B", f"enum {enum_index} member count")
        for member_index in range(member_count):
            if version >= 4:
                payload.value("<Q", f"enum {enum_index} member {member_index} value")
            payload.name_index(names, f"enum {enum_index} member {member_index} name")

    struct_count = payload.value("<I", "struct count")
    if struct_count > _MAX_TABLE_ENTRIES:
        raise UsmapError(f"{source}: unreasonable struct count {struct_count}.")

    # Read property type
    def property_type(depth: int = 0) -> UsmapPropertyType:
        if depth > 32:
            raise UsmapError(f"{source}: property type nesting exceeds the safety limit.")
        code = payload.value("<B", "property type")
        if code >= len(_PROPERTY_NAMES):
            raise UsmapError(f"{source}: unsupported property type code {code}.")
        if code == 9:  # StructProperty
            return UsmapPropertyType(
                code=code,
                struct_name=payload.name_index(names, "struct property type name"),
            )
        if code in (8, 25, 28):  # ArrayProperty, SetProperty, OptionalProperty
            return UsmapPropertyType(code=code, inner_type=property_type(depth + 1))
        if code == 24:  # MapProperty
            return UsmapPropertyType(
                code=code,
                key_type=property_type(depth + 1),
                value_type=property_type(depth + 1),
            )
        if code == 26:  # EnumProperty
            inner = property_type(depth + 1)
            enum_name = payload.name_index(names, "enum property type name", nullable=True)
            return UsmapPropertyType(code=code, inner_type=inner, enum_name=enum_name)
        return UsmapPropertyType(code=code)

    structs: dict[str, UsmapStruct] = {}
    for struct_index in range(struct_count):
        name = payload.name_index(names, f"struct {struct_index} name")
        if name is None:
            raise UsmapError(f"{source}: struct {struct_index} has no name.")
        super_name = payload.name_index(names, f"struct {struct_index} super name", nullable=True)
        property_count = payload.value("<H", f"struct {name} property count")
        serializable_count = payload.value("<H", f"struct {name} serializable property count")
        if serializable_count > property_count:
            raise UsmapError(f"{source}: struct {name} has more serializable than total properties.")

        properties: list[UsmapProperty] = []
        for property_index in range(serializable_count):
            schema_index = payload.value("<H", f"struct {name} property {property_index} index")
            array_dim = payload.value("<B", f"struct {name} property {property_index} array dimension")
            if array_dim == 0:
                raise UsmapError(f"{source}: struct {name} has a zero-sized property array.")
            property_name = payload.name_index(
                names, f"struct {name} property {property_index} name"
            )
            if property_name is None:
                raise UsmapError(f"{source}: struct {name} property {property_index} has no name.")
            properties.append(
                UsmapProperty(
                    schema_index=schema_index,
                    name=property_name,
                    array_dim=array_dim,
                    type=property_type(),
                )
            )

        # CUE4Parse resolves name-only mappings case-insensitively and keeps the
        # last definition if a mapping repeats a simple struct name.
        structs[name.casefold()] = UsmapStruct(
            name=name,
            super_name=super_name,
            property_count=property_count,
            properties=tuple(properties),
        )

    return UsmapMappings(
        version=version,
        name_count=name_count,
        enum_count=enum_count,
        struct_count=struct_count,
        structs=structs,
        ignored_trailing_bytes=payload.remaining,
    )


# Read USMAP
def read_usmap(mapping_path: Path) -> UsmapMappings:
    """Read an uncompressed .usmap from a caller-selected path."""
    try:
        data = mapping_path.read_bytes()
    except OSError as error:
        raise UsmapError(f"Could not read {mapping_path}: {error}") from error
    return parse_usmap(data, source=mapping_path.name)
