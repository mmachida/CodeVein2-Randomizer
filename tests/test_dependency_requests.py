from __future__ import annotations

import unittest

from cv2randomizer.game.dependency_requests import (
    DependencyRequestRegistry,
    find_missing_required_packages,
)


class DependencyRequestRegistryTests(unittest.TestCase):
    # Check required validation accept case preserved
    def test_required_validation_accepts_case_preserved_resolution(self) -> None:
        required = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Noah_01"
        extracted = required.casefold()

        missing = find_missing_required_packages(
            (required,),
            {required.casefold(): required},
            lambda _package: None,
            (extracted,),
        )

        self.assertEqual(missing, ())

    # Check required validation reports unextracted package
    def test_required_validation_reports_unextracted_package(self) -> None:
        required = "/Game/Blueprint/Article/BloodCode/DA_BloodCode_Noah_01"

        missing = find_missing_required_packages(
            (required,),
            {required.casefold(): required},
            lambda _package: None,
            (),
        )

        self.assertEqual(missing, (required,))

    # Check case variants cross system requests
    def test_case_variants_and_cross_system_requests_collapse_once(self) -> None:
        registry = DependencyRequestRegistry()
        registry.add(
            "/game/Blueprint/Article/DA_Test",
            system="world_loot",
            reason="reward source",
            required_components=(".uasset",),
        )
        registry.add(
            "/Game/Blueprint/Article/DA_Test",
            system="shops",
            reason="finite shop reward",
            required_components=(".uexp",),
        )

        self.assertEqual(registry.request_count, 2)
        self.assertEqual(registry.unique_package_count, 1)
        self.assertEqual(registry.duplicate_request_count, 1)
        self.assertEqual(registry.package_paths, ("/Game/Blueprint/Article/DA_Test",))
        request = registry.requests[0]
        self.assertEqual(request.requested_by_systems, ("shops", "world_loot"))
        self.assertEqual(request.required_components, (".uasset", ".uexp"))
        self.assertEqual(registry.system_counts(), {
            "shops": {"requests": 1, "unique_packages": 1},
            "world_loot": {"requests": 1, "unique_packages": 1},
        })
        self.assertEqual(len(registry.duplicate_rows()), 1)

    # Check repeated requests same system are
    def test_repeated_requests_from_same_system_are_counted(self) -> None:
        registry = DependencyRequestRegistry()
        registry.add_many(("/Game/A/One", "/Game/A/One"), system="required_core", reason="core")

        self.assertEqual(registry.summary()["duplicate_requests"], 1)
        self.assertEqual(registry.system_counts()["required_core"], {
            "requests": 2,
            "unique_packages": 1,
        })

    # Check reject non package paths
    def test_rejects_non_package_paths(self) -> None:
        registry = DependencyRequestRegistry()
        with self.assertRaises(ValueError):
            registry.add("/Game/../outside", system="test", reason="invalid")


if __name__ == "__main__":
    unittest.main()
