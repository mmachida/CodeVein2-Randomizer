"""Tests for background child-process launching from the desktop app."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from cv2randomizer.core.subprocesses import run_hidden


class HiddenSubprocessTests(unittest.TestCase):
    # Check windows processes do not create
    def test_windows_processes_do_not_create_console_windows(self) -> None:
        completed = subprocess.CompletedProcess(["helper.exe"], 0, "ok", "")
        with (
            patch("cv2randomizer.core.subprocesses.os.name", "nt"),
            patch("cv2randomizer.core.subprocesses.subprocess.run", return_value=completed) as runner,
        ):
            result = run_hidden(["helper.exe"], check=False, capture_output=True, creationflags=0x2)

        self.assertIs(result, completed)
        self.assertEqual(runner.call_args.kwargs["creationflags"], 0x2 | 0x08000000)
        self.assertTrue(runner.call_args.kwargs["capture_output"])

    # Check non windows process options are
    def test_non_windows_process_options_are_unchanged(self) -> None:
        completed = subprocess.CompletedProcess(["helper"], 0, "ok", "")
        with (
            patch("cv2randomizer.core.subprocesses.os.name", "posix"),
            patch("cv2randomizer.core.subprocesses.subprocess.run", return_value=completed) as runner,
        ):
            result = run_hidden(["helper"], check=False, capture_output=True)

        self.assertIs(result, completed)
        self.assertNotIn("creationflags", runner.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
