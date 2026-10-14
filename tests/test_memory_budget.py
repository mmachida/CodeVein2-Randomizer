from __future__ import annotations

import threading
import unittest

from cv2randomizer.core.memory_budget import GIB, MemoryBudgetScheduler


class _Memory:
    # Initialize this object
    def __init__(self, total: int, available: int) -> None:
        self.total = total
        self.available = available


class MemoryBudgetSchedulerTests(unittest.TestCase):
    # Check worker limit use physical memory
    def test_worker_limit_uses_physical_memory_and_available_reserve(self) -> None:
        memory = _Memory(total=15 * GIB, available=12 * GIB)
        scheduler = MemoryBudgetScheduler(memory_reader=lambda: memory)
        self.assertEqual(scheduler.recommended_worker_count([2 * GIB, 3 * GIB]), 2)
        with scheduler.job("map-a", estimated_bytes=2 * GIB):
            with scheduler.job("map-b", estimated_bytes=2 * GIB):
                self.assertEqual(scheduler.active_jobs, ("map-a", "map-b"))
                self.assertEqual(scheduler.maximum_active_jobs, 2)

    # Check nominal 8 16 24 32
    def test_nominal_8_16_24_and_32_gb_machines_get_bounded_worker_bands(self) -> None:
        cases = (
            (int(7.45 * GIB), 6 * GIB, 1),
            (15 * GIB, 12 * GIB, 2),
            (int(23.0 * GIB), 20 * GIB, 3),
            (int(31.09 * GIB), 24 * GIB, 4),
        )
        for total, available, expected_workers in cases:
            with self.subTest(total_gib=total / GIB):
                memory = _Memory(total=total, available=available)
                scheduler = MemoryBudgetScheduler(memory_reader=lambda: memory)
                self.assertEqual(scheduler.recommended_worker_count([2 * GIB]), expected_workers)

    # Check nominal 8 gb machine can
    def test_nominal_8_gb_machine_can_admit_one_heavy_job_with_four_gib_available(self) -> None:
        memory = _Memory(total=int(7.45 * GIB), available=4 * GIB)
        scheduler = MemoryBudgetScheduler(memory_reader=lambda: memory)

        self.assertEqual(
            scheduler.recommended_worker_count([scheduler.default_job_bytes]),
            1,
        )
        with scheduler.job("small-machine-map", estimated_bytes=scheduler.default_job_bytes):
            self.assertEqual(scheduler.active_jobs, ("small-machine-map",))

    # Check 32 gb machine can admit
    def test_32_gb_machine_can_admit_one_heavy_job_with_6814_mib_available(self) -> None:
        memory = _Memory(total=int(31.09 * GIB), available=6814 * 1024**2)
        scheduler = MemoryBudgetScheduler(memory_reader=lambda: memory)

        self.assertEqual(scheduler.reserve_bytes(memory.total), 3 * GIB)
        with scheduler.job("reported-low-available-memory", estimated_bytes=scheduler.default_job_bytes):
            self.assertEqual(scheduler.active_jobs, ("reported-low-available-memory",))

    # Check low memory still runs one
    def test_low_memory_still_runs_one_job_below_estimate_and_reserve(self) -> None:
        memory = _Memory(total=8 * GIB, available=512 * 1024**2)
        scheduler = MemoryBudgetScheduler(memory_reader=lambda: memory)

        self.assertEqual(scheduler.recommended_worker_count([2 * GIB]), 1)
        with scheduler.job("low-memory-map", estimated_bytes=2 * GIB):
            self.assertEqual(scheduler.active_jobs, ("low-memory-map",))

    # Check low memory serializes jobs until
    def test_low_memory_serializes_jobs_until_the_active_job_finishes(self) -> None:
        memory = _Memory(total=16 * GIB, available=512 * 1024**2)
        scheduler = MemoryBudgetScheduler(memory_reader=lambda: memory, maximum_workers=2)
        second_started = threading.Event()
        second_finished = threading.Event()
        errors: list[BaseException] = []

        # Run second
        def run_second() -> None:
            try:
                with scheduler.job("second", estimated_bytes=2 * GIB):
                    second_started.set()
            except BaseException as error:
                errors.append(error)
            finally:
                second_finished.set()

        with scheduler.job("first", estimated_bytes=2 * GIB):
            worker = threading.Thread(target=run_second)
            worker.start()
            self.assertFalse(second_started.wait(0.05))

        self.assertTrue(second_started.wait(2))
        self.assertTrue(second_finished.wait(2))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    # Check observed helper peak increases future
    def test_observed_helper_peak_increases_its_future_reservation(self) -> None:
        scheduler = MemoryBudgetScheduler()
        scheduler.record_peak("map-a", 3 * GIB)
        self.assertEqual(scheduler.estimate_job_bytes("map-a"), int(3.75 * GIB))


if __name__ == "__main__":
    unittest.main()
