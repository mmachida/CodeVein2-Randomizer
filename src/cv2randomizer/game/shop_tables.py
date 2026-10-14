"""Schema-guided reader for the five cooked shop DataTable fixtures."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Iterable

from .catalogs import ShopProduct
from .fname_identity import FNameIdentity
from .shop_packages import (
    CookedShopNameMap,
    SHOP_TABLE_NAMES,
    ShopPackageError,
    validate_shop_catalog_references,
)
from .usmap import UsmapMappings, UsmapProperty, UsmapPropertyType, UsmapStruct

_PACKAGE_TAG = 0x9E2A83C1
_MAX_ARRAY_COUNT = 100_000
_MAX_STRING_BYTES = 1_048_576
_MAX_UNVERSIONED_PROPERTIES = 4096


class ShopTableError(ShopPackageError):
    """Raised when a cooked shop table cannot be decoded and verified."""


@dataclass(frozen=True, slots=True)
class ShopTableProduct:
    """One decoded product slot and its source identity."""

    family: str
    shop_row: str
    product_index: int
    article_path: str
    item_object: str
    # Byte offset of ArticleDataPath within the serialized .uexp export.
    article_path_offset: int
    article_path_bytes: bytes
    price: int
    price_offset: int
    price_bytes: bytes
    uses_stock: bool | None
    uses_stock_offset: int | None
    uses_stock_bytes: bytes | None
    stock: int | None
    stock_offset: int | None
    stock_bytes: bytes | None
    important: bool
    evaluation_formula: str
    evaluation_raw: bytes
    raw_bytes: bytes
    protected: bool = False
    protection_reason: str | None = None
    article_path_number: int = 0
    item_object_number: int = 0
    what_content_class: str = "BASE"
    # Catalog stock semantics stay available even when unversioned serialization
    # zero-masks one or both raw fields. The raw fields above remain the source of
    # truth for bytes that can be copied or compared directly.
    catalog_uses_stock: bool | None = None
    catalog_stock: int | None = None
    uses_stock_span: tuple[int, int] | None = None
    stock_span: tuple[int, int] | None = None
    raw_struct_start: int | None = None
    raw_struct_end: int | None = None
    raw_header_end: int | None = None
    uses_stock_mask_bit: tuple[int, int] | None = None
    stock_mask_bit: tuple[int, int] | None = None

    # Resolve article package path
    @property
    def article_package_path(self) -> str:
        """Exact package identity, including the serialized FName Number."""
        return FNameIdentity(self.article_path, self.article_path_number).resolved

    # Check where content class
    @property
    def where_content_class(self) -> str:
        return "DLC" if self.family.upper() in {"DLC", "TRADE_DLC"} else "BASE"

    # Check explicit stock payload
    @property
    def has_explicit_stock_payload(self) -> bool:
        """Whether both stock properties are serialized with known raw values."""
        return self.uses_stock_bytes is not None and self.stock_bytes is not None

    # Find logical uses stock
    @property
    def logical_uses_stock(self) -> bool | None:
        """Return the decoded stock flag without claiming masked bytes exist."""
        if self.uses_stock is not None:
            return self.uses_stock
        if self.uses_stock_bytes is None and self.uses_stock_mask_bit is not None:
            # Unreal's unversioned-property zero mask selects the schema default.
            # bIsUseStock is a bool, whose default is False.
            return False
        return None

    # Find logical stock
    @property
    def logical_stock(self) -> int | None:
        """Return a decoded integer default while retaining raw absence metadata."""
        if self.stock is not None:
            return self.stock
        if self.stock_bytes is None and self.stock_mask_bit is not None:
            # This is useful for audit output only. A masked zero is not proof of
            # a finite stock sentinel and is never written by the Shop patcher.
            return 0
        return None

    # Read destination uses stock
    @property
    def destination_uses_stock(self) -> bool | None:
        """Validated destination stock behavior, including the trusted catalog."""
        logical = self.logical_uses_stock
        return logical if logical is not None else self.catalog_uses_stock


@dataclass(frozen=True, slots=True)
class ShopReplenishGroup:
    """One replenish group, with its unlock formula preserved byte-for-byte."""

    evaluation_formula: str
    evaluation_raw: bytes
    products: tuple[ShopTableProduct, ...]
    # Absolute .uexp span for the serialized FString and enclosing Flag struct.
    # These offsets are used only for a fixed-width, fail-closed unlock patch.
    evaluation_formula_offset: int | None = None
    evaluation_formula_bytes: bytes | None = None
    evaluation_flag_offset: int | None = None


@dataclass(frozen=True, slots=True)
class ShopTableRow:
    """A row and the grouped products that belong to it."""

    name: str
    groups: tuple[ShopReplenishGroup, ...]
    raw_bytes: bytes

    # List supported products
    @property
    def products(self) -> tuple[ShopTableProduct, ...]:
        return tuple(product for group in self.groups for product in group.products)


@dataclass(frozen=True, slots=True)
class ShopTableParseResult:
    """Decoded table plus opaque package bytes needed for lossless preservation."""

    family: str
    rows: tuple[ShopTableRow, ...]
    opaque_prefix: bytes
    footer: bytes
    has_product_evaluation_extension: bool

    # List supported products
    @property
    def products(self) -> tuple[ShopTableProduct, ...]:
        return tuple(product for row in self.rows for product in row.products)


@dataclass(frozen=True, slots=True)
class _Fragment:
    skip_count: int
    value_count: int
    has_zero_mask: bool
    is_last: bool


@dataclass(frozen=True, slots=True)
class _StructValue:
    values: dict[str, object]
    spans: dict[str, tuple[int, int]]
    raw: bytes
    extra_value: object | None = None
    extra_raw: bytes = b""
    defaulted_properties: frozenset[str] = frozenset()
    header_start: int = 0
    header_end: int = 0
    zero_mask_bits: dict[str, tuple[int, int]] | None = None


@dataclass(frozen=True, slots=True)
class _StructHeader:
    slots: dict[int, bool]
    zero_mask_bits: dict[int, tuple[int, int]]
    end: int


class _Reader:
    # Initialize this object
    def __init__(self, data: bytes, source: str, start: int = 0, end: int | None = None) -> None:
        self.data = data
        self.source = source
        self.position = start
        self.end = len(data) if end is None else end

    # Read remaining
    @property
    def remaining(self) -> int:
        return self.end - self.position

    # Read serialized package data
    def read(self, count: int, label: str) -> bytes:
        if count < 0 or count > self.remaining:
            raise ShopTableError(
                f"{self.source}: truncated while reading {label} at offset {self.position}."
            )
        value = self.data[self.position : self.position + count]
        self.position += count
        return value

    # Unpack serialized package data
    def unpack(self, format_string: str, label: str) -> tuple[int, ...]:
        return struct.unpack(format_string, self.read(struct.calcsize(format_string), label))

    # Read I32
    def i32(self, label: str) -> int:
        return self.unpack("<i", label)[0]

    # Parse U16
    def u16(self, label: str) -> int:
        return self.unpack("<H", label)[0]

    # Encode an Unreal FString
    def fstring(self, label: str) -> str:
        length = self.i32(f"{label} length")
        if length == 0:
            return ""
        if abs(length) > _MAX_STRING_BYTES:
            raise ShopTableError(f"{self.source}: unreasonable {label} length {length}.")
        if length > 0:
            raw = self.read(length, label)
            if not raw.endswith(b"\0"):
                raise ShopTableError(f"{self.source}: {label} has no ANSI terminator.")
            try:
                return raw[:-1].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ShopTableError(f"{self.source}: {label} is not valid UTF-8.") from error
        raw = self.read(abs(length) * 2, label)
        if not raw.endswith(b"\0\0"):
            raise ShopTableError(f"{self.source}: {label} has no UTF-16 terminator.")
        try:
            return raw[:-2].decode("utf-16-le")
        except UnicodeDecodeError as error:
            raise ShopTableError(f"{self.source}: {label} is not valid UTF-16LE.") from error


class _SchemaDecoder:
    # Initialize this object
    def __init__(
        self,
        data: bytes,
        *,
        source: str,
        start: int,
        end: int,
        mappings: UsmapMappings,
        name_map: CookedShopNameMap,
        allow_product_evaluation_extension: bool,
    ) -> None:
        self.reader = _Reader(data, source, start, end)
        self.mappings = mappings
        self.name_map = name_map
        self.allow_product_evaluation_extension = allow_product_evaluation_extension
        self.observed_product_evaluation_extension = False
        self._schema_cache: dict[str, tuple[UsmapProperty, ...]] = {}

    # Read schema properties
    def _schema_properties(self, name: str) -> tuple[UsmapProperty, ...]:
        key = name.casefold()
        if key in self._schema_cache:
            return self._schema_cache[key]
        schema = self.mappings.struct(name)
        if schema is None:
            raise ShopTableError(f"CV2.usmap has no mapping for {name}.")
        chain: list[UsmapStruct] = []
        seen: set[str] = set()
        current: UsmapStruct | None = schema
        while current is not None:
            current_key = current.name.casefold()
            if current_key in seen:
                raise ShopTableError(f"CV2.usmap contains an inheritance cycle at {current.name}.")
            seen.add(current_key)
            chain.append(current)
            current = self.mappings.struct(current.super_name) if current.super_name else None
        properties = tuple(
            sorted(
                (prop for item in reversed(chain) for prop in item.properties),
                key=lambda prop: prop.schema_index,
            )
        )
        indices = [prop.schema_index for prop in properties]
        if len(indices) != len(set(indices)):
            raise ShopTableError(f"CV2.usmap has duplicate inherited property indices in {name}.")
        self._schema_cache[key] = properties
        return properties

    # Read header
    def _read_header(self, label: str) -> _StructHeader:
        fragments: list[_Fragment] = []
        property_index = 0
        total_values = 0
        zero_values = 0
        for _ in range(256):
            bits = self.reader.u16(f"{label} unversioned header fragment")
            fragment = _Fragment(
                skip_count=bits & 0x7F,
                has_zero_mask=bool(bits & 0x80),
                is_last=bool(bits & 0x100),
                value_count=(bits >> 9) & 0x7F,
            )
            property_index += fragment.skip_count
            if property_index > _MAX_UNVERSIONED_PROPERTIES:
                raise ShopTableError(f"{self.reader.source}: unreasonable property index in {label}.")
            fragments.append(fragment)
            property_index += fragment.value_count
            total_values += fragment.value_count
            if fragment.has_zero_mask:
                zero_values += fragment.value_count
            if total_values > _MAX_UNVERSIONED_PROPERTIES:
                raise ShopTableError(f"{self.reader.source}: too many unversioned properties in {label}.")
            if fragment.is_last:
                break
        else:
            raise ShopTableError(f"{self.reader.source}: unterminated unversioned header for {label}.")

        zero_bytes = (zero_values + 7) // 8
        zero_mask_start = self.reader.position
        zero_mask = self.reader.read(zero_bytes, f"{label} zero mask") if zero_bytes else b""
        result: dict[int, bool] = {}
        zero_mask_bits: dict[int, tuple[int, int]] = {}
        cursor = 0
        property_index = 0
        for fragment in fragments:
            property_index += fragment.skip_count
            for _ in range(fragment.value_count):
                if fragment.has_zero_mask:
                    zero_mask_bits[property_index] = (zero_mask_start + cursor // 8, 1 << (cursor % 8))
                is_zero = (
                    bool(zero_mask[cursor // 8] & (1 << (cursor % 8)))
                    if fragment.has_zero_mask
                    else False
                )
                if property_index in result:
                    raise ShopTableError(f"{self.reader.source}: duplicate property slot in {label}.")
                result[property_index] = is_zero
                if fragment.has_zero_mask:
                    cursor += 1
                property_index += 1
        return _StructHeader(result, zero_mask_bits, self.reader.position)

    # Read struct
    def read_struct(self, name: str) -> _StructValue:
        start = self.reader.position
        properties = self._schema_properties(name)
        header = self._read_header(name)
        slots = header.slots
        is_product = name.casefold() == "shopproductinfo"
        schema_indices = {prop.schema_index for prop in properties}
        extra_evaluation_index: int | None = None
        if is_product:
            extras = set(slots) - schema_indices
            if extras:
                expected_extra = max(schema_indices, default=-1) + 1
                if (
                    not self.allow_product_evaluation_extension
                    or extras != {expected_extra}
                    or set(slots) != schema_indices | extras
                ):
                    raise ShopTableError(
                        f"{self.reader.source}: ShopProductInfo has unsupported extra slots "
                        f"{sorted(extras)}."
                    )
                extra_evaluation_index = expected_extra
                self.observed_product_evaluation_extension = True
        if set(slots) != schema_indices | (
            {extra_evaluation_index} if extra_evaluation_index is not None else set()
        ):
            raise ShopTableError(
                f"{self.reader.source}: serialized properties do not match CV2.usmap for {name}; "
                f"header={sorted(slots)}, schema={sorted(schema_indices)}."
            )

        values: dict[str, object] = {}
        spans: dict[str, tuple[int, int]] = {}
        defaulted_properties: set[str] = set()
        for prop in properties:
            value_start = self.reader.position
            if slots[prop.schema_index]:
                value: object = self._default_value(prop.type)
                defaulted_properties.add(prop.name.casefold())
            else:
                value = self.read_property(prop.type, prop.name)
            if prop.array_dim > 1:
                if slots[prop.schema_index]:
                    value = tuple(value for _ in range(prop.array_dim))
                else:
                    raise ShopTableError(
                        f"{self.reader.source}: fixed property arrays are unsupported in {name}.{prop.name}."
                    )
            values[prop.name] = value
            spans[prop.name.casefold()] = (value_start, self.reader.position)

        extra_value: object | None = None
        extra_raw = b""
        if extra_evaluation_index is not None:
            extra_start = self.reader.position
            if not slots[extra_evaluation_index]:
                formula_struct = self._product_evaluation_struct_name()
                extra_value = self.read_struct(formula_struct)
            extra_raw = self.reader.data[extra_start : self.reader.position]

        return _StructValue(
            values=values,
            spans=spans,
            raw=self.reader.data[start : self.reader.position],
            extra_value=extra_value,
            extra_raw=extra_raw,
            defaulted_properties=frozenset(defaulted_properties),
            header_start=start,
            header_end=header.end,
            zero_mask_bits={
                prop.name.casefold(): header.zero_mask_bits[prop.schema_index]
                for prop in properties
                if prop.schema_index in header.zero_mask_bits
            },
        )

    # Build product evaluation struct name
    def _product_evaluation_struct_name(self) -> str:
        replenish = self.mappings.struct("ShopReplenishInfo")
        flag = replenish.property("Flag") if replenish else None
        if flag is None or flag.type.code != 9 or not flag.type.struct_name:
            raise ShopTableError("CV2.usmap does not identify the shop evaluation formula struct.")
        return flag.type.struct_name

    # List default value
    @staticmethod
    def _default_value(prop_type: UsmapPropertyType) -> object:
        if prop_type.code == 1:
            return False
        if prop_type.code in (2, 18, 19, 20, 21, 22, 23):
            return 0
        if prop_type.code == 10:
            return ""
        if prop_type.code == 8:
            return ()
        if prop_type.code == 17:
            return ("", "", "")
        if prop_type.code == 9:
            return {}
        raise ShopTableError(f"Unsupported zero-masked property type {prop_type.name}.")

    # Read property
    def read_property(self, prop_type: UsmapPropertyType, label: str) -> object:
        code = prop_type.code
        if code == 1:  # BoolProperty
            value = self.reader.read(1, label)[0]
            if value not in (0, 1):
                raise ShopTableError(f"{self.reader.source}: invalid boolean value {value} in {label}.")
            return bool(value)
        if code == 2:
            return self.reader.i32(label)
        if code == 8:  # ArrayProperty
            count = self.reader.i32(f"{label} count")
            if not 0 <= count <= _MAX_ARRAY_COUNT:
                raise ShopTableError(f"{self.reader.source}: unreasonable {label} count {count}.")
            if prop_type.inner_type is None:
                raise ShopTableError(f"{self.reader.source}: {label} has no array element type.")
            return tuple(self.read_property(prop_type.inner_type, f"{label}[{index}]") for index in range(count))
        if code == 9:  # StructProperty
            if not prop_type.struct_name:
                raise ShopTableError(f"{self.reader.source}: {label} has no struct mapping name.")
            return self.read_struct(prop_type.struct_name)
        if code == 10:
            return self.reader.fstring(label)
        if code == 17:  # SoftObjectProperty / FSoftObjectPath
            package_path, package_number = self._fname(f"{label} package path")
            object_name, object_number = self._fname(f"{label} object name")
            subpath = self.reader.fstring(f"{label} subpath")
            return package_path, object_name, subpath, package_number, object_number
        raise ShopTableError(
            f"{self.reader.source}: property type {prop_type.name} is unsupported in {label}."
        )

    # Resolve the NameMap FName
    def _fname(self, label: str) -> tuple[str, int]:
        index, number = self.reader.unpack("<ii", label)
        if not 0 <= index < len(self.name_map.names) or number < 0:
            raise ShopTableError(f"{self.reader.source}: invalid FName in {label}.")
        return self.name_map.names[index], number


# Build row name pairs
def _row_name_pairs(name_map: CookedShopNameMap, row_name: str) -> tuple[tuple[int, int], ...]:
    direct = tuple((index, 0) for index, name in enumerate(name_map.names) if name == row_name)
    if direct:
        return direct
    suffix = re.fullmatch(r"(.+)_([0-9]+)", row_name)
    if suffix:
        base, number = suffix.groups()
        return tuple(
            (index, int(number) + 1)
            for index, name in enumerate(name_map.names)
            if name == base
        )
    return ()


# Find row anchor
def _find_row_anchor(
    data: bytes,
    name_map: CookedShopNameMap,
    row_name: str,
    body_end: int,
) -> int:
    pairs = _row_name_pairs(name_map, row_name)
    if not pairs:
        raise ShopTableError(f"Shop name map has no FName for row {row_name!r}.")
    found: set[int] = set()
    for index, number in pairs:
        needle = struct.pack("<ii", index, number)
        offset = 0
        while (offset := data.find(needle, offset, body_end)) >= 0:
            found.add(offset)
            offset += 1
    if len(found) != 1:
        raise ShopTableError(
            f"Expected one binary FName anchor for row {row_name!r}; found {len(found)}."
        )
    return next(iter(found))


# Read row
def _read_row(
    decoder: _SchemaDecoder,
    *,
    family: str,
    row_name: str,
    row_start: int,
    row_end: int,
    name_map: CookedShopNameMap,
    expected_by_slot: dict[tuple[str, int], ShopProduct],
) -> ShopTableRow:
    reader = decoder.reader
    reader.end = row_end
    reader.position = row_start
    comparison_index, number = reader.unpack("<ii", f"{row_name} row key")
    if name_map.resolve(comparison_index, number) != row_name:
        raise ShopTableError(f"{reader.source}: row key at {row_start} resolved to another FName.")
    row_data = decoder.read_struct("ShopLineupTableRow")
    groups_raw = row_data.values.get("ReplenishInfos")
    if not isinstance(groups_raw, tuple):
        raise ShopTableError(f"{reader.source}: row {row_name} has no ReplenishInfos array.")

    groups: list[ShopReplenishGroup] = []
    next_product_index = 0
    for group_value in groups_raw:
        if not isinstance(group_value, _StructValue):
            raise ShopTableError(f"{reader.source}: malformed ReplenishInfos entry in {row_name}.")
        flag = group_value.values.get("Flag")
        products_raw = group_value.values.get("ProductInfos")
        flag_start, flag_end = group_value.spans.get("flag", (0, 0))
        if isinstance(flag, _StructValue):
            formula_value = flag.values.get("EvaluationFormula", "")
            formula = formula_value if isinstance(formula_value, str) else ""
            flag_raw = reader.data[flag_start:flag_end]
            formula_span = flag.spans.get("evaluationformula")
            formula_offset = formula_span[0] if formula_span is not None else None
            formula_bytes = (
                reader.data[formula_span[0]:formula_span[1]]
                if formula_span is not None else None
            )
        elif flag is None or "flag" in group_value.defaulted_properties:
            formula = ""
            flag_raw = reader.data[flag_start:flag_end]
            formula_offset = None
            formula_bytes = None
        else:
            raise ShopTableError(f"{reader.source}: malformed Flag structure in {row_name}.")
        if not isinstance(products_raw, tuple):
            raise ShopTableError(f"{reader.source}: group in {row_name} has no ProductInfos array.")

        products: list[ShopTableProduct] = []
        for product_value in products_raw:
            if not isinstance(product_value, _StructValue):
                raise ShopTableError(f"{reader.source}: malformed product in {row_name}.")
            path_value = product_value.values.get("ArticleDataPath")
            path_span = product_value.spans.get("articledatapath")
            price_span = product_value.spans.get("price")
            uses_stock_span = product_value.spans.get("bisusestock")
            stock_span = product_value.spans.get("stock")
            if (
                not isinstance(path_value, tuple) or len(path_value) != 5
                or path_span is None or price_span is None
                or uses_stock_span is None or stock_span is None
            ):
                raise ShopTableError(f"{reader.source}: invalid ProductInfo fields in {row_name}.")
            article_path, item_object, subpath, article_path_number, item_object_number = path_value
            if not isinstance(article_path, str) or not article_path.startswith("/Game/"):
                raise ShopTableError(f"{reader.source}: invalid article package path in {row_name}.")
            if not isinstance(item_object, str) or not item_object or subpath:
                raise ShopTableError(f"{reader.source}: unsupported ArticleDataPath in {row_name}.")
            price = product_value.values.get("Price")
            uses_stock_value = product_value.values.get("bIsUseStock")
            stock_value = product_value.values.get("Stock")
            uses_stock = (
                None
                if "bisusestock" in product_value.defaulted_properties
                else uses_stock_value
            )
            stock = None if "stock" in product_value.defaulted_properties else stock_value
            article_path_bytes = reader.data[path_span[0]:path_span[1]]
            price_bytes = reader.data[price_span[0]:price_span[1]]
            uses_stock_bytes = (
                None if "bisusestock" in product_value.defaulted_properties
                else reader.data[uses_stock_span[0]:uses_stock_span[1]]
            )
            stock_bytes = (
                None if "stock" in product_value.defaulted_properties
                else reader.data[stock_span[0]:stock_span[1]]
            )
            if (
                len(article_path_bytes) != 20 or len(price_bytes) != 4
                or (uses_stock_bytes is not None and len(uses_stock_bytes) != 1)
                or (stock_bytes is not None and len(stock_bytes) != 4)
            ):
                raise ShopTableError(
                    f"{reader.source}: unsupported serialized ProductInfo field width in {row_name}."
                )
            if (
                isinstance(price, bool) or not isinstance(price, int) or price < 0
                or (uses_stock is not None and not isinstance(uses_stock, bool))
                or (stock is not None and (isinstance(stock, bool) or not isinstance(stock, int) or stock < 0))
            ):
                raise ShopTableError(f"{reader.source}: unreasonable product field value in {row_name}.")
            evaluation_value = product_value.extra_value
            if isinstance(evaluation_value, _StructValue):
                formula_value = evaluation_value.values.get("EvaluationFormula", "")
                evaluation_formula = formula_value if isinstance(formula_value, str) else ""
            else:
                evaluation_formula = ""
            catalog_product = expected_by_slot.get((row_name, next_product_index))
            if catalog_product is None:
                raise ShopTableError(
                    f"{reader.source}: row {row_name} product {next_product_index} is missing from the catalog."
                )
            products.append(
                ShopTableProduct(
                    family=family,
                    shop_row=row_name,
                    product_index=next_product_index,
                    article_path=article_path,
                    item_object=item_object,
                    article_path_offset=path_span[0],
                    article_path_bytes=article_path_bytes,
                    price=price,
                    price_offset=price_span[0],
                    price_bytes=price_bytes,
                    uses_stock=uses_stock,
                    uses_stock_offset=(
                        uses_stock_span[0] if uses_stock_bytes is not None else None
                    ),
                    uses_stock_bytes=uses_stock_bytes,
                    stock=stock,
                    stock_offset=stock_span[0] if stock_bytes is not None else None,
                    stock_bytes=stock_bytes,
                    important="/item/important/" in article_path.casefold(),
                    evaluation_formula=evaluation_formula,
                    evaluation_raw=product_value.extra_raw,
                    raw_bytes=(
                        product_value.raw[:-len(product_value.extra_raw)]
                        if product_value.extra_raw else product_value.raw
                    ),
                    protected=catalog_product.protected,
                    protection_reason=catalog_product.protection_reason,
                    article_path_number=article_path_number,
                    item_object_number=item_object_number,
                    what_content_class=catalog_product.what_content_class,
                    catalog_uses_stock=catalog_product.uses_stock,
                    catalog_stock=catalog_product.stock,
                    uses_stock_span=uses_stock_span,
                    stock_span=stock_span,
                    raw_struct_start=product_value.header_start,
                    raw_struct_end=product_value.header_start + len(product_value.raw),
                    raw_header_end=product_value.header_end,
                    uses_stock_mask_bit=(product_value.zero_mask_bits or {}).get("bisusestock"),
                    stock_mask_bit=(product_value.zero_mask_bits or {}).get("stock"),
                )
            )
            next_product_index += 1
        groups.append(ShopReplenishGroup(
            formula, flag_raw, tuple(products), formula_offset, formula_bytes,
            flag_start if flag_end > flag_start else None,
        ))

    if reader.position != row_end:
        raise ShopTableError(
            f"{reader.source}: decoded row {row_name} ended at {reader.position}, "
            f"but the next row/footer starts at {row_end}."
        )
    return ShopTableRow(row_name, tuple(groups), reader.data[row_start:row_end])


# Compare products
def _compare_products(
    actual: tuple[ShopTableProduct, ...],
    expected: tuple[ShopProduct, ...],
    family: str,
    *,
    verify_stock: bool = True,
) -> None:
    expected_by_slot: dict[tuple[str, int], ShopProduct] = {}
    for product in expected:
        key = (product.shop_row, product.product_index)
        if key in expected_by_slot:
            raise ShopTableError(f"{family} catalog has a duplicate slot {key!r}.")
        expected_by_slot[key] = product
    actual_by_slot = {(product.shop_row, product.product_index): product for product in actual}
    if len(actual_by_slot) != len(actual):
        raise ShopTableError(f"{family} table has duplicate decoded product indices.")
    if set(actual_by_slot) != set(expected_by_slot):
        raise ShopTableError(
            f"{family} decoded slot identities differ from the catalog "
            f"({len(actual_by_slot)} binary, {len(expected_by_slot)} catalog)."
        )
    for key, source in expected_by_slot.items():
        decoded = actual_by_slot[key]
        fields = [
            ("ArticleDataPath", decoded.article_path, source.original_product_path),
            ("item object", decoded.item_object, source.item_object),
            ("Price", decoded.price, source.price),
        ]
        if source.article_path_number is not None:
            fields.append(("ArticleDataPath FName number", decoded.article_path_number, source.article_path_number))
        if source.item_object_number is not None:
            fields.append(("item object FName number", decoded.item_object_number, source.item_object_number))
        if verify_stock:
            fields.append(("bIsUseStock", decoded.uses_stock, source.uses_stock))
            fields.append(("Stock", decoded.stock, source.stock))
        for label, actual_value, expected_value in fields:
            if actual_value != expected_value:
                raise ShopTableError(
                    f"{family} {key[0]}[{key[1]}] {label} mismatch: "
                    f"binary={actual_value!r}, catalog={expected_value!r}."
                )


# Parse candidate
def _parse_candidate(
    data: bytes,
    *,
    family: str,
    name_map: CookedShopNameMap,
    mappings: UsmapMappings,
    expected: tuple[ShopProduct, ...],
    anchors: dict[str, int],
    body_end: int,
    allow_product_evaluation_extension: bool,
    verify_stock_against_catalog: bool,
) -> ShopTableParseResult:
    ordered = sorted(anchors.items(), key=lambda item: item[1])
    first_anchor = ordered[0][1]
    if first_anchor < 4:
        raise ShopTableError(f"{family} has no row count before its first row key.")
    serialized_row_count = struct.unpack_from("<i", data, first_anchor - 4)[0]
    if serialized_row_count != len(ordered):
        raise ShopTableError(
            f"{family} serialized row count {serialized_row_count} does not match "
            f"the {len(ordered)} catalog rows."
        )

    decoder = _SchemaDecoder(
        data,
        source=family,
        start=first_anchor,
        end=body_end,
        mappings=mappings,
        name_map=name_map,
        allow_product_evaluation_extension=allow_product_evaluation_extension,
    )
    expected_by_slot = {(product.shop_row, product.product_index): product for product in expected}
    rows: list[ShopTableRow] = []
    for position, (row_name, row_start) in enumerate(ordered):
        row_end = ordered[position + 1][1] if position + 1 < len(ordered) else body_end
        rows.append(
            _read_row(
                decoder,
                family=family,
                row_name=row_name,
                row_start=row_start,
                row_end=row_end,
                name_map=name_map,
                expected_by_slot=expected_by_slot,
            )
        )
    result = ShopTableParseResult(
        family=family,
        rows=tuple(rows),
        opaque_prefix=data[:first_anchor],
        footer=data[body_end:],
        has_product_evaluation_extension=decoder.observed_product_evaluation_extension,
    )
    _compare_products(
        result.products,
        expected,
        family,
        verify_stock=verify_stock_against_catalog,
    )
    return result


# Parse shop table
def parse_shop_table(
    data: bytes,
    *,
    family: str,
    name_map: CookedShopNameMap,
    mappings: UsmapMappings,
    expected_products: Iterable[ShopProduct],
    source: str = "<memory>",
    verify_stock_against_catalog: bool = False,
) -> ShopTableParseResult:
    """Decode and verify one table against the trusted runtime shop catalog.

    The row anchors come from the package FName map and catalog identities. Product
    fields come from CV2.usmap. These exports also carry a trailing per-product
    evaluation struct that is absent from the ShopProductInfo mapping; its type is
    derived from ShopReplenishInfo.Flag and its raw bytes are retained. Article paths,
    item names, and prices are always checked against the catalog. Raw stock fields
    remain absent when zero-masked; logical accessors expose their schema defaults
    separately. Catalog stock comparison is opt-in because some catalog values do
    not match cooked serialization.
    """
    normalized_family = family.strip().upper()
    if normalized_family not in SHOP_TABLE_NAMES:
        raise ShopTableError(f"Unknown shop table family {family!r}.")
    expected = tuple(expected_products)
    if not expected or any(product.family.upper() != normalized_family for product in expected):
        raise ShopTableError(f"No consistent catalog products were supplied for {normalized_family}.")
    validate_shop_catalog_references(name_map, normalized_family, expected)
    expected_rows = {product.shop_row for product in expected}
    expected_by_row: dict[str, list[ShopProduct]] = {}
    for product in expected:
        expected_by_row.setdefault(product.shop_row, []).append(product)
    if any(sorted(item.product_index for item in values) != list(range(len(values))) for values in expected_by_row.values()):
        raise ShopTableError(f"{normalized_family} catalog product indices are not contiguous per row.")

    if len(data) < 8 or struct.unpack_from("<I", data, len(data) - 4)[0] != _PACKAGE_TAG:
        raise ShopTableError(f"{source}: missing Unreal package footer in shop table export.")
    body_end = len(data) - 4
    anchors = {
        row: _find_row_anchor(data, name_map, row, body_end)
        for row in expected_rows
    }
    if len(set(anchors.values())) != len(anchors):
        raise ShopTableError(f"{normalized_family} has overlapping row FName anchors.")

    candidates: list[tuple[bool, ShopTableParseResult]] = []
    errors: list[str] = []
    for allow_product_evaluation_extension in (False, True):
        try:
            result = _parse_candidate(
                data,
                family=normalized_family,
                name_map=name_map,
                mappings=mappings,
                expected=expected,
                anchors=anchors,
                body_end=body_end,
                allow_product_evaluation_extension=allow_product_evaluation_extension,
                verify_stock_against_catalog=verify_stock_against_catalog,
            )
        except ShopTableError as error:
            errors.append(str(error))
        else:
            candidates.append((result.has_product_evaluation_extension, result))

    unique: dict[bool, ShopTableParseResult] = {}
    for has_extension, result in candidates:
        unique[has_extension] = result
    if len(unique) != 1:
        if not unique:
            detail = errors[-1] if errors else "no valid layout was found"
            raise ShopTableError(f"{normalized_family} table failed schema/catalog validation: {detail}")
        raise ShopTableError(
            f"{normalized_family} has ambiguous product evaluation layouts: "
            f"{sorted(str(value) for value in unique)}."
        )
    return next(iter(unique.values()))
