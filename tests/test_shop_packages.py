import struct
import unittest

from cv2randomizer.game.catalogs import ShopProduct
from cv2randomizer.game.shop_packages import (
    ShopPackageError,
    parse_cooked_name_map,
    validate_shop_catalog_references,
)


_PACKAGE_TAG = 0x9E2A83C1


# Encode an Unreal FString
def _fstring(value: str) -> bytes:
    encoded = value.encode("utf-8") + b"\0"
    return struct.pack("<i", len(encoded)) + encoded


# Resolve a package asset path
def _package(package_name: str, names: list[str]) -> bytes:
    data = bytearray(
        struct.pack("<I5i", _PACKAGE_TAG, -8, 0, 0, 0, 0)
        + struct.pack("<ii", 0, 0)
        + _fstring(package_name)
    )
    data.extend(struct.pack("<Iii", 0x80002200, len(names), 0))
    name_offset = len(data)
    for name in names:
        data.extend(_fstring(name))
        data.extend(b"\0\0\0\0")
    struct.pack_into("<i", data, 28, len(data))
    struct.pack_into("<i", data, name_offset - 4, name_offset)
    return bytes(data)


# Read product
def _product(
    *,
    path: str = "/Game/Blueprint/Article/Item/DA_Test",
    object_name: str = "DA_Test",
    row: str = "Shop_Test",
) -> ShopProduct:
    return ShopProduct(
        family="NORMAL",
        shop_row=row,
        product_index=0,
        original_product_path=path,
        item_object=object_name,
        price=100,
        uses_stock=False,
        stock=0,
        protected=False,
        protection_reason=None,
    )


class ShopPackageTests(unittest.TestCase):
    # Check reads unversioned package summary name
    def test_reads_unversioned_package_summary_and_name_map(self) -> None:
        package_name = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"
        names = ["/Game/Blueprint/Article/Item/DA_Test", "DA_Test", "Shop_Test"]
        parsed = parse_cooked_name_map(_package(package_name, names))

        self.assertEqual(parsed.package_name, package_name)
        self.assertTrue(parsed.is_unversioned)
        self.assertEqual(parsed.names, tuple(names))
        self.assertEqual(parsed.resolve(0), names[0])
        self.assertEqual(parsed.resolve(1, 1), "DA_Test_0")

    # Check reject truncated name map
    def test_rejects_truncated_name_map(self) -> None:
        package = _package(
            "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup",
            ["/Game/Blueprint/Article/Item/DA_Test"],
        )
        truncated = bytearray(package[:-2])
        struct.pack_into("<i", truncated, 28, len(truncated))
        with self.assertRaisesRegex(ShopPackageError, "truncated"):
            parse_cooked_name_map(bytes(truncated))

    # Check catalog references must match table
    def test_catalog_references_must_match_the_table_name_map(self) -> None:
        package_name = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup"
        names = ["/Game/Blueprint/Article/Item/DA_Test", "DA_Test", "Shop_Test"]
        parsed = parse_cooked_name_map(_package(package_name, names))

        summary = validate_shop_catalog_references(parsed, "NORMAL", [_product()])
        self.assertEqual(summary.product_slots, 1)
        self.assertEqual(summary.unique_rows, 1)
        self.assertEqual(summary.unique_items, 1)

        missing = _product(path="/Game/Blueprint/Article/Item/DA_Missing")
        with self.assertRaisesRegex(ShopPackageError, "missing catalog references"):
            validate_shop_catalog_references(parsed, "NORMAL", [missing])


if __name__ == "__main__":
    unittest.main()
