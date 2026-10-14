"""RAM-aware concurrency control for memory-heavy Unreal package work."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Iterator

import psutil

GIB = 1024**3
DEFAULT_UASSET_JOB_BYTES = int(2.5 * GIB)
SAFETY_RESERVE_BYTES = 1 * GIB
SAFETY_RESERVE_RATIO = 0.15
SAFETY_RESERVE_CAP_BYTES = 3 * GIB
MAX_HEAVY_WORKERS = 4


@dataclass(frozen=True, slots=True)
class MemoryStatus:
    total_bytes: int
    available_bytes: int


class MemoryBudgetScheduler:
    """Use physical RAM, a parallelism reserve, and observed costs to limit workers."""

    # Initialize this object
    def __init__(
        self,
        *,
        memory_reader=None,
        poll_interval_seconds: float = 0.5,
        default_job_bytes: int = DEFAULT_UASSET_JOB_BYTES,
        maximum_workers: int = MAX_HEAVY_WORKERS,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Memory scheduler polling interval must be positive.")
        if default_job_bytes <= 0 or maximum_workers <= 0:
            raise ValueError("Memory scheduler job estimate and worker limit must be positive.")
        self._memory_reader = memory_reader or psutil.virtual_memory
        self.poll_interval_seconds = poll_interval_seconds
        self.default_job_bytes = default_job_bytes
        self.maximum_workers = maximum_workers
        self._lock = threading.RLock()
        self._active: dict[str, int] = {}
        self._observed_peaks: dict[str, int] = {}
        self._maximum_active_jobs = 0

    # Count reserve bytes
    @staticmethod
    def reserve_bytes(total_bytes: int) -> int:
        proportional = max(SAFETY_RESERVE_BYTES, int(total_bytes * SAFETY_RESERVE_RATIO))
        return min(SAFETY_RESERVE_CAP_BYTES, proportional)

    # Resolve physical worker cap
    @staticmethod
    def _physical_worker_cap(total_bytes: int, maximum_workers: int) -> int:
        # Round up physical-RAM bands so a nominal 16 GB machine (about 15 GiB)
        # can use two lanes when available RAM and job estimates permit it.
        eight_gib_bands = (max(0, int(total_bytes)) + 8 * GIB - 1) // (8 * GIB)
        return min(maximum_workers, max(1, eight_gib_bands))

    # Return the current status
    def status(self) -> MemoryStatus:
        memory = self._memory_reader()
        return MemoryStatus(int(memory.total), int(memory.available))

    # Describe estimate job bytes
    def estimate_job_bytes(self, job_key: str, package_bytes: int = 0) -> int:
        with self._lock:
            observed = self._observed_peaks.get(job_key.casefold())
        learned = int(observed * 1.25) if observed else 0
        size_estimate = max(0, int(package_bytes)) * 8
        return max(self.default_job_bytes, learned, size_estimate)

    # Describe recommended worker count
    def recommended_worker_count(self, job_estimates: tuple[int, ...] | list[int] = ()) -> int:
        """Return at least one worker; RAM only reduces optional parallelism."""
        status = self.status()
        reserve = self.reserve_bytes(status.total_bytes)
        physical_cap = self._physical_worker_cap(status.total_bytes, self.maximum_workers)
        estimate = min(job_estimates) if job_estimates else self.default_job_bytes
        with self._lock:
            already_reserved = sum(self._active.values())
            active_count = len(self._active)
        budget = max(0, status.available_bytes - already_reserved - reserve)
        budget_cap = budget // max(1, estimate)
        return max(1, active_count, min(physical_cap, int(budget_cap)))

    # Return active job count
    @property
    def active_jobs(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active, key=str.casefold))

    # Describe maximum active jobs
    @property
    def maximum_active_jobs(self) -> int:
        with self._lock:
            return self._maximum_active_jobs

    # Record the highest memory use
    def record_peak(self, job_key: str, working_set_bytes: int) -> None:
        if working_set_bytes <= 0:
            return
        key = job_key.casefold()
        with self._lock:
            self._observed_peaks[key] = max(self._observed_peaks.get(key, 0), int(working_set_bytes))

    # Read job
    @contextmanager
    def job(
        self,
        job_key: str,
        *,
        package_bytes: int = 0,
        estimated_bytes: int | None = None,
    ) -> Iterator[int]:
        """Reserve a worker lane; memory pressure serializes work instead of rejecting it."""
        if not isinstance(job_key, str) or not job_key.strip():
            raise ValueError("Memory scheduler job key must be a non-empty string.")
        estimate = self.estimate_job_bytes(job_key, package_bytes) if estimated_bytes is None else int(estimated_bytes)
        if estimate <= 0:
            raise ValueError("Memory scheduler estimate must be positive.")
        self._acquire(job_key, estimate)
        try:
            yield estimate
        finally:
            with self._lock:
                self._active.pop(job_key, None)

    # Reserve memory for a task
    def _acquire(self, job_key: str, estimate: int) -> None:
        while True:
            status = self.status()
            reserve = self.reserve_bytes(status.total_bytes)
            physical_cap = self._physical_worker_cap(status.total_bytes, self.maximum_workers)
            with self._lock:
                reserved = sum(self._active.values())
                active_count = len(self._active)
                has_lane = len(self._active) < physical_cap
                enough_ram = status.available_bytes - reserved >= reserve + estimate
                # Estimates and the reserve control concurrency, never whether
                # the only worker may proceed. Windows can page under pressure;
                # if memory is genuinely exhausted, the helper reports its own
                # failure and the caller's transactional cache remains intact.
                if has_lane and (enough_ram or active_count == 0):
                    self._active[job_key] = estimate
                    self._maximum_active_jobs = max(self._maximum_active_jobs, len(self._active))
                    return
            time.sleep(self.poll_interval_seconds)
