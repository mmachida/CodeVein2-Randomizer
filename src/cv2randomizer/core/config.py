"""Persistent user settings stored outside the project and game folders."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

from cv2randomizer import APP_NAME
from cv2randomizer.core.models import AppSettings


class SettingsError(ValueError):
    """Raised when settings cannot be read or written safely."""


class SettingsStore:
    """Load and atomically save the user's JSON settings file."""

    # Initialize this object
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(user_config_dir(APP_NAME)) / "settings.json"

    # Load and validate catalog data
    def load(self) -> AppSettings:
        """Read settings, returning defaults when no file exists."""
        if not self.path.exists():
            return AppSettings()
        try:
            data: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Settings root must be an object.")
            return AppSettings.from_dict(data)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise SettingsError(f"Could not read settings at {self.path}: {error}") from error

    # Save the current settings
    def save(self, settings: AppSettings) -> None:
        """Write a complete settings file and atomically replace the old one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(settings.to_dict(), temporary_file, indent=2, ensure_ascii=False)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except (OSError, TypeError, ValueError) as error:
            raise SettingsError(f"Could not save settings at {self.path}: {error}") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
