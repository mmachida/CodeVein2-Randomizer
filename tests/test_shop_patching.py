from __future__ import annotations

import struct
import shutil
import tempfile
import unittest
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cv2randomizer.game.catalogs import CatalogStore, ShopProduct
from cv2randomizer.game.asset_extraction import MaterializedAsset
from cv2randomizer.game.shop_generation import (
    ParsedShopTable,
    ShopTablesStage,
    _expected_after,
    _validate_mixed_shop_output,
    apply_shop_plan,
)
from cv2randomizer.game.shop_packages import CookedShopNameMap, SHOP_TABLE_NAMES, read_cooked_name_map
from cv2randomizer.game.shop_tables import ShopTableError, parse_shop_table
from cv2randomizer.game.usmap import UsmapMappings, UsmapProperty, UsmapPropertyType, UsmapStruct, read_usmap
from cv2randomizer.patching.shop_tables import ShopPatchError, patch_shop_table
from cv2randomizer.randomizer.shops import (
    ArticleRewardIdentity,
    ShopAssignment,
    ShopRandomizationPlan,
    build_npc_trader_only_plan,
    build_shop_randomization_plan,
    is_npc_trader_row,
    shop_slot_lock_reason,
)

_PACKAGE_TAG = 0x9E2A83C1
_PACKAGE_NAME = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"
_TEST_PATHS = (
    "/Game/Blueprint/Article/Item/DA_SourceA",
    "/Game/Blueprint/Article/Item/DA_SourceB",
)
_TEST_OBJECTS = ("DA_SourceA", "DA_SourceB")
_TEST_ROW = "Shop_Test"
_TEST_NAMES = (*_TEST_PATHS, *_TEST_OBJECTS, _TEST_ROW)


# Read mappings
def _mappings() -> UsmapMappings:
    formula = UsmapStruct(
        "StoryFlagEvaluationFormula", None, 1,
        (UsmapProperty(0, "EvaluationFormula", 1, UsmapPropertyType(10)),),
    )
    product = UsmapStruct(
        "ShopProductInfo", None, 4,
        (
            UsmapProperty(0, "ArticleDataPath", 1, UsmapPropertyType(17)),
            UsmapProperty(1, "Price", 1, UsmapPropertyType(2)),
            UsmapProperty(2, "bIsUseStock", 1, UsmapPropertyType(1)),
            UsmapProperty(3, "Stock", 1, UsmapPropertyType(2)),
        ),
    )
    replenish = UsmapStruct(
        "ShopReplenishInfo", None, 2,
        (
            UsmapProperty(0, "Flag", 1, UsmapPropertyType(9, struct_name=formula.name)),
            UsmapProperty(1, "ProductInfos", 1, UsmapPropertyType(8, inner_type=UsmapPropertyType(9, struct_name=product.name))),
        ),
    )
    row = UsmapStruct(
        "ShopLineupTableRow", None, 1,
        (UsmapProperty(0, "ReplenishInfos", 1, UsmapPropertyType(8, inner_type=UsmapPropertyType(9, struct_name=replenish.name))),),
    )
    structs = {item.name.casefold(): item for item in (formula, product, replenish, row)}
    return UsmapMappings(0, 0, 0, len(structs), structs, 0)


# Resolve name map
def _name_map() -> CookedShopNameMap:
    return CookedShopNameMap(
        package_name=_PACKAGE_NAME,
        package_flags=0,
        total_header_size=0,
        name_offset=0,
        names=_TEST_NAMES,
        is_unversioned=True,
    )


# Read fragment
def _fragment(count: int, *, has_zero_mask: bool = False) -> bytes:
    return struct.pack("<H", (count << 9) | (0x80 if has_zero_mask else 0) | 0x100)


# Read fixture
def _fixture(
    *, mask_second_stock: bool = False, first_stock: int = 1,
    mask_second_quantity: bool = False,
    second_uses_stock: bool = True, second_stock: int = 3,
) -> tuple[bytes, tuple[ShopProduct, ...], CookedShopNameMap, UsmapMappings]:
    specs = ((0, 0, 10, True, first_stock), (1, 1, 250, second_uses_stock, second_stock))
    data = bytearray(struct.pack("<i", 1))
    data.extend(struct.pack("<ii", 4, 0))
    data.extend(_fragment(1))
    data.extend(struct.pack("<i", 1))
    data.extend(_fragment(2))
    data.extend(_fragment(1))
    data.extend(struct.pack("<i", 0))
    data.extend(struct.pack("<i", len(specs)))
    expected = []
    for index, path_index, price, uses_stock, stock in specs:
        mask_flag_and_quantity = mask_second_stock and index == 1
        mask_quantity_only = mask_second_quantity and index == 1
        masked = mask_flag_and_quantity or mask_quantity_only
        data.extend(_fragment(4, has_zero_mask=masked))
        if masked:
            data.extend(b"\x0c" if mask_flag_and_quantity else b"\x08")
        data.extend(struct.pack("<iiii", path_index, 0, path_index + 2, 0))
        data.extend(struct.pack("<i", 0))
        if masked:
            data.extend(struct.pack("<i", price))
            if mask_quantity_only:
                data.extend(struct.pack("<B", int(uses_stock)))
        else:
            data.extend(struct.pack("<iB i", price, int(uses_stock), stock))
        expected.append(
            ShopProduct(
                family="NORMAL",
                shop_row=_TEST_ROW,
                product_index=index,
                original_product_path=_TEST_PATHS[index],
                item_object=_TEST_OBJECTS[index],
                price=price,
                uses_stock=uses_stock,
                stock=stock,
                protected=False,
                protection_reason=None,
            )
        )
    data.extend(struct.pack("<I", _PACKAGE_TAG))
    return bytes(data), tuple(expected), _name_map(), _mappings()


class ShopPatcherTests(unittest.TestCase):
    # Parse package source data
    def _parse(self, data, expected, name_map, mappings):
        return parse_shop_table(
            data,
            family="NORMAL",
            name_map=name_map,
            mappings=mappings,
            expected_products=expected,
            source="synthetic shop table",
        )

    # Check zero masked infinite slot is
    def test_zero_masked_infinite_slot_is_rejected_as_source_and_destination(self) -> None:
        data, expected, name_map, mappings = _fixture(mask_second_stock=True, second_uses_stock=False)
        original_data = data
        parsed = parse_shop_table(
            data,
            family="NORMAL",
            name_map=name_map,
            mappings=mappings,
            expected_products=expected,
            source="synthetic masked-stock table",
            verify_stock_against_catalog=False,
        )
        first, second = parsed.products
        self.assertIsNone(second.uses_stock)
        self.assertIsNone(second.stock)
        self.assertIsNone(second.uses_stock_bytes)
        self.assertIsNone(second.stock_bytes)
        self.assertIs(second.destination_uses_stock, False)
        with self.assertRaisesRegex(ShopPatchError, "source.*INFINITE_SHOP_STOCK"):
            patch_shop_table(data, parsed, (ShopAssignment(first, second),), reparse=lambda output: parsed)
        world_source = ArticleRewardIdentity(
            source_id="world:infinite-destination", canonical_path=first.article_path,
            article_path=first.article_path, item_object=first.item_object,
            article_path_number=0, item_object_number=0,
            origin_kind="WORLD", origin_label="World test source",
        )
        with self.assertRaisesRegex(ShopPatchError, "destination.*INFINITE_SHOP_STOCK"):
            patch_shop_table(
                data, parsed, (ShopAssignment(second, world_source, article_only=True),),
                reparse=lambda output: parsed,
            )
        self.assertEqual(data, original_data)

    # Check finite destination keep exact stock
    def test_finite_destination_keeps_its_exact_stock_quantity(self) -> None:
        data, expected, name_map, mappings = _fixture(first_stock=5, second_uses_stock=True, second_stock=3)
        parsed = self._parse(data, expected, name_map, mappings)
        first, second = parsed.products
        expected_after = (
            replace(expected[0], original_product_path=expected[1].original_product_path, item_object=expected[1].item_object),
            replace(expected[1], original_product_path=expected[0].original_product_path, item_object=expected[0].item_object),
        )
        result = patch_shop_table(
            data, parsed, (ShopAssignment(first, second), ShopAssignment(second, first)),
            reparse=lambda output: self._parse(output, expected_after, name_map, mappings),
        )
        after = {item.product_index: item for item in result.parsed.products}
        self.assertEqual((after[0].uses_stock, after[0].stock), (True, 5))
        self.assertEqual((after[1].uses_stock, after[1].stock), (True, 3))

    # Check move raw payload preserve destination
    def test_moves_raw_payload_and_preserves_destination_prices(self) -> None:
        data, expected, name_map, mappings = _fixture()
        parsed = self._parse(data, expected, name_map, mappings)
        first, second = parsed.products
        self.assertEqual(first.price_offset, first.article_path_offset + 20)
        self.assertEqual(first.uses_stock_offset, first.article_path_offset + 24)
        self.assertEqual(first.stock_offset, first.article_path_offset + 25)
        self.assertEqual((len(first.article_path_bytes), len(first.price_bytes), len(first.uses_stock_bytes), len(first.stock_bytes)), (20, 4, 1, 4))
        assignments = (
            ShopAssignment(destination=first, source=second),
            ShopAssignment(destination=second, source=first),
        )
        expected_after = (
            replace(expected[0], original_product_path=expected[1].original_product_path, item_object=expected[1].item_object),
            replace(expected[1], original_product_path=expected[0].original_product_path, item_object=expected[0].item_object),
        )
        result = patch_shop_table(
            data,
            parsed,
            assignments,
            reparse=lambda output: self._parse(output, expected_after, name_map, mappings),
        )

        self.assertEqual(len(result.data), len(data))
        self.assertEqual(result.changed_slots, (("NORMAL", "Shop_Test", 0), ("NORMAL", "Shop_Test", 1)))
        patched_by_index = {product.product_index: product for product in result.parsed.products}
        self.assertEqual(patched_by_index[0].article_path, second.article_path)
        self.assertEqual(patched_by_index[0].price, first.price)
        self.assertEqual((patched_by_index[0].uses_stock, patched_by_index[0].stock), (first.uses_stock, first.stock))
        self.assertEqual(patched_by_index[1].article_path, first.article_path)
        self.assertEqual(patched_by_index[1].price, second.price)
        self.assertEqual((patched_by_index[1].uses_stock, patched_by_index[1].stock), (second.uses_stock, second.stock))

    # Check catalog protected slots cannot be
    def test_catalog_protected_slots_cannot_be_patched(self) -> None:
        data, expected, name_map, mappings = _fixture()
        protected_expected = (replace(expected[0], protected=True, protection_reason="catalog exclusion"), expected[1])
        parsed = self._parse(data, protected_expected, name_map, mappings)
        first, second = parsed.products
        assignments = (
            ShopAssignment(destination=first, source=second),
            ShopAssignment(destination=second, source=first),
        )
        with self.assertRaisesRegex(ShopPatchError, "locked: CATALOG EXCLUSION"):
            patch_shop_table(data, parsed, assignments, reparse=lambda output: self._parse(output, expected, name_map, mappings))

    # Check article mixed patch preserve destination
    def test_article_only_mixed_patch_preserves_destination_stock_and_other_fields(self) -> None:
        data, expected, name_map, mappings = _fixture()
        parsed = self._parse(data, expected, name_map, mappings)
        first, second = parsed.products
        first_source = ArticleRewardIdentity(
            source_id="world:1", canonical_path=first.article_path,
            article_path=first.article_path, item_object=first.item_object,
            article_path_number=0, item_object_number=0,
            origin_kind="WORLD", origin_label="Chest A",
        )
        second_source = ArticleRewardIdentity(
            source_id="world:2", canonical_path=second.article_path,
            article_path=second.article_path, item_object=second.item_object,
            article_path_number=0, item_object_number=0,
            origin_kind="WORLD", origin_label="Chest B",
        )
        assignments = (
            ShopAssignment(destination=first, source=second_source, article_only=True),
            ShopAssignment(destination=second, source=first_source, article_only=True),
        )
        expected_after = (
            replace(expected[0], original_product_path=second.article_path, item_object=second.item_object),
            replace(expected[1], original_product_path=first.article_path, item_object=first.item_object),
        )
        encoded = {}
        for destination, source in ((first, second_source), (second, first_source)):
            encoded[("NORMAL", destination.shop_row, destination.product_index)] = struct.pack(
                "<iiiii", name_map.names.index(source.article_path), source.article_path_number,
                name_map.names.index(source.item_object), source.item_object_number, 0,
            )
        result = patch_shop_table(
            data,
            parsed,
            assignments,
            reparse=lambda output: self._parse(output, expected_after, name_map, mappings),
            encoded_article_path_bytes=encoded,
        )
        after_by_index = {item.product_index: item for item in result.parsed.products}
        self.assertEqual(after_by_index[0].article_path, second.article_path)
        self.assertEqual(after_by_index[0].price_bytes, first.price_bytes)
        self.assertEqual(after_by_index[0].uses_stock_bytes, first.uses_stock_bytes)
        self.assertEqual(after_by_index[0].stock_bytes, first.stock_bytes)
        self.assertEqual(after_by_index[0].evaluation_raw, first.evaluation_raw)
        self.assertEqual(after_by_index[1].uses_stock_bytes, second.uses_stock_bytes)
        self.assertEqual(after_by_index[1].stock_bytes, second.stock_bytes)
        for original, after in ((first, after_by_index[0]),):
            self.assertEqual(result.data[original.price_offset:original.price_offset + 4], original.price_bytes)
            self.assertEqual(result.data[original.uses_stock_offset:original.uses_stock_offset + 1], original.uses_stock_bytes)
            self.assertEqual(result.data[original.stock_offset:original.stock_offset + 4], original.stock_bytes)

    # Check special shop items set stock
    def test_article_only_stock_override_sets_one_and_preserves_other_fields(self) -> None:
        data, expected, name_map, mappings = _fixture(first_stock=4, second_stock=3)
        parsed = self._parse(data, expected, name_map, mappings)
        destination, source_product = parsed.products
        source = ArticleRewardIdentity(
            source_id="world:booster",
            canonical_path=source_product.article_package_path,
            article_path=source_product.article_path,
            item_object=source_product.item_object,
            article_path_number=source_product.article_path_number,
            item_object_number=source_product.item_object_number,
            origin_kind="WORLD",
            origin_label="Booster Dex",
        )
        assignment = ShopAssignment(
            destination=destination,
            source=source,
            article_only=True,
            stock_override=1,
        )
        expected_after = _expected_after(expected, (assignment,))
        self.assertEqual(expected_after[0].stock, 1)
        encoded = {
            ("NORMAL", destination.shop_row, destination.product_index): struct.pack(
                "<iiiii", name_map.names.index(source.article_path), source.article_path_number,
                name_map.names.index(source.item_object), source.item_object_number, 0,
            ),
        }

        result = patch_shop_table(
            data,
            parsed,
            (assignment,),
            reparse=lambda output: self._parse(output, expected_after, name_map, mappings),
            encoded_article_path_bytes=encoded,
        )

        patched = {item.product_index: item for item in result.parsed.products}
        table = ParsedShopTable(
            "NORMAL", "DT_ShopLineup", _PACKAGE_NAME,
            MaterializedAsset(_PACKAGE_NAME, ()), data, name_map, parsed, expected,
        )
        _validate_mixed_shop_output(table, result.parsed, (assignment,))
        self.assertEqual(patched[0].stock, 1)
        self.assertEqual(patched[0].stock_bytes, struct.pack("<i", 1))
        self.assertEqual(patched[0].price, destination.price)
        self.assertEqual(patched[0].uses_stock, destination.uses_stock)
        self.assertEqual(patched[1].stock, source_product.stock)

    # Check linked alternate assignment is physically
    def test_linked_alternate_assignment_is_physically_patched_without_pool_assignment(self) -> None:
        data, expected, name_map, mappings = _fixture()
        parsed = self._parse(data, expected, name_map, mappings)
        destination, source_product = parsed.products
        source = ArticleRewardIdentity(
            source_id="linked-jail-stinger:josee-shop-alternate",
            canonical_path=source_product.article_path,
            article_path=source_product.article_path,
            item_object=source_product.item_object,
            article_path_number=0,
            item_object_number=0,
            origin_kind="LINKED_WORLD_REWARD",
            origin_label="Josee's Outpost Stinger alternate route",
        )
        with tempfile.TemporaryDirectory(prefix="cv2-linked-shop-route-") as temporary:
            root = Path(temporary)
            content = root / "source" / "Content"
            header = content / "Blueprint" / "GameSystem" / "Shop" / "DT_ShopLineup.uasset"
            export = header.with_suffix(".uexp")
            header.parent.mkdir(parents=True)
            header.write_bytes(b"fixture header")
            export.write_bytes(data)
            virtual = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"
            table = ParsedShopTable(
                "NORMAL", "DT_ShopLineup", virtual,
                MaterializedAsset(virtual, (header, export)), data, name_map, parsed, expected,
            )
            stage = ShopTablesStage(content, (table,))
            plan = ShopRandomizationPlan(
                assignments=(), locked_slots=(), unvalidated_slots=(), disabled_slots=(),
                requires_name_map_preparation=True,
                linked_assignments=(ShopAssignment(destination, source, article_only=True),),
            )
            mapping_path = root / "CV2.usmap"
            mapping_path.write_bytes(b"test-only mapping marker")
            with (
                patch("cv2randomizer.game.shop_generation.read_usmap", return_value=mappings),
                patch("cv2randomizer.game.shop_generation.read_cooked_name_map", return_value=name_map),
            ):
                result = apply_shop_plan(
                    stage,
                    plan,
                    CatalogStore(_LOCAL_ROOT / "data" / "catalogs").load("current"),
                    output_content_root=root / "staging" / "Content",
                    mappings_path=mapping_path,
                )

            self.assertEqual(result.assignment_count, 1)
            self.assertEqual(result.changed_slot_count, 1)
            self.assertEqual(len(result.outputs), 1)
            after = {item.product_index: item for item in result.outputs[0].parsed.products}
            self.assertEqual(after[destination.product_index].article_path, source.article_path)
            self.assertEqual(after[destination.product_index].price_bytes, destination.price_bytes)
            self.assertEqual(after[destination.product_index].uses_stock_bytes, destination.uses_stock_bytes)
            self.assertEqual(after[destination.product_index].stock_bytes, destination.stock_bytes)

    # Check zero masked boolean default is
    def test_zero_masked_boolean_default_is_infinite_and_rejected(self) -> None:
        data, expected, name_map, mappings = _fixture(mask_second_stock=True)
        expected_unknown = (expected[0], replace(expected[1], uses_stock=None, stock=None))
        parsed = parse_shop_table(
            data, family="NORMAL", name_map=name_map, mappings=mappings,
            expected_products=expected_unknown, source="unknown stock table",
            verify_stock_against_catalog=False,
        )
        first, second = parsed.products
        self.assertIs(second.destination_uses_stock, False)
        source = ArticleRewardIdentity(
            source_id="world:unknown-stock-test",
            canonical_path=first.article_path,
            article_path=first.article_path,
            item_object=first.item_object,
            article_path_number=first.article_path_number,
            item_object_number=first.item_object_number,
            origin_kind="WORLD",
            origin_label="world test source",
        )
        with self.assertRaisesRegex(ShopPatchError, "destination.*INFINITE_SHOP_STOCK"):
            patch_shop_table(
                data, parsed, (ShopAssignment(second, source, article_only=True),),
                reparse=lambda output: parsed,
            )

    # Check zero masked quantity true flag
    def test_zero_masked_quantity_with_true_flag_remains_unknown(self) -> None:
        data, expected, name_map, mappings = _fixture(mask_second_quantity=True)
        expected_unknown = (expected[0], replace(expected[1], stock=0))
        parsed = parse_shop_table(
            data, family="NORMAL", name_map=name_map, mappings=mappings,
            expected_products=expected_unknown, source="zero-masked quantity table",
            verify_stock_against_catalog=False,
        )
        first, second = parsed.products
        self.assertIs(second.logical_uses_stock, True)
        self.assertEqual(second.logical_stock, 0)
        self.assertIsNone(second.stock_bytes)
        self.assertEqual(shop_slot_lock_reason(second), "UNKNOWN_SHOP_STOCK_SEMANTICS")
        source = ArticleRewardIdentity(
            source_id="world:masked-quantity-test",
            canonical_path=first.article_path,
            article_path=first.article_path,
            item_object=first.item_object,
            article_path_number=first.article_path_number,
            item_object_number=first.item_object_number,
            origin_kind="WORLD",
            origin_label="world test source",
        )
        with self.assertRaisesRegex(ShopPatchError, "destination.*UNKNOWN_SHOP_STOCK_SEMANTICS"):
            patch_shop_table(
                data, parsed, (ShopAssignment(second, source, article_only=True),),
                reparse=lambda output: parsed,
            )

    # Check article trader payload assignments can
    def test_article_only_and_trader_payload_assignments_can_share_one_table_patch(self) -> None:
        data, expected, name_map, mappings = _fixture()
        parsed = self._parse(data, expected, name_map, mappings)
        first, second = parsed.products
        world_source = ArticleRewardIdentity(
            source_id="world:separate-pool", canonical_path=second.article_path,
            article_path=second.article_path, item_object=second.item_object,
            article_path_number=0, item_object_number=0,
            origin_kind="WORLD", origin_label="Chest",
        )
        assignments = (
            ShopAssignment(first, world_source, article_only=True),
            ShopAssignment(second, first),
        )
        expected_after = (
            replace(expected[0], original_product_path=second.article_path, item_object=second.item_object),
            replace(expected[1], original_product_path=first.article_path, item_object=first.item_object,
                    uses_stock=first.uses_stock, stock=first.stock),
        )
        encoded = {
            ("NORMAL", first.shop_row, first.product_index): struct.pack(
                "<iiiii", name_map.names.index(second.article_path), 0,
                name_map.names.index(second.item_object), 0, 0,
            ),
        }
        result = patch_shop_table(
            data, parsed, assignments,
            reparse=lambda output: self._parse(output, expected_after, name_map, mappings),
            allow_cross_family=True,
            encoded_article_path_bytes=encoded,
        )
        after = {item.product_index: item for item in result.parsed.products}
        self.assertEqual(after[0].article_path, second.article_path)
        self.assertEqual((after[0].uses_stock, after[0].stock), (first.uses_stock, first.stock))
        self.assertEqual(after[1].article_path, first.article_path)
        self.assertEqual((after[1].uses_stock, after[1].stock), (second.uses_stock, second.stock))
        self.assertEqual(after[0].evaluation_raw, first.evaluation_raw)
        self.assertEqual(after[1].evaluation_raw, second.evaluation_raw)

    # Check refuses stale input non permutation
    def test_refuses_stale_input_or_non_permutation(self) -> None:
        data, expected, name_map, mappings = _fixture()
        parsed = self._parse(data, expected, name_map, mappings)
        first, second = parsed.products
        assignments = (
            ShopAssignment(destination=first, source=second),
            ShopAssignment(destination=second, source=first),
        )
        expected_after = (
            replace(expected[0], original_product_path=expected[1].original_product_path, item_object=expected[1].item_object, uses_stock=expected[1].uses_stock, stock=expected[1].stock),
            replace(expected[1], original_product_path=expected[0].original_product_path, item_object=expected[0].item_object, uses_stock=expected[0].uses_stock, stock=expected[0].stock),
        )
        reparse = lambda output: self._parse(output, expected_after, name_map, mappings)
        stale = bytearray(data)
        stale[first.price_offset] ^= 1
        with self.assertRaisesRegex(ShopPatchError, "Destination Price"):
            patch_shop_table(bytes(stale), parsed, assignments, reparse=reparse)
        with self.assertRaisesRegex(ShopPatchError, "permutation"):
            patch_shop_table(data, parsed, assignments[:1], reparse=reparse)


_LOCAL_ROOT = Path(__file__).resolve().parents[1]
_LOCAL_ASSETS = _LOCAL_ROOT / "workspace" / "research_assets" / "shop"
_LOCAL_MAPPING = _LOCAL_ROOT / "workspace" / "research_assets" / "CV2.usmap"
_LOCAL_FIXTURES_AVAILABLE = _LOCAL_MAPPING.is_file() and all(
    (_LOCAL_ASSETS / f"{name}.{extension}").is_file()
    for name in SHOP_TABLE_NAMES.values()
    for extension in ("uasset", "uexp")
)


@unittest.skipUnless(_LOCAL_FIXTURES_AVAILABLE, "local shop research assets are not staged")
class LocalShopPatcherTests(unittest.TestCase):
    # Read normal stage
    def _normal_stage(self, work_root: Path):
        catalogs = CatalogStore(_LOCAL_ROOT / "data" / "catalogs").load("current")
        mappings = read_usmap(_LOCAL_MAPPING)
        family = "NORMAL"
        table_name = SHOP_TABLE_NAMES[family]
        virtual = f"/Game/Blueprint/GameSystem/Shop/{table_name}"
        content_root = work_root / "source" / "Content"
        directory = content_root / "Blueprint" / "GameSystem" / "Shop"
        directory.mkdir(parents=True, exist_ok=True)
        header = directory / f"{table_name}.uasset"
        export = directory / f"{table_name}.uexp"
        shutil.copyfile(_LOCAL_ASSETS / header.name, header)
        shutil.copyfile(_LOCAL_ASSETS / export.name, export)
        expected = tuple(item for item in catalogs.shops if item.family == family)
        name_map = read_cooked_name_map(header, expected_package_name=virtual)
        raw = export.read_bytes()
        parsed = parse_shop_table(
            raw, family=family, name_map=name_map, mappings=mappings,
            expected_products=expected, source=str(export), verify_stock_against_catalog=False,
        )
        table = ParsedShopTable(
            family, table_name, virtual, MaterializedAsset(virtual, (header, export)),
            raw, name_map, parsed, expected,
        )
        return catalogs, ShopTablesStage(content_root, (table,)), table

    # Check shop exclude infinite slots preserve
    def test_shop_only_excludes_infinite_slots_and_preserves_tool_shop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cv2-shop-stock-only-") as temporary:
            work_root = Path(temporary)
            catalogs, stage, table = self._normal_stage(work_root)
            plan = build_shop_randomization_plan(table.parsed.products, "shop-stock-eligibility")
            infinite_slots = {
                item for item in table.parsed.products
                if shop_slot_lock_reason(item) == "INFINITE_SHOP_STOCK"
            }
            infinite = next(iter(infinite_slots))
            assigned_destinations = {item.destination for item in plan.assignments}
            assigned_sources = {item.source for item in plan.assignments if not item.article_only}
            self.assertTrue(infinite_slots.isdisjoint(assigned_destinations))
            self.assertTrue(infinite_slots.isdisjoint(assigned_sources))
            finite = next(item for item in table.parsed.products if item in assigned_destinations)
            tool_before = {
                (item.shop_row, item.product_index): item
                for item in table.parsed.products
                if item.protection_reason == "MAGMELL_TOOL_SHOP"
            }
            self.assertTrue(tool_before)
            self.assertEqual(len(tool_before), 56)
            self.assertTrue(all(item.protected and item.protection_reason == "MAGMELL_TOOL_SHOP"
                                for item in tool_before.values()))
            result = apply_shop_plan(
                stage, plan, catalogs,
                output_content_root=work_root / "staging" / "Content",
                mappings_path=_LOCAL_MAPPING,
            )
            output = result.outputs[0]
            after_by_slot = {
                (item.shop_row, item.product_index): item for item in output.parsed.products
            }
            infinite_after = after_by_slot[(infinite.shop_row, infinite.product_index)]
            finite_after = after_by_slot[(finite.shop_row, finite.product_index)]
            self.assertEqual(output.export_path.stat().st_size, len(table.raw_export))
            self.assertEqual(
                (infinite_after.article_path_bytes, infinite_after.price_bytes,
                 infinite_after.uses_stock_bytes, infinite_after.stock_bytes,
                 infinite_after.evaluation_raw, infinite_after.raw_bytes),
                (infinite.article_path_bytes, infinite.price_bytes,
                 infinite.uses_stock_bytes, infinite.stock_bytes,
                 infinite.evaluation_raw, infinite.raw_bytes),
            )
            self.assertEqual((finite_after.uses_stock, finite_after.stock), (finite.uses_stock, finite.stock))
            self.assertEqual((finite_after.uses_stock_bytes, finite_after.stock_bytes),
                             (finite.uses_stock_bytes, finite.stock_bytes))
            self.assertTrue(output.header_path.is_file())
            self.assertEqual(output.header_path.read_bytes(), table.asset.primary_file.read_bytes())
            self.assertEqual(set(tool_before), {
                (item.shop_row, item.product_index) for item in output.parsed.products
                if (item.shop_row, item.product_index) in tool_before
            })
            for key, before in tool_before.items():
                after = after_by_slot[key]
                self.assertEqual(
                    (after.article_path_bytes, after.price_bytes, after.uses_stock_bytes,
                     after.stock_bytes, after.evaluation_raw, after.raw_bytes),
                    (before.article_path_bytes, before.price_bytes, before.uses_stock_bytes,
                     before.stock_bytes, before.evaluation_raw, before.raw_bytes),
                )

    # Check shop world article mix preserve
    def test_shop_world_article_mix_preserves_finite_destination_stock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cv2-shop-world-stock-") as temporary:
            work_root = Path(temporary)
            catalogs, stage, table = self._normal_stage(work_root)
            finite = next(
                item for item in table.parsed.products
                if shop_slot_lock_reason(item) is None
            )
            world_reward = next(
                reward
                for location in catalogs.world_loot
                for reward in location.rewards
                if reward.article_path not in table.name_map.names
                and reward.article_path.rsplit("/", 1)[-1] not in table.name_map.names
            )
            object_name = world_reward.article_path.rsplit("/", 1)[-1]
            source = ArticleRewardIdentity(
                source_id="world:finite-stock-test",
                canonical_path=world_reward.article_path,
                article_path=world_reward.article_path,
                item_object=object_name,
                article_path_number=0,
                item_object_number=0,
                origin_kind="WORLD",
                origin_label="catalogued World Loot source",
            )
            plan = ShopRandomizationPlan(
                assignments=(ShopAssignment(finite, source, article_only=True),),
                locked_slots=(), unvalidated_slots=(), disabled_slots=(),
                mix_shops_with_world=True, available_families=("NORMAL",),
                global_world_locations=1, global_shop_locations=1, global_candidate_count=2,
            )
            result = apply_shop_plan(
                stage, plan, catalogs,
                output_content_root=work_root / "staging" / "Content",
                mappings_path=_LOCAL_MAPPING,
            )
            output = result.outputs[0]
            after = next(
                item for item in output.parsed.products
                if (item.shop_row, item.product_index) == (finite.shop_row, finite.product_index)
            )
            output_map = read_cooked_name_map(output.header_path, expected_package_name=table.virtual_path)
            self.assertEqual(output.export_path.stat().st_size, len(table.raw_export))
            self.assertEqual((after.article_path, after.item_object), (source.article_path, source.item_object))
            self.assertEqual((after.uses_stock, after.stock), (finite.uses_stock, finite.stock))
            self.assertEqual((after.uses_stock_bytes, after.stock_bytes),
                             (finite.uses_stock_bytes, finite.stock_bytes))
            self.assertEqual(after.price_bytes, finite.price_bytes)
            self.assertEqual(after.evaluation_raw, finite.evaluation_raw)
            self.assertIn(source.article_path, output_map.names)
            self.assertIn(source.item_object, output_map.names)

    # Check article world sources reopen shop
    def test_article_only_world_sources_reopen_with_shop_metadata_unchanged(self) -> None:
        catalogs = CatalogStore(_LOCAL_ROOT / "data" / "catalogs").load("current")
        mappings = read_usmap(_LOCAL_MAPPING)
        family = "NORMAL"
        table_name = SHOP_TABLE_NAMES[family]
        virtual = f"/Game/Blueprint/GameSystem/Shop/{table_name}"
        with tempfile.TemporaryDirectory(prefix="cv2-shop-world-article-only-") as temporary:
            work_root = Path(temporary)
            content_root = work_root / "source" / "Content"
            directory = content_root / "Blueprint" / "GameSystem" / "Shop"
            directory.mkdir(parents=True, exist_ok=True)
            header = directory / f"{table_name}.uasset"
            export = directory / f"{table_name}.uexp"
            shutil.copyfile(_LOCAL_ASSETS / header.name, header)
            shutil.copyfile(_LOCAL_ASSETS / export.name, export)
            expected = tuple(product for product in catalogs.shops if product.family == family)
            name_map = read_cooked_name_map(header, expected_package_name=virtual)
            raw = export.read_bytes()
            parsed = parse_shop_table(
                raw, family=family, name_map=name_map, mappings=mappings,
                expected_products=expected, source=str(export), verify_stock_against_catalog=False,
            )
            first, second = next(
                (left, right) for index, left in enumerate(parsed.products)
                for right in parsed.products[index + 1:]
                if (left.article_path, left.item_object) != (right.article_path, right.item_object)
            )
            source_first = ArticleRewardIdentity(
                "world:slot-a", first.article_path, first.article_path, first.item_object,
                first.article_path_number, first.item_object_number, "WORLD", "World chest A",
            )
            source_second = ArticleRewardIdentity(
                "world:slot-b", second.article_path, second.article_path, second.item_object,
                second.article_path_number, second.item_object_number, "WORLD", "World chest B",
            )
            plan = ShopRandomizationPlan(
                assignments=(
                    ShopAssignment(first, source_second, article_only=True),
                    ShopAssignment(second, source_first, article_only=True),
                ),
                locked_slots=(), unvalidated_slots=(), disabled_slots=(),
                mix_shops_with_world=True,
                available_families=(family,),
                global_world_locations=2,
                global_shop_locations=2,
                global_candidate_count=4,
            )
            stage = ShopTablesStage(
                content_root,
                (ParsedShopTable(
                    family, table_name, virtual,
                    MaterializedAsset(virtual, (header, export)), raw, name_map,
                    parsed, expected,
                ),),
            )
            result = apply_shop_plan(
                stage, plan, catalogs,
                output_content_root=work_root / "staging" / "Content",
                mappings_path=_LOCAL_MAPPING,
            )
            self.assertEqual(result.changed_slot_count, 2)
            after = result.outputs[0].parsed.products
            after_by_slot = {(item.shop_row, item.product_index): item for item in after}
            patched_first = after_by_slot[(first.shop_row, first.product_index)]
            patched_second = after_by_slot[(second.shop_row, second.product_index)]
            self.assertEqual(patched_first.article_path, second.article_path)
            self.assertEqual(patched_first.price_bytes, first.price_bytes)
            self.assertEqual(patched_first.uses_stock_bytes, first.uses_stock_bytes)
            self.assertEqual(patched_first.stock_bytes, first.stock_bytes)
            self.assertEqual(patched_second.price_bytes, second.price_bytes)
            self.assertEqual(patched_second.uses_stock_bytes, second.uses_stock_bytes)
            self.assertEqual(patched_second.stock_bytes, second.stock_bytes)

    # Check patches reparses safe slots all
    def test_patches_and_reparses_safe_slots_in_all_five_families(self) -> None:
        from cv2randomizer.game.catalogs import CatalogStore

        mappings = read_usmap(_LOCAL_MAPPING)
        catalogs = CatalogStore(_LOCAL_ROOT / "data" / "catalogs").load("current")
        parsed_tables = {}
        name_maps = {}
        source_bytes = {}
        for family, table in SHOP_TABLE_NAMES.items():
            expected = tuple(
                replace(item, what_content_class="BASE")
                for item in catalogs.shops if item.family == family
            )
            name_map = read_cooked_name_map(
                _LOCAL_ASSETS / f"{table}.uasset",
                expected_package_name=f"/Game/Blueprint/GameSystem/Shop/{table}",
            )
            data = (_LOCAL_ASSETS / f"{table}.uexp").read_bytes()
            parsed = parse_shop_table(
                data,
                family=family,
                name_map=name_map,
                mappings=mappings,
                expected_products=expected,
                verify_stock_against_catalog=False,
            )
            parsed_tables[family] = (parsed, expected)
            name_maps[family] = name_map
            source_bytes[family] = data

        plan = build_shop_randomization_plan(
            (product for parsed, _ in parsed_tables.values() for product in parsed.products),
            "phase7-integration",
        )
        self.assertEqual(len(plan.unvalidated_slots), 0)
        all_products = tuple(item for parsed, _ in parsed_tables.values() for item in parsed.products)
        self.assertEqual(sum(shop_slot_lock_reason(item) == "MAGMELL_TOOL_SHOP" for item in plan.locked_slots), 56)
        for reason in ("INFINITE_SHOP_STOCK", "UNKNOWN_SHOP_STOCK_SEMANTICS"):
            self.assertEqual(
                sum(shop_slot_lock_reason(item) == reason for item in plan.locked_slots + plan.unvalidated_slots),
                sum(shop_slot_lock_reason(item) == reason for item in all_products),
            )
        self.assertEqual(sum(item.protection_reason == "MAGMELL_TOOL_SHOP" for item in all_products), 56)
        self.assertTrue(plan.assignments)
        self.assertEqual(sum(item.destination.destination_uses_stock is False for item in plan.assignments), 0)
        by_family = {}
        for assignment in plan.assignments:
            by_family.setdefault(assignment.destination.family, []).append(assignment)

        changed_count = 0
        for family, table in SHOP_TABLE_NAMES.items():
            parsed, catalog_products = parsed_tables[family]
            assignments = tuple(by_family.get(family, ()))
            assigned = {(item.destination.shop_row, item.destination.product_index): item for item in assignments}
            catalog_by_key = {(item.shop_row, item.product_index): item for item in catalog_products}
            expected_after = tuple(
                replace(
                    catalog_product,
                    original_product_path=assigned[key].source.article_path,
                    item_object=assigned[key].source.item_object,
                ) if key in assigned else catalog_product
                for key, catalog_product in sorted(catalog_by_key.items())
            )
            result = patch_shop_table(
                source_bytes[family],
                parsed,
                assignments,
                reparse=lambda output, family=family, expected_after=expected_after: parse_shop_table(
                    output,
                    family=family,
                    name_map=name_maps[family],
                    mappings=mappings,
                    expected_products=expected_after,
                    verify_stock_against_catalog=False,
                    source=f"patched {table}.uexp",
                ),
            )
            self.assertEqual(len(result.data), len(source_bytes[family]))
            self.assertEqual(result.parsed.family, family)
            changed_count += len(result.changed_slots)
        self.assertGreater(changed_count, 0)

    # Check global mix crosses compatible shop
    def test_global_mix_crosses_only_compatible_shop_family_pairs_and_reopens(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        catalogs = CatalogStore(repository_root / "data" / "catalogs").load("current")
        mappings = read_usmap(_LOCAL_MAPPING)
        with tempfile.TemporaryDirectory(prefix="cv2-shop-mix-") as temporary:
            work_root = Path(temporary)
            content_root = work_root / "source" / "Content"
            parsed_tables = []
            products = []
            for family, table_name in SHOP_TABLE_NAMES.items():
                virtual = f"/Game/Blueprint/GameSystem/Shop/{table_name}"
                directory = content_root / "Blueprint" / "GameSystem" / "Shop"
                directory.mkdir(parents=True, exist_ok=True)
                header = directory / f"{table_name}.uasset"
                export = directory / f"{table_name}.uexp"
                shutil.copyfile(_LOCAL_ASSETS / header.name, header)
                shutil.copyfile(_LOCAL_ASSETS / export.name, export)
                expected = tuple(
                    replace(item, what_content_class="BASE")
                    for item in catalogs.shops if item.family == family
                )
                name_map = read_cooked_name_map(header, expected_package_name=virtual)
                raw = export.read_bytes()
                parsed = parse_shop_table(
                    raw, family=family, name_map=name_map, mappings=mappings,
                    expected_products=expected, source=str(export), verify_stock_against_catalog=False,
                )
                parsed_tables.append(ParsedShopTable(
                    family, table_name, virtual, MaterializedAsset(virtual, (header, export)),
                    raw, name_map, parsed, expected,
                ))
                products.extend(parsed.products)

            plan = build_shop_randomization_plan(
                products, "shop-cross-family-reopen", mix_postgame_dlc_with_base=True,
            )
            self.assertTrue(any(item.destination.family != item.source.family for item in plan.assignments))
            for item in plan.assignments:
                allowed = {
                    "NORMAL": {"NORMAL", "DLC"},
                    "DLC": {"NORMAL", "DLC"},
                    "TRADE": {"TRADE", "TRADE_DLC"},
                    "TRADE_DLC": {"TRADE", "TRADE_DLC"},
                    "LUXURY": {"LUXURY"},
                }
                self.assertIn(item.source.family, allowed[item.destination.family])

            result = apply_shop_plan(
                ShopTablesStage(content_root, tuple(parsed_tables)),
                plan,
                catalogs,
                output_content_root=work_root / "staging" / "Content",
                mappings_path=_LOCAL_MAPPING,
            )
            self.assertTrue(result.outputs)
            for output in result.outputs:
                self.assertTrue(output.header_path.is_file())
                self.assertTrue(output.export_path.is_file())

    # Check NPC trader reopens cross family
    def test_npc_trader_only_reopens_cross_family_trade_payloads(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        catalogs = CatalogStore(repository_root / "data" / "catalogs").load("current")
        mappings = read_usmap(_LOCAL_MAPPING)
        with tempfile.TemporaryDirectory(prefix="cv2-trader-cross-family-") as temporary:
            work_root = Path(temporary)
            content_root = work_root / "source" / "Content"
            parsed_tables = []
            traders = []
            for family in ("TRADE", "TRADE_DLC"):
                table_name = SHOP_TABLE_NAMES[family]
                virtual = f"/Game/Blueprint/GameSystem/Shop/{table_name}"
                directory = content_root / "Blueprint" / "GameSystem" / "Shop"
                directory.mkdir(parents=True, exist_ok=True)
                header = directory / f"{table_name}.uasset"
                export = directory / f"{table_name}.uexp"
                shutil.copyfile(_LOCAL_ASSETS / header.name, header)
                shutil.copyfile(_LOCAL_ASSETS / export.name, export)
                expected = tuple(item for item in catalogs.shops if item.family == family)
                name_map = read_cooked_name_map(header, expected_package_name=virtual)
                raw = export.read_bytes()
                parsed = parse_shop_table(
                    raw, family=family, name_map=name_map, mappings=mappings,
                    expected_products=expected, source=str(export), verify_stock_against_catalog=False,
                )
                parsed_tables.append(ParsedShopTable(
                    family, table_name, virtual, MaterializedAsset(virtual, (header, export)),
                    raw, name_map, parsed, expected,
                ))
                traders.extend(
                    product for product in parsed.products
                    if is_npc_trader_row(product.family, product.shop_row)
                )

            plan = build_npc_trader_only_plan(traders, "npc-trader-cross-family-reopen")
            self.assertEqual(len(traders), 423)
            self.assertTrue(plan.requires_name_map_preparation)
            self.assertTrue(any(
                assignment.destination.family != assignment.source.family
                for assignment in plan.assignments
            ))
            assigned_destinations = {
                (item.destination.family, item.destination.shop_row, item.destination.product_index)
                for item in plan.assignments
            }
            protected_formula_slots = (
                ("TRADE", "Character.Type.Buddy.Noah", 32),
                ("TRADE", "Character.Type.Buddy.Lyle", 32),
                ("TRADE_DLC", "Character.Type.Buddy.Liv", 12),
            )
            original_groups = {}
            for table in parsed_tables:
                for row in table.parsed.rows:
                    for group in row.groups:
                        for product in group.products:
                            original_groups[(product.family, row.name, product.product_index)] = group
            for key in protected_formula_slots:
                self.assertIn(key, assigned_destinations)
                self.assertTrue(original_groups[key].evaluation_raw)
            self.assertIn("CharacterEpisode", original_groups[protected_formula_slots[0]].evaluation_formula)
            self.assertIn("MainEpisode", original_groups[protected_formula_slots[1]].evaluation_formula)
            self.assertIn(">=  32", original_groups[protected_formula_slots[2]].evaluation_formula)
            result = apply_shop_plan(
                ShopTablesStage(content_root, tuple(parsed_tables)),
                plan,
                catalogs,
                output_content_root=work_root / "staging" / "Content",
                mappings_path=_LOCAL_MAPPING,
            )
            self.assertEqual({output.family for output in result.outputs}, {"TRADE", "TRADE_DLC"})
            for output in result.outputs:
                self.assertTrue(output.header_path.is_file())
                self.assertTrue(output.export_path.is_file())
                self.assertEqual(len(output.parsed.products), 392 if output.family == "TRADE" else 31)
                after_by_slot = {
                    (item.family, item.shop_row, item.product_index): item
                    for item in output.parsed.products
                }
                for key in protected_formula_slots:
                    if key[0] == output.family:
                        after_group = next(
                            group for row in output.parsed.rows if row.name == key[1]
                            for group in row.groups
                            if any(product.product_index == key[2] for product in group.products)
                        )
                        self.assertEqual(after_group.evaluation_raw, original_groups[key].evaluation_raw)

    # Check unlock NPC trader tiers opens
    def test_unlock_npc_trader_tiers_opens_all_base_and_dlc_affinity_groups(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        catalogs = CatalogStore(repository_root / "data" / "catalogs").load("current")
        mappings = read_usmap(_LOCAL_MAPPING)
        with tempfile.TemporaryDirectory(prefix="cv2-trader-unlock-") as temporary:
            work_root = Path(temporary)
            content_root = work_root / "source" / "Content"
            parsed_tables = []
            for family in ("TRADE", "TRADE_DLC"):
                table_name = SHOP_TABLE_NAMES[family]
                virtual = f"/Game/Blueprint/GameSystem/Shop/{table_name}"
                directory = content_root / "Blueprint" / "GameSystem" / "Shop"
                directory.mkdir(parents=True, exist_ok=True)
                header = directory / f"{table_name}.uasset"
                export = directory / f"{table_name}.uexp"
                shutil.copyfile(_LOCAL_ASSETS / header.name, header)
                shutil.copyfile(_LOCAL_ASSETS / export.name, export)
                expected = tuple(item for item in catalogs.shops if item.family == family)
                name_map = read_cooked_name_map(header, expected_package_name=virtual)
                raw = export.read_bytes()
                parsed = parse_shop_table(
                    raw, family=family, name_map=name_map, mappings=mappings,
                    expected_products=expected, source=str(export), verify_stock_against_catalog=False,
                )
                parsed_tables.append(ParsedShopTable(
                    family, table_name, virtual, MaterializedAsset(virtual, (header, export)),
                    raw, name_map, parsed, expected,
                ))

            plan = ShopRandomizationPlan(
                assignments=(), locked_slots=(), unvalidated_slots=(), disabled_slots=(),
            )
            result = apply_shop_plan(
                ShopTablesStage(content_root, tuple(parsed_tables)), plan, catalogs,
                output_content_root=work_root / "staging" / "Content",
                mappings_path=_LOCAL_MAPPING,
                unlock_npc_trader_shops=True,
            )
            self.assertEqual({output.family for output in result.outputs}, {"TRADE", "TRADE_DLC"})
            changed_formulas = 0
            changed_traders = set()
            thresholds_by_trader = {}
            for table in parsed_tables:
                output = next(item for item in result.outputs if item.family == table.family)
                original_rows = {row.name: row for row in table.parsed.rows}
                output_rows = {row.name: row for row in output.parsed.rows}
                self.assertEqual(tuple(original_rows), tuple(output_rows))
                for row_name, original_row in original_rows.items():
                    output_row = output_rows[row_name]
                    self.assertEqual(len(original_row.groups), len(output_row.groups))
                    for original_group, output_group in zip(
                        original_row.groups, output_row.groups, strict=True,
                    ):
                        self.assertEqual(
                            tuple(product.product_index for product in original_group.products),
                            tuple(product.product_index for product in output_group.products),
                        )
                        self.assertEqual(original_group.products, output_group.products)
                        affinity_match = re.match(
                            r"Friendship\.([A-Za-z0-9_]+)\.Present \+ "
                            r"Friendship\.\1\.Adventure \+ Friendship\.\1\.Episode >=\s*([0-9]+)",
                            original_group.evaluation_formula,
                            re.IGNORECASE,
                        )
                        if row_name.startswith("Character.Type.Buddy.") and affinity_match:
                            match = affinity_match
                            threshold = match.group(2)
                            thresholds_by_trader.setdefault(row_name, set()).add(int(threshold))
                            expected_formula = (
                                original_group.evaluation_formula[:match.start(2)]
                                + ("0" * len(threshold))
                                + original_group.evaluation_formula[match.end(2):]
                            )
                            self.assertEqual(
                                output_group.evaluation_formula,
                                expected_formula,
                            )
                            self.assertNotEqual(output_group.evaluation_raw, original_group.evaluation_raw)
                            changed_formulas += 1
                            changed_traders.add(row_name)
                        else:
                            self.assertEqual(output_group.evaluation_formula, original_group.evaluation_formula)
                            self.assertEqual(output_group.evaluation_raw, original_group.evaluation_raw)
            self.assertGreaterEqual(changed_formulas, 117)
            self.assertEqual(len(changed_traders), 13)
            self.assertEqual(set(thresholds_by_trader), changed_traders)
            for thresholds in thresholds_by_trader.values():
                self.assertEqual(thresholds, set(range(8, 73, 8)))

    # Check zenon controlled swap preserve price
    def test_zenon_controlled_swap_preserves_price_stock_and_unlock_bytes(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        catalogs = CatalogStore(repository_root / "data" / "catalogs").load("current")
        mappings = read_usmap(_LOCAL_MAPPING)
        with tempfile.TemporaryDirectory(prefix="cv2-zenon-controlled-swap-") as temporary:
            work_root = Path(temporary)
            content_root = work_root / "source" / "Content"
            family = "TRADE"
            table_name = SHOP_TABLE_NAMES[family]
            virtual = f"/Game/Blueprint/GameSystem/Shop/{table_name}"
            directory = content_root / "Blueprint" / "GameSystem" / "Shop"
            directory.mkdir(parents=True, exist_ok=True)
            header = directory / f"{table_name}.uasset"
            export = directory / f"{table_name}.uexp"
            shutil.copyfile(_LOCAL_ASSETS / header.name, header)
            shutil.copyfile(_LOCAL_ASSETS / export.name, export)
            expected = tuple(item for item in catalogs.shops if item.family == family)
            name_map = read_cooked_name_map(header, expected_package_name=virtual)
            raw = export.read_bytes()
            parsed = parse_shop_table(
                raw, family=family, name_map=name_map, mappings=mappings,
                expected_products=expected, source=str(export), verify_stock_against_catalog=False,
            )
            table = ParsedShopTable(
                family, table_name, virtual, MaterializedAsset(virtual, (header, export)),
                raw, name_map, parsed, expected,
            )
            row = "Character.Type.Buddy.Zenon"
            first = next(item for item in parsed.products if item.shop_row == row and item.product_index == 0)
            late = next(item for item in parsed.products if item.shop_row == row and item.product_index == 27)
            self.assertEqual((first.item_object, first.price, first.stock), ("DA_Item_AttrUpSTR", 2, 2))
            self.assertEqual((late.item_object, late.price, late.stock), ("DA_Halberd_Solomonal_Zenon", 12, 1))
            plan = ShopRandomizationPlan(
                assignments=(ShopAssignment(first, late), ShopAssignment(late, first)),
                locked_slots=(), unvalidated_slots=(), disabled_slots=(), available_families=(family,),
            )
            result = apply_shop_plan(
                ShopTablesStage(content_root, (table,)), plan, catalogs,
                output_content_root=work_root / "staging" / "Content", mappings_path=_LOCAL_MAPPING,
            )
            after = result.outputs[0].parsed.products
            after_first = next(item for item in after if item.shop_row == row and item.product_index == 0)
            after_late = next(item for item in after if item.shop_row == row and item.product_index == 27)
            self.assertEqual((after_first.item_object, after_first.price, after_first.stock),
                             ("DA_Halberd_Solomonal_Zenon", 2, 2))
            self.assertEqual((after_late.item_object, after_late.price, after_late.stock),
                             ("DA_Item_AttrUpSTR", 12, 1))
            self.assertEqual(after_first.evaluation_raw, first.evaluation_raw)
            self.assertEqual(after_late.evaluation_raw, late.evaluation_raw)
            assigned = {(first.family, row, index) for row, index in ((row, 0), (row, 27))}
            before_by_slot = {
                (item.family, item.shop_row, item.product_index): item for item in parsed.products
            }
            after_by_slot = {
                (item.family, item.shop_row, item.product_index): item
                for item in result.outputs[0].parsed.products
            }
            for key, before in before_by_slot.items():
                after = after_by_slot[key]
                if key in assigned:
                    continue
                self.assertEqual(after.raw_bytes, before.raw_bytes)
                self.assertEqual(after.evaluation_raw, before.evaluation_raw)
            before_groups = {
                row.name: tuple((group.evaluation_formula, group.evaluation_raw,
                                 tuple(product.product_index for product in group.products))
                                for group in row.groups)
                for row in parsed.rows
            }
            after_groups = {
                row.name: tuple((group.evaluation_formula, group.evaluation_raw,
                                 tuple(product.product_index for product in group.products))
                                for group in row.groups)
                for row in result.outputs[0].parsed.rows
            }
            self.assertEqual(after_groups, before_groups)


if __name__ == "__main__":
    unittest.main()

