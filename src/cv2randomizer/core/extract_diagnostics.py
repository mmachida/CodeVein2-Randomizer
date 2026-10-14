"""Process-level memory telemetry for transactional game-data extraction."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .memory_budget import MemoryBudgetScheduler

LOGGER = logging.getLogger(__name__)
_PYTHON_PROCESS = re.compile(r"python", re.IGNORECASE)
_HEAVY_PROCESS = re.compile(r"retoc|uassetapiworldloot", re.IGNORECASE)


class ExtractMemoryDiagnostics:
    """Sample Python, helper, and system RAM without forwarding samples to the UI."""

    # Initialize this object
    def __init__(
        self,
        diagnostics_root: Path,
        scheduler: MemoryBudgetScheduler,
        *,
        sample_interval_seconds: float = 1.0,
    ) -> None:
        if sample_interval_seconds <= 0:
            raise ValueError("Memory sample interval must be positive.")
        self.diagnostics_root = diagnostics_root.expanduser().resolve()
        self.scheduler = scheduler
        self.sample_interval_seconds = sample_interval_seconds
        self._started = time.perf_counter()
        self._utc_started = datetime.now(UTC)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase = "starting"
        self._detail = ""
        self._samples: list[dict[str, Any]] = []
        self._peak_python_ws = 0
        self._peak_python_private = 0
        self._peak_child_ws = 0
        self._peak_child_private = 0
        self._peak_helper_ws = 0
        self._peak_helper_private = 0
        self._peak_helper_name: str | None = None
        self._peak_helper_pid: int | None = None
        self._peak_system_used = 0
        self._minimum_available: int | None = None
        self._total_physical: int | None = None
        self._max_heavy_workers = 0
        self._managed_heap_peak = 0
        self._managed_heap_size_peak = 0
        self._largest_job: dict[str, Any] | None = None
        self._max_elapsed_job = 0.0
        self._helper_jobs: list[dict[str, Any]] = []
        self._operations: list[dict[str, Any]] = []
        self._helper_launch_counts: dict[str, int] = {}

    # Start collecting diagnostics
    def start(self) -> None:
        self.diagnostics_root.mkdir(parents=True, exist_ok=True)
        self._sample_once()
        self._thread = threading.Thread(target=self._sample_loop, name="extract-memory-monitor", daemon=True)
        self._thread.start()

    # Update the current phase
    def set_phase(self, phase: str, current: int | None = None, total: int | None = None) -> None:
        detail = "" if current is None else f"{current}/{total if total is not None else '?'}"
        with self._lock:
            self._phase = str(phase)
            self._detail = detail

    # Set the current job label
    def set_job(self, phase: str, detail: str) -> None:
        with self._lock:
            self._phase = phase
            self._detail = detail

    # Record helper memory usage
    def observe_helper(self, job_key: str, metrics: object) -> None:
        if not isinstance(metrics, dict):
            return
        working_set = _positive_int(metrics.get("peakWorkingSetBytes")) or _positive_int(
            metrics.get("workingSetBytes")
        )
        private_bytes = _positive_int(metrics.get("privateBytes"))
        managed_heap = _positive_int(metrics.get("managedHeapBytes"))
        managed_heap_size = _positive_int(metrics.get("managedHeapSizeBytes"))
        elapsed_ms = _positive_int(metrics.get("elapsedMilliseconds"))
        if working_set:
            self.scheduler.record_peak(job_key, working_set)
        with self._lock:
            self._managed_heap_peak = max(self._managed_heap_peak, managed_heap or 0)
            self._managed_heap_size_peak = max(self._managed_heap_size_peak, managed_heap_size or 0)
            if elapsed_ms:
                self._max_elapsed_job = max(self._max_elapsed_job, elapsed_ms / 1000.0)
            package = metrics.get("package")
            if not isinstance(package, str):
                package = job_key.split(":", 1)[-1]
            operation = metrics.get("operation")
            if not isinstance(operation, str):
                operation = "uassetapi"
            self._helper_jobs.append({
                "package": package,
                "operation": operation,
                "reason": metrics.get("reason"),
                "systems": metrics.get("systems"),
                "load_ms": _positive_int(metrics.get("loadMilliseconds")),
                "analysis_ms": _positive_int(metrics.get("analysisMilliseconds")),
                "total_ms": elapsed_ms,
                "working_set_bytes": working_set,
                "private_bytes": private_bytes,
                "managed_heap_bytes": managed_heap,
                "managed_heap_size_bytes": managed_heap_size,
            })
            candidate = {
                "job": job_key,
                "working_set_bytes": working_set,
                "private_bytes": private_bytes,
                "managed_heap_bytes": managed_heap,
            }
            if working_set and (
                self._largest_job is None
                or working_set > int(self._largest_job.get("working_set_bytes") or 0)
            ):
                self._largest_job = candidate
        LOGGER.info(
            "Extract helper memory job=%s peak_working_set=%s private=%s managed_heap=%s heap_size=%s elapsed_ms=%s",
            job_key, working_set, private_bytes, managed_heap, managed_heap_size, elapsed_ms,
        )

    # Record operation
    def record_operation(self, operation: str, **fields: Any) -> None:
        """Record a measured Extract operation without changing UI phase state."""
        with self._lock:
            row = {"operation": str(operation), **fields}
            self._operations.append(row)
            if operation == "helper_launch":
                helper_name = str(fields.get("helper_operation", "unknown"))
                self._helper_launch_counts[helper_name] = self._helper_launch_counts.get(helper_name, 0) + 1

    # Record stage timing
    def record_stage(self, stage: str, elapsed_seconds: float, **fields: Any) -> None:
        self.record_operation(
            "stage", stage=str(stage), elapsed_seconds=max(0.0, float(elapsed_seconds)), **fields,
        )

    # Summarize recorded diagnostics
    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "started_at_utc": self._utc_started.isoformat(),
                "elapsed_seconds": round(time.perf_counter() - self._started, 3),
                "peak_python_working_set_bytes": self._peak_python_ws,
                "peak_python_private_bytes": self._peak_python_private,
                "peak_child_working_set_bytes": self._peak_child_ws,
                "peak_child_private_bytes": self._peak_child_private,
                "peak_helper_working_set_bytes": self._peak_helper_ws,
                "peak_helper_private_bytes": self._peak_helper_private,
                "peak_helper_name": self._peak_helper_name,
                "peak_helper_pid": self._peak_helper_pid,
                "peak_managed_heap_bytes": self._managed_heap_peak,
                "peak_managed_heap_size_bytes": self._managed_heap_size_peak,
                "peak_system_used_bytes": self._peak_system_used,
                "minimum_available_physical_bytes": self._minimum_available,
                "total_physical_bytes": self._total_physical,
                "maximum_heavy_workers": self._max_heavy_workers,
                "maximum_helper_elapsed_seconds": round(self._max_elapsed_job, 3),
                "largest_memory_job": self._largest_job,
                "scheduler_maximum_active_jobs": self.scheduler.maximum_active_jobs,
                "helper_process_launches": sum(self._helper_launch_counts.values()),
                "helper_launches_by_operation": dict(sorted(self._helper_launch_counts.items())),
                "helper_package_records": len(self._helper_jobs),
            }

    # Stop collecting diagnostics
    def finish(self, *, success: bool, error: str | None = None) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.sample_interval_seconds * 3))
        self._sample_once()
        report = {
            "schema_version": 1,
            "success": bool(success),
            "error": error,
            **self.summary(),
            "helper_jobs": tuple(self._helper_jobs),
            "operations": tuple(self._operations),
            "samples": tuple(self._samples),
        }
        timestamp = self._utc_started.strftime("%Y%m%dT%H%M%S%fZ")
        report_path = self.diagnostics_root / f"extract-{timestamp}-{uuid.uuid4().hex[:8]}.json"
        self._atomic_json(report_path, report)
        self._atomic_json(self.diagnostics_root / "latest-extract.json", report)
        summary = {key: value for key, value in report.items() if key != "samples"}
        LOGGER.info("Extract memory summary %s", json.dumps(summary, ensure_ascii=False, sort_keys=True))
        LOGGER.info("Extract memory samples saved to %s", report_path)
        return {**summary, "report_path": str(report_path)}

    # Sample memory in the background
    def _sample_loop(self) -> None:
        while not self._stop.wait(self.sample_interval_seconds):
            self._sample_once()

    # Capture one memory sample
    def _sample_once(self) -> None:
        try:
            memory = psutil.virtual_memory()
            root = psutil.Process(os.getpid())
            processes = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return

        python_ws = python_private = child_ws = child_private = 0
        helper_ws = helper_private = 0
        helper_name: str | None = None
        helper_pid: int | None = None
        process_rows: list[dict[str, Any]] = []
        heavy_pids: set[int] = set()
        for process in processes:
            try:
                info = process.memory_full_info()
                name = process.name()
                working_set = int(info.rss)
                private = int(getattr(info, "private", info.vms))
                command_line = " ".join(process.cmdline())
                is_python = bool(_PYTHON_PROCESS.search(name))
                is_helper = bool(_HEAVY_PROCESS.search(name)) or (
                    name.casefold() == "dotnet.exe"
                    and bool(re.search(
                        r"inspect-map-batch|blueprint-parents|prepare-world-loot-namemaps|"
                        r"prepare-map-binary-descriptors|prepare-healing-upgrades|to-legacy",
                        command_line,
                        re.I,
                    ))
                )
                if is_python:
                    python_ws += working_set
                    python_private += private
                elif process.pid != os.getpid():
                    child_ws += working_set
                    child_private += private
                if is_helper:
                    heavy_pids.add(process.pid)
                    if working_set > helper_ws:
                        helper_ws, helper_private = working_set, private
                        helper_name, helper_pid = name, process.pid
                process_rows.append({
                    "pid": process.pid,
                    "name": name,
                    "working_set_bytes": working_set,
                    "private_bytes": private,
                    "is_python": is_python,
                    "is_heavy_helper": is_helper,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue

        with self._lock:
            phase, detail = self._phase, self._detail
            self._peak_python_ws = max(self._peak_python_ws, python_ws)
            self._peak_python_private = max(self._peak_python_private, python_private)
            self._peak_child_ws = max(self._peak_child_ws, child_ws)
            self._peak_child_private = max(self._peak_child_private, child_private)
            if helper_ws > self._peak_helper_ws:
                self._peak_helper_ws = helper_ws
                self._peak_helper_private = helper_private
                self._peak_helper_name = helper_name
                self._peak_helper_pid = helper_pid
            total = int(memory.total)
            available = int(memory.available)
            used = total - available
            self._peak_system_used = max(self._peak_system_used, used)
            self._minimum_available = available if self._minimum_available is None else min(
                self._minimum_available, available
            )
            self._total_physical = total
            active_jobs = self.scheduler.active_jobs
            self._max_heavy_workers = max(
                self._max_heavy_workers,
                len(heavy_pids),
                self.scheduler.maximum_active_jobs,
            )
            sample = {
                "elapsed_seconds": round(time.perf_counter() - self._started, 3),
                "phase": phase,
                "current_package_or_batch": detail,
                "active_jobs": active_jobs,
                "python_working_set_bytes": python_ws,
                "python_private_bytes": python_private,
                "child_working_set_bytes": child_ws,
                "child_private_bytes": child_private,
                "helper_working_set_bytes": helper_ws,
                "helper_private_bytes": helper_private,
                "helper_name": helper_name,
                "helper_pid": helper_pid,
                "total_physical_bytes": total,
                "available_physical_bytes": available,
                "used_physical_bytes": used,
                "active_heavy_workers": len(heavy_pids),
                "processes": process_rows,
            }
            self._samples.append(sample)
        LOGGER.info("Extract memory sample %s", json.dumps(sample, ensure_ascii=False, sort_keys=True))

    # Write JSON atomically
    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


# Parse a positive integer
def _positive_int(value: object) -> int | None:
    return int(value) if type(value) is int and value > 0 else None
