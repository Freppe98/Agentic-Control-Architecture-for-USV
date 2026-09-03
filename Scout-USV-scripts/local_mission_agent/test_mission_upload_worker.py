"""
Standalone tests for mission_upload_worker.py -- the bounded single-command
background worker that keeps a long MISSION_UPLOAD from freezing the Local
Agent's main reporting loop. No pytest dependency:

    python3 test_mission_upload_worker.py

run_fn/finalize_fn are injected fakes coordinated with threading.Events so the
test controls exactly when the "upload" is in flight vs done -- letting it
assert the idle -> executing -> idle lifecycle, that status() never blocks on
the upload, and the one-at-a-time boundedness.
"""
import threading
import time
import unittest

import mission_upload_worker as worker


def _cmd(command_id="up-1"):
    return {"command_id": command_id, "usv_id": "usv-2", "command_type": "MISSION_UPLOAD",
            "requested_by": "test", "params": {"waypoints": [{"latitude": 56.6, "longitude": 12.8}]}}


class TestUploadWorker(unittest.TestCase):
    def setUp(self):
        worker._reset_for_tests()

    def tearDown(self):
        worker._reset_for_tests()

    def test_idle_status_initially(self):
        s = worker.status()
        self.assertFalse(s["active"])
        self.assertEqual(s["state"], "idle")
        self.assertFalse(worker.is_busy())

    def test_runs_and_finalizes_then_returns_to_idle(self):
        finalized = {}
        done = threading.Event()

        def run_fn(command):
            return ({"command_id": command["command_id"], "status": "executed"},
                    {"type": "command_executed"})

        def finalize_fn(payload):
            finalized["payload"] = payload
            done.set()

        outcome = worker.try_start(_cmd(), run_fn, finalize_fn)
        self.assertEqual(outcome, "STARTED")
        self.assertTrue(done.wait(2.0), "worker never finalized")
        self.assertEqual(finalized["payload"]["status"], "executed")

        # Give the thread a moment to clear the slot after finalize.
        for _ in range(200):
            if not worker.is_busy():
                break
            time.sleep(0.005)
        self.assertFalse(worker.is_busy(), "worker slot must free after completion")

    def test_bounded_second_upload_is_busy_and_run_fn_not_called_twice(self):
        run_calls = []
        release = threading.Event()
        started = threading.Event()

        def run_fn(command):
            run_calls.append(command["command_id"])
            started.set()
            release.wait(2.0)   # hold the worker "in flight"
            return ({"command_id": command["command_id"], "status": "executed"}, {"type": "x"})

        def finalize_fn(payload):
            pass

        first = worker.try_start(_cmd("up-A"), run_fn, finalize_fn)
        self.assertEqual(first, "STARTED")
        self.assertTrue(started.wait(2.0))

        # While the first is still running, a second must be refused.
        second = worker.try_start(_cmd("up-B"), run_fn, finalize_fn)
        self.assertEqual(second, "BUSY")

        s = worker.status()          # status must not block on the upload
        self.assertTrue(s["active"])
        self.assertEqual(s["state"], "executing")
        self.assertEqual(s["command_id"], "up-A")

        release.set()
        for _ in range(400):
            if not worker.is_busy():
                break
            time.sleep(0.005)
        self.assertFalse(worker.is_busy())
        self.assertEqual(run_calls, ["up-A"], "the busy upload must never have run twice")

    def test_run_fn_exception_still_finalizes_a_failed_result(self):
        finalized = {}
        done = threading.Event()

        def run_fn(command):
            raise RuntimeError("boom")

        def finalize_fn(payload):
            finalized["payload"] = payload
            done.set()

        worker.try_start(_cmd("up-crash"), run_fn, finalize_fn)
        self.assertTrue(done.wait(2.0), "a crashing run_fn must still produce a terminal result")
        self.assertEqual(finalized["payload"]["status"], "failed")
        self.assertIn("boom", finalized["payload"]["reason"])
        # And the slot is freed.
        for _ in range(200):
            if not worker.is_busy():
                break
            time.sleep(0.005)
        self.assertFalse(worker.is_busy())


if __name__ == "__main__":
    unittest.main(verbosity=2)
