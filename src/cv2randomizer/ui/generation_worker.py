"""Cancellable background task runner for long randomizer operations."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

from PySide6.QtCore import QThread, Signal

from cv2randomizer.generation.pipeline import GenerationError

LOGGER = logging.getLogger(__name__)
Progress = Callable[[str, int, int], None]
TaskOperation = Callable[[Progress], object]


class GenerationCancelled(GenerationError):
    """Raised at a pipeline checkpoint after a user requests cancellation."""


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    result: object | None = None
    error: Exception | None = None


class GenerationWorker(QThread):
    """Run generation or another long action without blocking the Qt event loop."""

    progress = Signal(str, int, int)
    completed = Signal(object)

    # Initialize this object
    def __init__(self, operation: TaskOperation, parent=None) -> None:
        super().__init__(parent)
        self._operation = operation
        self._cancel_requested = Event()

    # Build request cancel
    def request_cancel(self) -> None:
        self._cancel_requested.set()

    # Report progress
    def _report_progress(self, stage: str, current: int, total: int) -> None:
        if self._cancel_requested.is_set():
            raise GenerationCancelled("Generation was cancelled by the user.")
        self.progress.emit(stage, current, total)

    # Run the requested background task
    def run(self) -> None:
        try:
            result = self._operation(self._report_progress)
        except Exception as error:
            if not isinstance(error, GenerationCancelled):
                LOGGER.exception("Background randomizer task failed")
            self.completed.emit(TaskOutcome(error=error))
        else:
            self.completed.emit(TaskOutcome(result=result))
