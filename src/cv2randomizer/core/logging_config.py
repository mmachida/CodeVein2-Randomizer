"""Logging setup shared by the application entry points."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_log_dir

from cv2randomizer import APP_NAME

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_FILE_NAME = "log.txt"


# Resolve activity log path
def activity_log_path() -> Path:
    """Return the app's rolling activity log path."""
    return Path(user_log_dir(APP_NAME)) / _LOG_FILE_NAME


# Configure file and console logging
def configure_logging(log_path: Path | None = None) -> Path:
    """Write application logs to console and a small rolling file."""
    target = (log_path or activity_log_path()).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)
    if not any(
        isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
        for handler in root_logger.handlers
    ):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root_logger.addHandler(console)

    file_handler = next((
        handler for handler in root_logger.handlers
        if isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename).resolve() == target
    ), None)
    if file_handler is None:
        file_handler = RotatingFileHandler(
            target,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        root_logger.addHandler(file_handler)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    return target
