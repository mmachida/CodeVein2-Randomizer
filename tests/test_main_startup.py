"""Smoke-test entry-point modes used by the release builder."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from cv2randomizer.main import main


class MainStartupTests(unittest.TestCase):
    # Check startup smoke flag exits before
    def test_startup_smoke_flag_exits_before_creating_the_ui(self) -> None:
        with (
            patch.object(sys, "argv", ["CodeVein2Randomizer-v1.0.0.exe", "--startup-smoke-test"]),
            patch("cv2randomizer.main.QApplication") as application,
            patch("cv2randomizer.main.QIcon") as icon,
            patch("cv2randomizer.main.resolve_app_icon", return_value=Path("icon.ico")),
            patch("cv2randomizer.main.resolve_cv2_usmap", return_value=Path(__file__)),
            patch("cv2randomizer.main.resolve_retoc_executable", return_value=Path(__file__)),
            patch("cv2randomizer.main.resolve_uassetapi_helper", return_value=Path(__file__)),
            patch("cv2randomizer.main.application_resource", return_value=Path(__file__)) as resource,
            patch("cv2randomizer.main.application_data_path", return_value=Path(__file__)),
            patch("cv2randomizer.main.MainWindow") as window,
            patch("cv2randomizer.main.configure_logging") as configure_logging,
        ):
            self.assertEqual(main(), 0)

        application.assert_called_once_with(["CodeVein2Randomizer-v1.0.0.exe"])
        application.return_value.setWindowIcon.assert_called_once_with(icon.return_value)
        icon.assert_called_once_with(str(Path("icon.ico")))
        resource.assert_any_call("ico", "icon.png")
        resource.assert_any_call("licenses", "THIRD-PARTY-NOTICES.txt")
        window.assert_not_called()
        configure_logging.assert_not_called()


if __name__ == "__main__":
    unittest.main()
