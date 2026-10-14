"""Launch helper tools without flashing console windows from the GUI app."""

from __future__ import annotations

import os
import subprocess
from typing import Any

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


# Run hidden
def run_hidden(*popenargs: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a child process while keeping Windows console windows hidden.

    The caller still captures stdout/stderr as usual, so diagnostics remain
    available in the activity log and error messages.
    """
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
    return subprocess.run(*popenargs, **kwargs)
