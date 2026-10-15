"""Application entry point."""

import logging
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from cv2randomizer.core.logging_config import configure_logging
from cv2randomizer.core.resources import (
    application_data_path,
    application_resource,
    resolve_app_icon,
    resolve_cv2_usmap,
    resolve_retoc_executable,
    resolve_uassetapi_helper,
)
from cv2randomizer.ui.main_window import MainWindow


# Start the application
def main() -> int:
    """Configure the application and start its Qt event loop."""
    if "--startup-smoke-test" in sys.argv[1:]:
        app = QApplication([sys.argv[0]])
        app.setWindowIcon(QIcon(str(resolve_app_icon())))
        required_resources = (
            resolve_cv2_usmap(),
            resolve_retoc_executable(),
            resolve_uassetapi_helper(),
            application_resource("tools", "oo2core_9_win64.dll"),
            application_resource("licenses", "THIRD-PARTY-NOTICES.txt"),
            application_resource("ico", "icon.png"),
            application_data_path("bFormae", "current", "bequeathed_formae.json"),
            application_data_path("blood_codes", "current", "manifest.json"),
            application_data_path("blood_codes", "current", "blood_code_grants.json"),
            application_data_path("boosters", "current", "manifest.json"),
            application_data_path("catalogs", "current", "manifest.json"),
            application_data_path("content_availability", "current.json"),
            application_data_path("content_classification", "current", "rules.json"),
            application_data_path("defensive_gadgets", "current", "defensive_gadgets.json"),
            application_data_path("enemy_drops", "manifest.json"),
            application_data_path("extractor", "current", "manifest.json"),
            application_data_path("field_enemy", "current", "manifest.json"),
            application_data_path("jails", "current", "jails.json"),
            application_data_path("reward_pools", "current", "manifest.json"),
        )
        missing = tuple(path for path in required_resources if not path.is_file() or path.stat().st_size <= 0)
        if missing:
            raise RuntimeError(
                "Application bundle is missing required resources: "
                + ", ".join(str(path) for path in missing)
            )
        return 0

    configure_logging()
    logging.getLogger(__name__).info("Starting Code Vein 2 Randomizer")

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(resolve_app_icon())))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
