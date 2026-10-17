from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from cv2randomizer.core.logging_config import configure_logging


class LoggingConfigTests(unittest.TestCase):
    # Check activity log file is written
    def test_configure_logging_writes_to_the_requested_log_file(self) -> None:
        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_level = root_logger.level
        root_logger.handlers.clear()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                try:
                    log_path = Path(temporary) / "Logs" / "log.txt"
                    configured_path = configure_logging(log_path)
                    logging.getLogger("cv2randomizer.test").error("test activity entry")
                    for handler in root_logger.handlers:
                        handler.flush()

                    self.assertEqual(configured_path, log_path.resolve())
                    self.assertIn("test activity entry", log_path.read_text(encoding="utf-8"))
                finally:
                    for handler in root_logger.handlers:
                        handler.close()
                    root_logger.handlers.clear()
        finally:
            root_logger.handlers[:] = original_handlers
            root_logger.setLevel(original_level)


if __name__ == "__main__":
    unittest.main()
