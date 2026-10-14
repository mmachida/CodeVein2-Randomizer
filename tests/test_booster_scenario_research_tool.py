from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cv2randomizer.game.asset_extraction import AssetExtractionError, ExtractedAsset
from tools.extract_booster_scenario_research import extract_packages, read_package_paths


class _Extractor:
    # Initialize this object
    def __init__(self, source_root: Path, failed_path: str) -> None:
        self.source_root = source_root
        self.failed_path = failed_path
        self.calls: list[tuple[str, ...]] = []

    # Extract requested game assets
    def extract_assets(self, _game_root, virtual_paths, **_kwargs):
        requested = tuple(virtual_paths)
        self.calls.append(requested)
        if len(requested) > 1:
            raise AssetExtractionError("synthetic batch failure")
        virtual_path = requested[0]
        if virtual_path == self.failed_path:
            raise AssetExtractionError("synthetic missing package")
        relative = Path(*virtual_path.split("/")[2:])
        package_dir = self.source_root / relative.parent
        package_dir.mkdir(parents=True, exist_ok=True)
        header = package_dir / f"{relative.name}.uasset"
        export = package_dir / f"{relative.name}.uexp"
        header.write_bytes(b"uasset")
        export.write_bytes(b"uexp")
        return (ExtractedAsset(virtual_path, (header, export)),)


class BoosterScenarioResearchTests(unittest.TestCase):
    # Check reads exactly 31 unique virtual
    def test_reads_exactly_31_unique_virtual_package_paths(self) -> None:
        packages = tuple(f"/Game/Cinematics/Scenario/Scenario_{index:02d}" for index in range(31))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "packages.txt"
            source.write_text("\n".join(packages), encoding="utf-8")
            self.assertEqual(read_package_paths(source), packages)

            source.write_text("\n".join(packages[:-1]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly 31"):
                read_package_paths(source)

    # Check accept pass specific expected package
    def test_accepts_pass_specific_expected_package_count(self) -> None:
        packages = tuple(f"/Game/Cinematics/Dependencies/Package_{index:02d}" for index in range(35))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "packages.txt"
            source.write_text("\n".join(packages), encoding="utf-8")
            self.assertEqual(read_package_paths(source, expected_count=35), packages)

    # Check fall back per package materializes
    def test_falls_back_per_package_and_materializes_only_successes(self) -> None:
        packages = ("/Game/Cinematics/Scenario/One", "/Game/Cinematics/Scenario/Missing")
        missing = packages[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extractor = _Extractor(root / "cache", missing)
            content_root = root / "output" / "CodeVein2" / "Content"
            results = extract_packages(
                packages,
                game_root=root,
                content_root=content_root,
                game_version="local-unverified-test",
                data_revision="research-test",
                extractor=extractor,
            )

            self.assertEqual(len(results), 2)
            self.assertTrue(results[0].succeeded)
            self.assertEqual(
                {path.name for path in results[0].outputs},
                {"One.uasset", "One.uexp"},
            )
            self.assertEqual(results[0].outputs[0].parent, content_root / "Cinematics" / "Scenario")
            self.assertFalse(results[1].succeeded)
            self.assertIn("synthetic missing package", results[1].error or "")
            self.assertFalse((content_root / "Cinematics" / "Scenario" / "Missing.uasset").exists())
            self.assertEqual(extractor.calls[0], packages)
            self.assertEqual(extractor.calls[1:], [(packages[0],), (packages[1],)])


if __name__ == "__main__":
    unittest.main()
