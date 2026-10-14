from __future__ import annotations

import struct
import unittest
from pathlib import Path
from dataclasses import replace

from cv2randomizer.game.catalogs import ShopProduct
from cv2randomizer.game.shop_packages import CookedShopNameMap
from cv2randomizer.game.shop_tables import ShopTableError, parse_shop_table
from cv2randomizer.game.usmap import (
    UsmapMappings,
    UsmapProperty,
    UsmapPropertyType,
    UsmapStruct,
)
from cv2randomizer.patching.shop_tables import ShopPatchError, patch_shop_table
from cv2randomizer.randomizer.shops import ShopAssignment, shop_slot_lock_reason

_PACKAGE_TAG = 0x9E2A83C1
_PACKAGE_NAME = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"
_ARTICLE_PATH = "/Game/Blueprint/Article/Item/DA_Test"


# Read mappings
def _mappings() -> UsmapMappings:
    soft_path = UsmapPropertyType(17)
    integer = UsmapPropertyType(2)
    boolean = UsmapPropertyType(1)
    formula = UsmapStruct(
        "StoryFlagEvaluationFormula",
        None,
        1,
        (UsmapProperty(0, "EvaluationFormula", 1, UsmapPropertyType(10)),),
    )
    product = UsmapStruct(
        "ShopProductInfo",
        None,
        4,
        (
            UsmapProperty(0, "ArticleDataPath", 1, soft_path),
            UsmapProperty(1, "Price", 1, integer),
            UsmapProperty(2, "bIsUseStock", 1, boolean),
            UsmapProperty(3, "Stock", 1, integer),
        ),
    )
    replenish = UsmapStruct(
        "ShopReplenishInfo",
        None,
        2,
        (
            UsmapProperty(0, "Flag", 1, UsmapPropertyType(9, struct_name=formula.name)),
            UsmapProperty(
                1,
                "ProductInfos",
                1,
                UsmapPropertyType(8, inner_type=UsmapPropertyType(9, struct_name=product.name)),
            ),
        ),
    )
    row = UsmapStruct(
        "ShopLineupTableRow",
        None,
        1,
        (
            UsmapProperty(
                0,
                "ReplenishInfos",
                1,
                UsmapPropertyType(8, inner_type=UsmapPropertyType(9, struct_name=replenish.name)),
            ),
        ),
    )
    structs = {item.name.casefold(): item for item in (formula, product, replenish, row)}
    return UsmapMappings(0, 0, 0, len(structs), structs, 0)


# Resolve name map
def _name_map(
    article_path: str = _ARTICLE_PATH,
    row_name: str = "Shop_Test",
    package_name: str = _PACKAGE_NAME,
) -> CookedShopNameMap:
    return CookedShopNameMap(
        package_name=package_name,
        package_flags=0,
        total_header_size=0,
        name_offset=0,
        names=(article_path, "DA_Test", row_name),
        is_unversioned=True,
    )


# Read product
def _product(*, uses_stock: bool = True, stock: int = 3, price: int = 100) -> ShopProduct:
    return ShopProduct(
        family="NORMAL",
        shop_row="Shop_Test",
        product_index=0,
        original_product_path=_ARTICLE_PATH,
        item_object="DA_Test",
        price=price,
        uses_stock=uses_stock,
        stock=stock,
        protected=False,
        protection_reason=None,
    )


# Read fragment
def _fragment(value_count: int, *, has_zero_mask: bool = False) -> bytes:
    bits = (value_count << 9) | 0x100 | (0x80 if has_zero_mask else 0)
    return struct.pack("<H", bits)


# Build table
def _table(
    *, default_stock_fields: bool = False, uses_stock: bool = True, stock: int = 3,
    article_path_number: int = 0, item_object_number: int = 0,
    row_name: str = "Shop_Test", formula: str = "",
) -> bytes:
    data = bytearray(struct.pack("<i", 1))  # serialized row count
    data.extend(struct.pack("<ii", 2, 0))  # Shop_Test row FName
    data.extend(_fragment(1))  # ShopLineupTableRow
    data.extend(struct.pack("<i", 1))  # ReplenishInfos count
    data.extend(_fragment(2))  # ShopReplenishInfo
    data.extend(_fragment(1))  # StoryFlagEvaluationFormula
    if formula:
        encoded_formula = formula.encode("utf-8") + b"\0"
        data.extend(struct.pack("<i", len(encoded_formula)))
        data.extend(encoded_formula)
    else:
        data.extend(struct.pack("<i", 0))  # empty FString formula
    data.extend(struct.pack("<i", 1))  # ProductInfos count
    data.extend(_fragment(4, has_zero_mask=default_stock_fields))
    if default_stock_fields:
        data.append(0b1100)  # bIsUseStock and Stock are supplied by struct defaults
    data.extend(struct.pack("<iiii", 0, article_path_number, 1, item_object_number))  # path/object FNames
    data.extend(struct.pack("<i", 0))  # empty soft-object subpath
    data.extend(struct.pack("<i", 100))  # Price
    if not default_stock_fields:
        data.extend(struct.pack("<B", int(uses_stock)))
        data.extend(struct.pack("<i", stock))
    data.extend(struct.pack("<I", _PACKAGE_TAG))
    return bytes(data)


class ShopTableTests(unittest.TestCase):
    # Parse package source data
    def _parse(
        self,
        data: bytes,
        expected: ShopProduct | None = None,
        *,
        verify_stock_against_catalog: bool = True,
    ):
        return parse_shop_table(
            data,
            family="NORMAL",
            name_map=_name_map(),
            mappings=_mappings(),
            expected_products=(expected or _product(),),
            verify_stock_against_catalog=verify_stock_against_catalog,
        )

    # Check decodes defaulted stock without materializing
    def test_decodes_defaulted_stock_without_materializing_raw_payload(self) -> None:
        data = _table(default_stock_fields=True)
        parsed = self._parse(data, verify_stock_against_catalog=False)

        self.assertEqual(len(parsed.rows), 1)
        self.assertEqual(len(parsed.products), 1)
        item = parsed.products[0]
        self.assertEqual(item.article_path, _ARTICLE_PATH)
        self.assertEqual(item.item_object, "DA_Test")
        self.assertEqual(item.price, 100)
        self.assertIsNone(item.uses_stock)
        self.assertIsNone(item.stock)
        self.assertFalse(item.logical_uses_stock)
        self.assertEqual(item.logical_stock, 0)
        self.assertEqual(item.raw_bytes, data[30:-4])
        self.assertEqual(len(item.article_path_bytes), 20)
        self.assertEqual(item.price_offset, item.article_path_offset + 20)
        self.assertIsNone(item.uses_stock_offset)
        self.assertIsNone(item.stock_offset)
        self.assertIsNone(item.uses_stock_bytes)
        self.assertIsNone(item.stock_bytes)
        self.assertIsNotNone(item.uses_stock_mask_bit)
        self.assertIsNotNone(item.stock_mask_bit)

    # Check default parse keep catalog stock
    def test_default_parse_keeps_catalog_stock_mismatch_as_observation(self) -> None:
        expected = replace(_product(), stock=0)
        parsed = parse_shop_table(
            _table(stock=1),
            family="NORMAL",
            name_map=_name_map(),
            mappings=_mappings(),
            expected_products=(expected,),
        )
        self.assertEqual(parsed.products[0].stock, 1)
        self.assertEqual(parsed.products[0].stock_bytes, struct.pack("<i", 1))

    # Check strict validation reject unresolved stock
    def test_strict_validation_rejects_unresolved_stock_defaults(self) -> None:
        with self.assertRaisesRegex(ShopTableError, "bIsUseStock mismatch"):
            self._parse(_table(default_stock_fields=True))

    # Check match serialized stock fields against
    def test_matches_serialized_stock_fields_against_catalog(self) -> None:
        parsed = self._parse(_table(uses_stock=False, stock=4), _product(uses_stock=False, stock=4))

        self.assertFalse(parsed.products[0].uses_stock)
        self.assertEqual(parsed.products[0].stock, 4)

    # Check reject serialized stock mismatch
    def test_rejects_serialized_stock_mismatch(self) -> None:
        expected = replace(_product(), stock=0)
        with self.assertRaisesRegex(ShopTableError, "Stock mismatch"):
            self._parse(_table(stock=1), expected)

    # Check reject catalog price mismatch
    def test_rejects_catalog_price_mismatch(self) -> None:
        expected = replace(_product(), price=101)
        with self.assertRaisesRegex(ShopTableError, "Price mismatch"):
            self._parse(_table(), expected)

    # Check detect important item paths
    def test_detects_important_item_paths(self) -> None:
        important_path = "/Game/Blueprint/Article/Item/Important/DA_Test"
        expected = replace(
            _product(),
            original_product_path=important_path,
            protected=True,
            protection_reason="Important item",
        )
        parsed = parse_shop_table(
            _table(),
            family="NORMAL",
            name_map=_name_map(important_path),
            mappings=_mappings(),
            expected_products=(expected,),
        )

        self.assertTrue(parsed.products[0].important)

    # Check NPC trader unlock changes fixed
    def test_npc_trader_unlock_changes_only_fixed_width_affinity_threshold(self) -> None:
        row_name = "Character.Type.Buddy.Lou"
        formula = (
            "Friendship.Lou.Present + Friendship.Lou.Adventure + "
            "Friendship.Lou.Episode >= 72"
        )
        data = _table(row_name=row_name, formula=formula)
        expected = replace(_product(), family="TRADE", shop_row=row_name)
        name_map = _name_map(
            row_name=row_name,
            package_name="/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_Trade",
        )
        mappings = _mappings()

        # Reopen the package after patching
        def reparse(output: bytes):
            return parse_shop_table(
                output, family="TRADE", name_map=name_map, mappings=mappings,
                expected_products=(expected,), source="NPC Trader unlock fixture",
            )

        parsed = reparse(data)
        group = parsed.rows[0].groups[0]
        self.assertEqual(group.evaluation_formula, formula)
        self.assertIsNotNone(group.evaluation_formula_offset)
        self.assertEqual(
            data[group.evaluation_formula_offset:group.evaluation_formula_offset + len(group.evaluation_formula_bytes)],
            group.evaluation_formula_bytes,
        )

        patched = patch_shop_table(
            data, parsed, (ShopAssignment(parsed.products[0], parsed.products[0]),),
            reparse=reparse, unlock_npc_trader_shops=True,
        )
        unlocked = patched.parsed.rows[0].groups[0]
        self.assertEqual(
            unlocked.evaluation_formula,
            "Friendship.Lou.Present + Friendship.Lou.Adventure + Friendship.Lou.Episode >= 00",
        )
        self.assertEqual(len(patched.data), len(data))
        self.assertNotEqual(unlocked.evaluation_raw, group.evaluation_raw)
        self.assertEqual(patched.parsed.products, parsed.products)

    # Check NPC trader unlock fail closed
    def test_npc_trader_unlock_fails_closed_on_unrecognized_formula(self) -> None:
        row_name = "Character.Type.Buddy.Lou"
        data = _table(row_name=row_name, formula="Friendship.Lou.Episode >= 72")
        expected = replace(_product(), family="TRADE", shop_row=row_name)
        name_map = _name_map(
            row_name=row_name,
            package_name="/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_Trade",
        )
        mappings = _mappings()
        parsed = parse_shop_table(
            data, family="TRADE", name_map=name_map, mappings=mappings,
            expected_products=(expected,), source="NPC Trader invalid formula fixture",
        )
        with self.assertRaisesRegex(ShopPatchError, "Unrecognized NPC Trader affinity formula"):
            patch_shop_table(
                data, parsed, (), reparse=lambda output: parsed,
                unlock_npc_trader_shops=True,
            )

    # Check validate explicit FName numbers without
    def test_validates_explicit_fname_numbers_without_rewriting_the_identity(self) -> None:
        base_path = "/Game/Blueprint/Article/Item/DA_Test"
        expected = replace(
            _product(),
            original_product_path=base_path,
            item_object="DA_Test",
            article_path_number=8,
            item_object_number=8,
        )

        parsed = parse_shop_table(
            _table(article_path_number=8, item_object_number=8),
            family="NORMAL",
            name_map=_name_map(base_path),
            mappings=_mappings(),
            expected_products=(expected,),
        )

        self.assertEqual(parsed.products[0].article_path, base_path)
        self.assertEqual(parsed.products[0].article_path_number, 8)
        self.assertEqual(parsed.products[0].item_object, "DA_Test")
        self.assertEqual(parsed.products[0].item_object_number, 8)

    # Check validate zero padded suffix literal
    def test_validates_zero_padded_suffix_as_a_literal_name(self) -> None:
        path = "/Game/Blueprint/Article/Item/DA_Test_07"
        object_name = "DA_Test_07"
        expected = replace(_product(), original_product_path=path, item_object=object_name)
        name_map = CookedShopNameMap(
            package_name=_PACKAGE_NAME,
            package_flags=0,
            total_header_size=0,
            name_offset=0,
            names=(path, object_name, "Shop_Test"),
            is_unversioned=True,
        )

        parsed = parse_shop_table(
            _table(),
            family="NORMAL",
            name_map=name_map,
            mappings=_mappings(),
            expected_products=(expected,),
        )

        self.assertEqual(parsed.products[0].article_path, path)
        self.assertEqual(parsed.products[0].article_path_number, 0)
        self.assertEqual(parsed.products[0].item_object, object_name)
        self.assertEqual(parsed.products[0].item_object_number, 0)


_LOCAL_ROOT = Path(__file__).resolve().parents[1]
_LOCAL_ASSET_CANDIDATES = (
    _LOCAL_ROOT / "workspace" / "research_assets" / "shop",
    _LOCAL_ROOT / "workspace" / "research_assets",
    _LOCAL_ROOT / "workspace" / "research" / "_assets" / "shop",
)
_LOCAL_ASSETS = next(
    (directory for directory in _LOCAL_ASSET_CANDIDATES if (directory / "DT_ShopLineup.uasset").is_file()),
    _LOCAL_ASSET_CANDIDATES[0],
)
_LOCAL_MAPPING_CANDIDATES = (
    _LOCAL_ASSETS / "CV2.usmap",
    _LOCAL_ASSETS.parent / "CV2.usmap",
    _LOCAL_ROOT / "workspace" / "research_assets" / "CV2.usmap",
)
_LOCAL_MAPPING = next(
    (path for path in _LOCAL_MAPPING_CANDIDATES if path.is_file()),
    _LOCAL_MAPPING_CANDIDATES[0],
)
_LOCAL_FIXTURES_AVAILABLE = _LOCAL_MAPPING.is_file() and all(
    (_LOCAL_ASSETS / name).is_file()
    for name in (
        "DT_ShopLineup.uasset",
        "DT_ShopLineup.uexp",
        "DT_ShopLineup_DLC.uasset",
        "DT_ShopLineup_DLC.uexp",
        "DT_ShopLineup_Luxury.uasset",
        "DT_ShopLineup_Luxury.uexp",
        "DT_ShopLineup_Trade.uasset",
        "DT_ShopLineup_Trade.uexp",
        "DT_ShopLineup_Trade_DLC.uasset",
        "DT_ShopLineup_Trade_DLC.uexp",
    )
)


@unittest.skipUnless(_LOCAL_FIXTURES_AVAILABLE, "local shop research assets are not staged")
class LocalShopTableFixtureTests(unittest.TestCase):
    # Check decodes all local table families
    def test_decodes_all_local_table_families_and_records_stock_discrepancies(self) -> None:
        import csv

        from cv2randomizer.game.catalogs import CatalogStore
        from cv2randomizer.game.shop_packages import SHOP_TABLE_NAMES, read_cooked_name_map
        from cv2randomizer.game.shop_tables import parse_shop_table
        from cv2randomizer.game.usmap import read_usmap

        catalogs = CatalogStore(_LOCAL_ROOT / "data" / "catalogs").load("current")
        mappings = read_usmap(_LOCAL_MAPPING)
        family_names = {
            "normal": "NORMAL",
            "dlc": "DLC",
            "luxury": "LUXURY",
            "trade": "TRADE",
            "trade_dlc": "TRADE_DLC",
        }
        csv_by_slot = {}
        row_positions = {}
        source_csv = _LOCAL_ROOT / "data" / "catalogs" / "current" / "shop_catalog_all_tables.csv"
        with source_csv.open(encoding="utf-8-sig", newline="") as handle:
            for record in csv.DictReader(handle):
                family = family_names[record["table"].strip().casefold()]
                row = record["shop_row"]
                key = (family, row)
                index = row_positions.get(key, 0)
                csv_by_slot[(family, row, index)] = record
                row_positions[key] = index + 1
        self.assertEqual(len(csv_by_slot), 855)
        expected_counts = {
            "NORMAL": (23, 374),
            "DLC": (1, 23),
            "LUXURY": (1, 35),
            "TRADE": (12, 392),
            "TRADE_DLC": (1, 31),
        }
        defaulted_uses_stock = 0
        defaulted_stock = 0
        explicit_stock_mismatches = 0

        for family, table_name in SHOP_TABLE_NAMES.items():
            expected = tuple(item for item in catalogs.shops if item.family == family)
            name_map = read_cooked_name_map(
                _LOCAL_ASSETS / f"{table_name}.uasset",
                expected_package_name=f"/Game/Blueprint/GameSystem/Shop/{table_name}",
            )
            parsed = parse_shop_table(
                (_LOCAL_ASSETS / f"{table_name}.uexp").read_bytes(),
                family=family,
                name_map=name_map,
                mappings=mappings,
                expected_products=expected,
                source=f"{table_name}.uexp",
                verify_stock_against_catalog=False,
            )
            self.assertEqual((len(parsed.rows), len(parsed.products)), expected_counts[family])
            expected_by_slot = {(item.shop_row, item.product_index): item for item in expected}
            for item in parsed.products:
                catalog_product = expected_by_slot[(item.shop_row, item.product_index)]
                source = csv_by_slot[(family, item.shop_row, item.product_index)]
                self.assertEqual(source["item_path"], catalog_product.original_product_path)
                self.assertEqual(source["item_object"], catalog_product.item_object)
                self.assertEqual(int(source["price"]), catalog_product.price)
                self.assertEqual(
                    int(source["binary_offset_hex"], 16),
                    item.article_path_offset,
                    f"{family} {item.shop_row}[{item.product_index}] ArticleDataPath offset",
                )
                self.assertEqual(len(item.article_path_bytes), 20)
                self.assertEqual(item.price_offset, item.article_path_offset + 20)
                if item.has_explicit_stock_payload:
                    self.assertEqual(item.uses_stock_offset, item.article_path_offset + 24)
                    self.assertEqual(item.stock_offset, item.article_path_offset + 25)
                    self.assertEqual(len(item.uses_stock_bytes), 1)
                    self.assertEqual(len(item.stock_bytes), 4)
                self.assertEqual(source["uses_stock"].casefold() == "true", catalog_product.uses_stock)
                self.assertEqual(int(source["stock"]), catalog_product.stock)
                if item.uses_stock is None:
                    defaulted_uses_stock += 1
                    self.assertFalse(item.logical_uses_stock)
                    self.assertIsNone(item.uses_stock_bytes)
                if item.stock is None:
                    defaulted_stock += 1
                    self.assertEqual(item.logical_stock, 0)
                    self.assertIsNone(item.stock_bytes)
                elif item.stock != int(source["stock"]):
                    explicit_stock_mismatches += 1
                    self.assertEqual((source["stock"], item.stock), ("0", 1))

        self.assertEqual(defaulted_uses_stock, 180)
        self.assertEqual(defaulted_stock, 63)
        self.assertEqual(explicit_stock_mismatches, 117)

    # Check magmell tool shop is protected
    def test_magmell_tool_shop_is_protected_and_separate_from_weapon_row(self) -> None:
        from cv2randomizer.game.catalogs import CatalogStore
        from cv2randomizer.game.shop_packages import read_cooked_name_map
        from cv2randomizer.game.usmap import read_usmap

        catalogs = CatalogStore(_LOCAL_ROOT / "data" / "catalogs").load("current")
        family = "NORMAL"
        table_name = "DT_ShopLineup"
        expected = tuple(item for item in catalogs.shops if item.family == family)
        name_map = read_cooked_name_map(
            _LOCAL_ASSETS / f"{table_name}.uasset",
            expected_package_name="/Game/Blueprint/GameSystem/Shop/DT_ShopLineup",
        )
        source = (_LOCAL_ASSETS / f"{table_name}.uexp").read_bytes()
        mappings = read_usmap(_LOCAL_MAPPING)

        # Reopen the package after patching
        def reparse(data):
            return parse_shop_table(
                data,
                family=family,
                name_map=name_map,
                mappings=mappings,
                expected_products=expected,
                source="DT_ShopLineup test output",
                verify_stock_against_catalog=False,
            )

        parsed = reparse(source)
        tool_row = next(row for row in parsed.rows if row.name == "Shop_Jadwiga_Item")
        weapon_row = next(row for row in parsed.rows if row.name == "Shop_Jadwiga_Weapon")
        self.assertEqual((len(tool_row.products), len(tool_row.groups)), (56, 40))
        self.assertEqual(len(weapon_row.products), 32)
        self.assertTrue(all(product.protected for product in tool_row.products))
        self.assertTrue(all(product.protection_reason == "MAGMELL_TOOL_SHOP" for product in tool_row.products))
        self.assertTrue(all(shop_slot_lock_reason(product) == "MAGMELL_TOOL_SHOP" for product in tool_row.products))
        self.assertTrue(all(not product.protected for product in weapon_row.products))
        self.assertEqual(tool_row.groups[0].evaluation_formula, "")
        self.assertEqual(tool_row.groups[1].evaluation_formula, "ShopCert_Normal_01_SC_Normal1")
        self.assertEqual(
            tool_row.groups[18].evaluation_formula,
            "ShopCert_VIPPass_2 && ShopCert_MatEnchantWeaponSTR_1",
        )
        self.assertEqual(tool_row.groups[-1].evaluation_formula, "ShopCert_ForsakenWorld")
        self.assertTrue(all("ShopCert_" in group.evaluation_formula for group in tool_row.groups[1:]))

        catalog_products = {(item.shop_row, item.product_index): item for item in expected}
        card_articles = [item for item in expected if "shopcert" in item.original_product_path.casefold()]
        self.assertEqual(len(card_articles), 16)
        self.assertTrue(all(not item.protected for item in card_articles))
        self.assertTrue(all(
            not catalog_products[("Shop_Jadwiga_Weapon", item.product_index)].protected
            for item in weapon_row.products
        ))

    # Check magmell tool shop row bytes
    def test_magmell_tool_shop_row_bytes_survive_randomizing_another_row(self) -> None:
        from cv2randomizer.game.catalogs import CatalogStore
        from cv2randomizer.game.shop_packages import read_cooked_name_map
        from cv2randomizer.game.usmap import read_usmap
        from cv2randomizer.patching.shop_tables import patch_shop_table
        from cv2randomizer.randomizer.shops import ShopAssignment

        catalogs = CatalogStore(_LOCAL_ROOT / "data" / "catalogs").load("current")
        family = "NORMAL"
        table_name = "DT_ShopLineup"
        expected = tuple(item for item in catalogs.shops if item.family == family)
        name_map = read_cooked_name_map(
            _LOCAL_ASSETS / f"{table_name}.uasset",
            expected_package_name="/Game/Blueprint/GameSystem/Shop/DT_ShopLineup",
        )
        source = (_LOCAL_ASSETS / f"{table_name}.uexp").read_bytes()
        mappings = read_usmap(_LOCAL_MAPPING)

        # Reopen the package after patching
        def reparse(data):
            return parse_shop_table(
                data,
                family=family,
                name_map=name_map,
                mappings=mappings,
                expected_products=expected,
                source="DT_ShopLineup patched test output",
                verify_stock_against_catalog=False,
            )

        parsed = reparse(source)
        tool_before = next(row for row in parsed.rows if row.name == "Shop_Jadwiga_Item")
        weapon = next(row for row in parsed.rows if row.name == "Shop_Jadwiga_Weapon")
        patchable = [item for item in weapon.products if item.has_explicit_stock_payload]
        self.assertGreaterEqual(len(patchable), 2)
        first, second = patchable[:2]
        source_by_destination = {
            (first.shop_row, first.product_index): second,
            (second.shop_row, second.product_index): first,
        }
        expected_after = tuple(
            replace(
                item,
                original_product_path=source_by_destination[(item.shop_row, item.product_index)].article_path,
                item_object=source_by_destination[(item.shop_row, item.product_index)].item_object,
            ) if (item.shop_row, item.product_index) in source_by_destination else item
            for item in expected
        )

        # Read reparse after
        def reparse_after(data):
            return parse_shop_table(
                data,
                family=family,
                name_map=name_map,
                mappings=mappings,
                expected_products=expected_after,
                source="DT_ShopLineup patched test output",
                verify_stock_against_catalog=False,
            )

        patched = patch_shop_table(
            source,
            parsed,
            (
                ShopAssignment(first, second),
                ShopAssignment(second, first),
            ),
            reparse=reparse_after,
        )
        tool_after = next(row for row in patched.parsed.rows if row.name == "Shop_Jadwiga_Item")
        self.assertEqual(tool_after.raw_bytes, tool_before.raw_bytes)
        self.assertEqual(tool_after.groups, tool_before.groups)
        self.assertEqual(tool_after.products, tool_before.products)
        self.assertEqual(
            patched.changed_slots,
            (("NORMAL", "Shop_Jadwiga_Weapon", first.product_index),
             ("NORMAL", "Shop_Jadwiga_Weapon", second.product_index)),
        )


if __name__ == "__main__":
    unittest.main()
