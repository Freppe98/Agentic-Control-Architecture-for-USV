"""
Standalone tests for process_health.py -- this process's own CPU usage
(distinct from the vehicle Flask host's cpu/memory/storage). No pytest
dependency:

    python3 test_process_health.py
"""
import time
import unittest

import process_health


class TestProcessHealth(unittest.TestCase):
    def setUp(self):
        process_health._last_sample = None

    def test_first_call_returns_none_no_baseline_yet(self):
        self.assertIsNone(process_health.cpu_percent())

    def test_second_call_returns_a_number(self):
        process_health.cpu_percent()
        time.sleep(0.05)
        result = process_health.cpu_percent()
        self.assertIsInstance(result, float)
        self.assertGreaterEqual(result, 0.0)

    def test_reads_real_proc_stat_not_fabricated(self):
        self.assertIsNotNone(process_health._process_cpu_seconds())


if __name__ == "__main__":
    unittest.main(verbosity=2)
