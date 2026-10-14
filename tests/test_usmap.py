import struct
import unittest

from cv2randomizer.game.usmap import UsmapError, parse_usmap


_MAGIC = 0x30C4


# Parse mapping
def _mapping() -> bytes:
    names = [
        "ShopProductInfo",
        "ShopReplenishInfo",
        "ShopLineupTableRow",
        "TableRowBase",
        "StoryFlagEvaluationFormula",
        "ArticleDataPath",
        "Price",
        "bIsUseStock",
        "Stock",
        "Flag",
        "ProductInfos",
        "ReplenishInfos",
        "TestEnum",
        "One",
    ]
    indices = {name: index for index, name in enumerate(names)}
    payload = bytearray(struct.pack("<I", len(names)))
    for name in names:
        raw = name.encode("utf-8")
        payload.extend(struct.pack("<H", len(raw)))
        payload.extend(raw)

    payload.extend(struct.pack("<I", 1))  # enum count
    payload.extend(struct.pack("<iH", indices["TestEnum"], 1))
    payload.extend(struct.pack("<Qi", 7, indices["One"]))

    # Write property
    def write_property(index: int, name: str, type_data: bytes, array_dim: int = 1) -> None:
        payload.extend(struct.pack("<HB i", index, array_dim, indices[name]))
        payload.extend(type_data)

    # Read primitive
    def primitive(code: int) -> bytes:
        return struct.pack("<B", code)

    # Read struct type
    def struct_type(name: str) -> bytes:
        return struct.pack("<Bi", 9, indices[name])

    # Parse array of struct
    def array_of_struct(name: str) -> bytes:
        return struct.pack("<B", 8) + struct_type(name)

    payload.extend(struct.pack("<I", 3))  # struct count

    payload.extend(struct.pack("<iiHH", indices["ShopProductInfo"], -1, 4, 4))
    write_property(0, "ArticleDataPath", primitive(17))
    write_property(1, "Price", primitive(2))
    write_property(2, "bIsUseStock", primitive(1))
    write_property(3, "Stock", primitive(2))

    payload.extend(struct.pack("<iiHH", indices["ShopReplenishInfo"], -1, 2, 2))
    write_property(0, "Flag", struct_type("StoryFlagEvaluationFormula"))
    write_property(1, "ProductInfos", array_of_struct("ShopProductInfo"))

    payload.extend(
        struct.pack(
            "<iiHH",
            indices["ShopLineupTableRow"],
            indices["TableRowBase"],
            1,
            1,
        )
    )
    write_property(0, "ReplenishInfos", array_of_struct("ShopReplenishInfo"))

    return struct.pack("<HBBiII", _MAGIC, 4, 0, 0, len(payload), len(payload)) + payload


class UsmapTests(unittest.TestCase):
    # Check reads nested shop row schema
    def test_reads_nested_shop_row_schema(self) -> None:
        mappings = parse_usmap(_mapping())

        self.assertEqual((mappings.version, mappings.name_count, mappings.enum_count), (4, 14, 1))
        product = mappings.struct("shopproductinfo")
        self.assertIsNotNone(product)
        assert product is not None
        self.assertEqual(
            [(item.schema_index, item.name, item.type.name) for item in product.properties],
            [
                (0, "ArticleDataPath", "SoftObjectProperty"),
                (1, "Price", "IntProperty"),
                (2, "bIsUseStock", "BoolProperty"),
                (3, "Stock", "IntProperty"),
            ],
        )

        row = mappings.struct("ShopLineupTableRow")
        self.assertIsNotNone(row)
        assert row is not None
        replenish = row.property("replenishinfos")
        self.assertIsNotNone(replenish)
        assert replenish is not None
        self.assertEqual(replenish.type.name, "ArrayProperty")
        self.assertEqual(replenish.type.inner_type.struct_name, "ShopReplenishInfo")

    # Check reject compressed maps
    def test_rejects_compressed_maps(self) -> None:
        compressed = bytearray(_mapping())
        struct.pack_into("<i", compressed, 4, 1)
        with self.assertRaisesRegex(UsmapError, "compression method 1"):
            parse_usmap(bytes(compressed))

    # Check reject truncated maps
    def test_rejects_truncated_maps(self) -> None:
        with self.assertRaisesRegex(UsmapError, "size does not match"):
            parse_usmap(_mapping()[:-1])


if __name__ == "__main__":
    unittest.main()
