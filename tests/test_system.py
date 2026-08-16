"""Host stats parsing. Pure once the file contents are handed in.

These matter because a stats panel must degrade rather than raise: it is a
convenience, and it must never be able to take down a transcoder.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import system  # noqa: E402

PROC_STAT_1 = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3 4\n"
# 100 more busy (50 user + 50 system), 100 more idle -> 50%
PROC_STAT_2 = "cpu  150 0 150 900 0 0 0 0 0 0\ncpu0 1 2 3 4\n"

MEMINFO = """MemTotal:       65817696 kB
MemFree:         1000000 kB
MemAvailable:   57705476 kB
"""


class Cpu(unittest.TestCase):
    def setUp(self):
        system._prev_cpu = None

    def read(self, mapping):
        return lambda path: mapping.get(path)

    def test_the_first_reading_admits_it_cannot_know_yet(self):
        # /proc/stat is cumulative since boot, so one sample says nothing about
        # now. Reporting a number here would be inventing one.
        with mock.patch.object(system, "_read", self.read({"/proc/stat": PROC_STAT_1})):
            self.assertIsNone(system.cpu()["percent"])

    def test_the_second_reading_is_the_delta_between_them(self):
        with mock.patch.object(system, "_read", self.read({"/proc/stat": PROC_STAT_1})):
            system.cpu()
        with mock.patch.object(system, "_read", self.read({"/proc/stat": PROC_STAT_2})):
            self.assertEqual(system.cpu()["percent"], 50.0)

    def test_iowait_counts_as_idle_not_busy(self):
        # A NAS waiting on spindles is not a NAS short of CPU, and calling it
        # busy would argue against raising concurrency for the wrong reason.
        system._prev_cpu = None
        with mock.patch.object(system, "_read", self.read({"/proc/stat": "cpu 100 0 100 800 0 0 0 0 0 0\n"})):
            system.cpu()
        with mock.patch.object(system, "_read", self.read({"/proc/stat": "cpu 100 0 100 800 200 0 0 0 0 0\n"})):
            self.assertEqual(system.cpu()["percent"], 0.0)

    def test_an_unreadable_proc_returns_none_rather_than_raising(self):
        with mock.patch.object(system, "_read", lambda p: None):
            self.assertIsNone(system.cpu())
            self.assertIsNone(system.memory())
            self.assertIsNone(system.load())


class Memory(unittest.TestCase):
    def test_available_is_preferred_over_free(self):
        # MemFree excludes reclaimable cache and makes every busy NAS look
        # full; MemAvailable is the kernel's own estimate of what is gettable.
        with mock.patch.object(system, "_read", lambda p: MEMINFO if p == "/proc/meminfo" else None):
            m = system.memory()
        self.assertEqual(m["total"], 65817696 * 1024)
        self.assertEqual(m["available"], 57705476 * 1024)
        self.assertAlmostEqual(m["percent"], 12.3, places=1)

    def test_a_meminfo_without_a_total_is_refused(self):
        with mock.patch.object(system, "_read", lambda p: "Bogus: 1 kB\n"):
            self.assertIsNone(system.memory())


class Load(unittest.TestCase):
    def test_load_is_reported_per_thread_so_it_means_something(self):
        # "12.06" is alarming on 8 threads and unremarkable on 64. The ratio is
        # the number worth showing.
        with mock.patch.object(system, "_read", lambda p: "12.06 11.58 11.02 11/1756 729\n"):
            with mock.patch.object(os, "cpu_count", lambda: 8):
                l = system.load()
        self.assertEqual(l["one"], 12.06)
        self.assertEqual(l["cores"], 8)
        self.assertAlmostEqual(l["per_core"], 1.51, places=2)

    def test_garbage_is_refused(self):
        with mock.patch.object(system, "_read", lambda p: "not a load average\n"):
            self.assertIsNone(system.load())


class Gpu(unittest.TestCase):
    def fake_run(self, stdout, code=0):
        return lambda *a, **k: mock.Mock(returncode=code, stdout=stdout, stderr="")

    def test_a_real_nvidia_smi_line_is_parsed(self):
        # Exactly what the T1000 returned on the live box.
        line = "NVIDIA T1000 8GB, 30, 7, 166, 8192, 68, 1, 246\n"
        with mock.patch.object(system.subprocess, "run", self.fake_run(line)):
            g = system.gpu()
        self.assertEqual(g["name"], "NVIDIA T1000 8GB")
        self.assertEqual(g["percent"], 30.0)
        self.assertEqual(g["temperature_c"], 68.0)
        self.assertEqual(g["encoder_sessions"], 1.0)

    def test_no_nvidia_smi_is_not_an_error(self):
        def boom(*a, **k):
            raise FileNotFoundError("nvidia-smi")
        with mock.patch.object(system.subprocess, "run", boom):
            self.assertIsNone(system.gpu())

    def test_a_failing_nvidia_smi_is_not_an_error(self):
        with mock.patch.object(system.subprocess, "run", self.fake_run("", code=9)):
            self.assertIsNone(system.gpu())

    def test_unparseable_numbers_become_none_rather_than_crashing(self):
        line = "GPU, [N/A], [N/A], 0, 0, [N/A], [N/A], [N/A]\n"
        with mock.patch.object(system.subprocess, "run", self.fake_run(line)):
            g = system.gpu()
        self.assertEqual(g["name"], "GPU")
        self.assertIsNone(g["percent"])


class Snapshot(unittest.TestCase):
    def test_a_snapshot_never_raises_even_with_nothing_readable(self):
        with mock.patch.object(system, "_read", lambda p: None):
            with mock.patch.object(system.subprocess, "run", mock.Mock(side_effect=OSError)):
                snap = system.snapshot()
        self.assertIn("note", snap)
        self.assertIsNone(snap["cpu"])


if __name__ == "__main__":
    unittest.main()
