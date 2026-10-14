from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from cv2randomizer.core.resources import (
    ApplicationResourceError,
    application_data_path,
    cv2_usmap_sha256,
    resolve_app_icon,
    resolve_cv2_usmap,
    resolve_uassetapi_helper,
    retoc_environment,
    uassetapi_command,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_MAPPING = REPOSITORY_ROOT / "workspace" / "research_assets" / "CV2.usmap"


class ApplicationResourceTests(unittest.TestCase):
    # Check application icon resolve project resources
    def test_application_icon_resolves_from_project_resources(self) -> None:
        self.assertEqual(resolve_app_icon(), REPOSITORY_ROOT / "ico" / "icon.ico")
        self.assertGreater(resolve_app_icon().stat().st_size, 0)

    # Check runtime catalog path is rooted
    def test_runtime_catalog_path_is_rooted_in_the_application(self) -> None:
        self.assertEqual(application_data_path("catalogs").resolve(), (REPOSITORY_ROOT / "data" / "catalogs").resolve())

    # Check frozen application resolve packaged helper
    def test_frozen_application_resolves_packaged_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(root), create=True):
                self.assertEqual(resolve_uassetapi_helper(), root / "tools" / "UAssetAPIWorldLoot.exe")

    # Check helper commands support packaged executable
    def test_helper_commands_support_packaged_executable_and_dev_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "UAssetAPIWorldLoot.exe"
            self.assertEqual(uassetapi_command(helper, "dotnet", "inspect", "input"), [str(helper.resolve()), "inspect", "input"])
            project = root / "UAssetAPIWorldLoot.csproj"
            self.assertEqual(
                uassetapi_command(project, "dotnet", "inspect", "input", no_restore=True),
                ["dotnet", "run", "--configuration", "Release", "--no-restore", "--project", str(project.resolve()), "--", "inspect", "input"],
            )

    # Check retoc resolve oodle supported game
    def test_retoc_resolves_oodle_from_supported_game_layouts(self) -> None:
        layouts = (
            Path("CodeVein2/Binaries/Win64"),
            Path("Binaries/Win64"),
            Path("Engine/Binaries/ThirdParty/Oodle/Win64"),
        )
        for layout in layouts:
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                oodle = root / layout
                oodle.mkdir(parents=True)
                dll = oodle / "oo2core_9_win64.dll"
                dll.write_bytes(b"test oodle binary")
                executable = root / "CodeVein2" / "Binaries" / "Win64" / "CodeVein2-Win64-Shipping.exe"
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_bytes(b"test game executable")

                environment = retoc_environment(root, required=True, executable_path=executable)

                assert environment is not None
                self.assertEqual(environment["PATH"].split(";", 1)[0], str(oodle.resolve()))
                self.assertEqual(Path(environment["PATH"].split(";", 1)[0]) / "oo2core_9_win64.dll", dll.resolve())

    # Check retoc prefers detected executable directory
    def test_retoc_prefers_the_detected_executable_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            executable = root / "CodeVein2" / "Binaries" / "Win64" / "CodeVein2-Win64-Shipping.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"game")
            dll = executable.parent / "oo2core_9_win64.dll"
            dll.write_bytes(b"oodle")
            fallback = root / "Engine" / "Binaries" / "ThirdParty" / "Oodle" / "Win64"
            fallback.mkdir(parents=True)
            (fallback / "oo2core_9_win64.dll").write_bytes(b"other copy")

            environment = retoc_environment(root, required=True, executable_path=executable)

            assert environment is not None
            self.assertEqual(environment["PATH"].split(";", 1)[0], str(executable.parent.resolve()))

    # Check retoc use explicit app local
    def test_retoc_uses_explicit_app_local_dll_when_game_install_has_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            game = root / "game"
            app = root / "app"
            app_tools = app / "tools"
            app_tools.mkdir(parents=True)
            local_dll = app_tools / "oo2core_9_win64.dll"
            local_dll.write_bytes(b"user-provided local oodle")
            executable = game / "CodeVein2" / "Binaries" / "Win64" / "CodeVein2-Win64-Shipping.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"game")

            with patch("cv2randomizer.core.resources.application_root", return_value=app):
                environment = retoc_environment(game, required=True, executable_path=executable)

            assert environment is not None
            self.assertEqual(environment["PATH"].split(";", 1)[0], str(app_tools.resolve()))

    # Check unrelated validation generated workspace does
    def test_unrelated_validation_or_generated_workspace_does_not_affect_oodle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            game = root / "game"
            oodle = game / "CodeVein2" / "Binaries" / "Win64"
            oodle.mkdir(parents=True)
            (oodle / "oo2core_9_win64.dll").write_bytes(b"game-owned oodle")
            executable = oodle / "CodeVein2-Win64-Shipping.exe"
            executable.write_bytes(b"game")
            unrelated_workspace = root / "workspace" / "phase17-validation" / "generated"

            before = retoc_environment(game, required=True, executable_path=executable)
            unrelated_workspace.mkdir(parents=True)
            (unrelated_workspace / "unrelated.marker").write_text("ignored", encoding="utf-8")
            during = retoc_environment(game, required=True, executable_path=executable)
            shutil.rmtree(root / "workspace")
            after = retoc_environment(game, required=True, executable_path=executable)

            assert before is not None and during is not None and after is not None
            self.assertEqual(before["PATH"].split(";", 1)[0], str(oodle.resolve()))
            self.assertEqual(during["PATH"].split(";", 1)[0], before["PATH"].split(";", 1)[0])
            self.assertEqual(after["PATH"].split(";", 1)[0], before["PATH"].split(";", 1)[0])

    # Check missing oodle error lists game
    def test_missing_oodle_error_lists_game_executable_and_checked_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            executable = root / "CodeVein2" / "Binaries" / "Win64" / "CodeVein2-Win64-Shipping.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"game")
            with patch("cv2randomizer.core.resources.application_root", return_value=root):
                with self.assertRaises(ApplicationResourceError) as caught:
                    retoc_environment(root, required=True, executable_path=executable)

            message = str(caught.exception)
            self.assertIn(str(root), message)
            self.assertIn(str(executable.resolve()), message)
            self.assertIn(str(executable.parent / "oo2core_9_win64.dll"), message)
            self.assertIn("Engine\\Binaries\\ThirdParty\\Oodle\\Win64", message)
            self.assertIn(str(root / "tools" / "oo2core_9_win64.dll"), message)

    # Check resolve canonical mapping without using
    def test_resolves_canonical_mapping_without_using_the_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "workspace" / "research_assets" / "CV2.usmap"
            destination.parent.mkdir(parents=True)
            shutil.copy2(CANONICAL_MAPPING, destination)

            self.assertEqual(resolve_cv2_usmap(root=root), destination.resolve())
            self.assertEqual(len(cv2_usmap_sha256(destination)), 64)

    # Check packaged internal resource layout is
    def test_packaged_internal_resource_layout_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "_internal" / "workspace" / "research_assets" / "CV2.usmap"
            destination.parent.mkdir(parents=True)
            shutil.copy2(CANONICAL_MAPPING, destination)

            self.assertEqual(resolve_cv2_usmap(root=root), destination.resolve())

    # Check missing invalid mapping fail application
    def test_missing_or_invalid_mapping_fails_with_an_application_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ApplicationResourceError, "missing or unreadable"):
                resolve_cv2_usmap(root=root)

            invalid = root / "workspace" / "research_assets" / "CV2.usmap"
            invalid.parent.mkdir(parents=True)
            invalid.write_bytes(b"not a usmap")
            with self.assertRaisesRegex(ApplicationResourceError, "missing or unreadable"):
                resolve_cv2_usmap(root=root)


if __name__ == "__main__":
    unittest.main()
