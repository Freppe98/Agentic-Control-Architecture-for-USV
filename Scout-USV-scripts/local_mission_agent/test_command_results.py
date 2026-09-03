"""
Standalone tests for command_results.py -- the persisted, keyed-by-
command_id authoritative terminal result store (requirements 1/2/4/5/6/8
of the buffering/dedup fix). Run directly:

    python3 test_command_results.py
"""
import json
import os
import tempfile
import unittest

import config
# Throwaway temp file, set before importing command_results. That module reads
# config.COMMAND_RESULTS_FILE dynamically per call, so this override always
# takes effect and the real command_results.json is never touched.
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")

import command_results


def tearDownModule():
    try:
        os.remove(config.COMMAND_RESULTS_FILE)
    except FileNotFoundError:
        pass


class TestCommandResults(unittest.TestCase):
    def setUp(self):
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)

    def test_get_stored_result_missing_returns_none(self):
        self.assertIsNone(command_results.get_stored_result("nope"))

    def test_store_then_get_round_trips_exactly(self):
        payload = {
            "command_id": "cid-1", "status": "executed",
            "result": {"accepted": False, "verified": False, "ack_result": "MAV_RESULT_FAILED",
                       "error": {"code": "ACK_REJECTED", "message": "Pixhawk rejected MAV_CMD_DO_SET_HOME: MAV_RESULT_FAILED"}},
        }
        command_results.store_result("cid-1", payload)
        self.assertEqual(command_results.get_stored_result("cid-1"), payload)

    def test_first_write_wins_second_store_is_a_no_op(self):
        """Requirement 8: the first terminal result for a command_id must
        never be discarded or replaced, even by a later explicit store call
        for the same command_id (e.g. a duplicate-path fallback)."""
        first = {"command_id": "cid-2", "status": "executed", "result": {"ok": True}}
        second = {"command_id": "cid-2", "status": "rejected", "reason": "duplicate command_id, already processed"}

        command_results.store_result("cid-2", first)
        command_results.store_result("cid-2", second)

        self.assertEqual(command_results.get_stored_result("cid-2"), first)

    def test_clear_result_removes_entry(self):
        command_results.store_result("cid-3", {"command_id": "cid-3", "status": "executed"})
        command_results.clear_result("cid-3")
        self.assertIsNone(command_results.get_stored_result("cid-3"))

    def test_clear_result_on_missing_id_is_a_no_op(self):
        command_results.clear_result("never-stored")  # must not raise

    def test_store_result_with_falsy_command_id_is_a_no_op(self):
        command_results.store_result(None, {"status": "executed"})
        command_results.store_result("", {"status": "executed"})
        self.assertEqual(command_results._read_all(), {})

    def test_persisted_to_disk_survives_a_simulated_restart(self):
        """Requirement 5: buffer deduplication must work across process
        restarts. command_results.py keeps no in-memory cache -- every call
        reads/writes COMMAND_RESULTS_FILE directly -- so a fresh read (as a
        newly started process would perform) must see exactly what an
        earlier "process" stored."""
        payload = {"command_id": "cid-restart", "status": "executed", "result": {"ok": True}}
        command_results.store_result("cid-restart", payload)

        with open(config.COMMAND_RESULTS_FILE, "r") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["cid-restart"], payload)

        # No module state to reset here (unlike an in-memory dict) --
        # get_stored_result reading it back straight from disk *is* the
        # restart-survival guarantee.
        self.assertEqual(command_results.get_stored_result("cid-restart"), payload)

    def test_bounded_by_max_tracked_command_ids_oldest_dropped_first(self):
        # command_results.py reads config.MAX_TRACKED_COMMAND_IDS dynamically
        # (no import-time snapshot), so overriding it on config -- the single
        # source of truth -- is what actually takes effect.
        original_max = config.MAX_TRACKED_COMMAND_IDS
        config.MAX_TRACKED_COMMAND_IDS = 3
        try:
            for i in range(5):
                command_results.store_result(f"cid-{i}", {"command_id": f"cid-{i}", "status": "executed"})
            remaining = command_results._read_all()
            self.assertEqual(len(remaining), 3)
            self.assertEqual(set(remaining.keys()), {"cid-2", "cid-3", "cid-4"})
        finally:
            config.MAX_TRACKED_COMMAND_IDS = original_max


if __name__ == "__main__":
    unittest.main(verbosity=2)
