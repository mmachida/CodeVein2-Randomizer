from __future__ import annotations

import unittest

from cv2randomizer.game.catalogs import RewardPayload, WorldLootLocation
from cv2randomizer.game.versioning import VersionDetection
from cv2randomizer.game.world_loot_compatibility import apply_world_loot_export_rebindings


# Build a test pickup location
def _location(
    package: str,
    index: int,
    *,
    mirror_package: str | None = None,
    mirror_index: int | None = None,
) -> WorldLootLocation:
    return WorldLootLocation(
        package_path=package,
        source_file=package.rsplit("/", 1)[-1] + ".umap",
        export_index=index,
        export_name=f"BP_Pickup_C_UAID_{index}",
        actor_class="BP_Pickup_C",
        actor_category="GROUND_PICKUP",
        save_id=f"save-{index}",
        protected=False,
        protection_reasons=(),
        rewards=(RewardPayload("/Game/Blueprint/Article/Item/DA_Item_1", 1, True),),
        destination_kind="RENEWABLE_INGREDIENT",
        destination_id=f"{package}#{index}",
        mirror_of_package_path=mirror_package,
        mirror_of_export_index=mirror_index,
    )


class WorldLootCompatibilityTests(unittest.TestCase):
    # Select profile for recognized build
    def test_recognized_109_build_selects_its_profile(self) -> None:
        detection = VersionDetection(
            False, "local-unverified-abc", "current", None, "local-only",
            local_unverified=True, display_version="1.0.9.0",
        )
        self.assertEqual(detection.world_loot_compatibility_profile, "1.0.9")

    # Keep other builds unprofiled
    def test_other_local_build_does_not_get_the_109_profile(self) -> None:
        detection = VersionDetection(
            False, "local-unverified-abc", "current", None, "local-only",
            local_unverified=True, display_version="1.0.3.0",
        )
        self.assertIsNone(detection.world_loot_compatibility_profile)

    # Update moved slots and mirrors
    def test_rebinding_updates_slot_mirror_and_destination_id(self) -> None:
        first_package = "/Game/Map/MainWorld/MainWorld/_Generated_/Map001"
        second_package = "/Game/Map/MainWorld/MainWorld/_Generated_/Map002"
        first = _location(first_package, 8)
        mirror = _location(second_package, 12, mirror_package=first_package, mirror_index=8)
        index = {
            "world_loot_export_rebindings": [{
                "package_path": first_package,
                "catalog_export_index": 8,
                "resolved_export_index": 10,
                "export_name": first.export_name,
                "class_package": "/Game/Blueprint/Pickup/BP_Pickup",
            }],
        }

        rebound = apply_world_loot_export_rebindings((first, mirror), index)

        self.assertEqual(rebound[0].export_index, 10)
        self.assertEqual(rebound[0].destination_id, f"{first_package}#10")
        self.assertEqual(rebound[1].mirror_of_export_index, 10)

    # Reject collisions safely
    def test_rebinding_collision_fails_closed(self) -> None:
        package = "/Game/Map/MainWorld/MainWorld/_Generated_/Map001"
        first = _location(package, 8)
        second = _location(package, 10)
        index = {
            "world_loot_export_rebindings": [{
                "package_path": package,
                "catalog_export_index": 8,
                "resolved_export_index": 10,
                "export_name": first.export_name,
                "class_package": "/Game/Blueprint/Pickup/BP_Pickup",
            }],
        }

        with self.assertRaisesRegex(ValueError, "collide"):
            apply_world_loot_export_rebindings((first, second), index)


if __name__ == "__main__":
    unittest.main()
