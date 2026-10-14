from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.content_availability import (
    AssetAvailabilityState,
    PackageInventory,
    resolve_content_availability,
)


class ContentAvailabilityTests(unittest.TestCase):
    # Check numbered identity resolve without guessing
    def test_numbered_identity_resolves_without_guessing_between_variants(self) -> None:
        for name in ("DA_SingleSword_IronPipe", "DA_Hammer_Concrete", "DA_Item_AttrUpMND"):
            with self.subTest(name=name):
                base = "/Game/Blueprint/Article/" + name
                inventory = PackageInventory.create((base + "_1", base + "_2"))
                self.assertFalse(inventory.contains(base))
                self.assertTrue(inventory.contains(base + "_1"))
                availability = resolve_content_availability({"shops": (base, base + "_1")}, inventory)
                self.assertEqual(availability.state_for(base), AssetAvailabilityState.INVALID)
                self.assertEqual(availability.state_for(base + "_1"), AssetAvailabilityState.AVAILABLE)

    # Check known weapon hammer numbered packages
    def test_known_weapon_and_hammer_numbered_packages_resolve_as_exact_assets(self) -> None:
        for directory, name in (
            ("Weapon/SingleSword", "DA_SingleSword_IronPipe"),
            ("Weapon/Hammer", "DA_Hammer_Concrete"),
        ):
            with self.subTest(name=name):
                base = f"/Game/Blueprint/Article/{directory}/{name}"
                inventory = PackageInventory.create((base + "_1",))
                # An unnumbered reference gets only the uniquely observed asset
                # alias. The shop parser's nonzero FName Number resolves to it exactly.
                self.assertEqual(inventory.resolve_package_path(base)[0], base.casefold() + "_1")
                self.assertEqual(inventory.resolve_package_path(base + "_1")[0], (base + "_1").casefold())

    # Check spelling lookup preserve case exact
    def test_spelling_lookup_preserves_case_for_exact_and_ambiguous_paths(self) -> None:
        numbered = "/Game/Blueprint/Article/Weapon/DA_Test"
        exact = "/Game/Blueprint/Article/Weapon/DA_Exact"
        inventory = PackageInventory.create((
            numbered + "_1",
            numbered + "_2",
            exact,
        ))

        self.assertEqual(
            inventory.resolve_package_path_spelling(exact.lower()),
            (exact, (exact,)),
        )
        self.assertEqual(
            inventory.resolve_package_path_spelling(numbered.lower()),
            (None, (numbered + "_1", numbered + "_2")),
        )

    # Check literal numeric suffix is never
    def test_literal_numeric_suffix_is_never_stripped_or_guessed(self) -> None:
        base = "/Game/Blueprint/Article/DA_Test"
        self.assertFalse(PackageInventory.create((base,)).contains(base + "_07"))
        self.assertFalse(PackageInventory.create((base,)).contains(base + "_1"))
        self.assertFalse(PackageInventory.create((base + "_07",)).contains(base))
        self.assertFalse(PackageInventory.create((base + "_7",)).contains(base + "_07"))

    # Check source packages resolve available missing
    def test_source_packages_resolve_available_and_missing_independently_of_classification(self) -> None:
        base = "/Game/Blueprint/Article/DropItem/DT_DropItemData"
        optional = "/Game/Blueprint/Article/DropItem/DT_DropItemData_DLC"
        inventory = PackageInventory.create((base,))
        availability = resolve_content_availability(
            {"enemy_drops": (base, optional)},
            inventory,
        )

        self.assertTrue(availability.contains(base, subsystem="enemy_drops"))
        self.assertIs(
            availability.state_for(optional, subsystem="enemy_drops"),
            AssetAvailabilityState.MISSING,
        )
        self.assertEqual(availability.features[0].state, "PARTIAL")
        self.assertEqual(availability.features[0].available, 1)
        self.assertEqual(availability.features[0].missing, 1)
        self.assertEqual(availability.to_dict()["missing_optional_sources"][0]["package"], optional)

    # Check invalid state is distinct missing
    def test_invalid_state_is_distinct_from_missing(self) -> None:
        package = "/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_DLC"
        snapshot = resolve_content_availability(
            {"shops": (package,)}, PackageInventory.create((package,)),
        ).with_invalid(package, "package could not be parsed", subsystem="shops")

        self.assertIs(snapshot.state_for(package), AssetAvailabilityState.INVALID)
        self.assertEqual(snapshot.to_dict()["invalid_sources"][0]["reason"], "package could not be parsed")

    # Check inventory is same availability source
    def test_inventory_is_the_same_availability_source_for_game_or_extracted_cache(self) -> None:
        available = "/Game/Blueprint/Article/DropItem/DT_DropItemData_DLC"
        missing = "/Game/Blueprint/Article/DropItem/DT_DropItemData"
        snapshot = resolve_content_availability(
            {"enemy_drops": (available, missing)}, PackageInventory.create((available,)),
        )
        self.assertTrue(snapshot.contains(available, subsystem="enemy_drops"))
        self.assertFalse(snapshot.contains(missing, subsystem="enemy_drops"))
        self.assertEqual(snapshot.to_dict()["missing_sources"][0]["package"], missing)

    # Check extracted content tree can serve
    def test_extracted_content_tree_can_serve_as_the_same_inventory_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Content"
            package = root / "Blueprint" / "GameSystem" / "Shop" / "DT_ShopLineup_DLC.uasset"
            package.parent.mkdir(parents=True)
            package.write_bytes(b"header")
            inventory = PackageInventory.from_content_tree(root)
            self.assertTrue(inventory.contains("/Game/Blueprint/GameSystem/Shop/DT_ShopLineup_DLC"))


if __name__ == "__main__":
    unittest.main()
