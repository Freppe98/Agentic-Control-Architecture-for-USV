"""
Tests for the MISSION_CLEAR operator command on the Local Agent side -- its
command_executor spec, its normalization, and the safety gates it does and
does not inherit. Run directly:

    python3 test_mission_clear_command.py

The Operator Station already exposes MISSION_CLEAR, so Scout must speak it.
The property that matters most here mirrors MISSION_UPLOAD's: the terminal
status keys off `verified` (the vehicle Flask service proved by fresh readback
that no route remains), never off `cleared` (MISSION_CLEAR_ALL was merely
sent).
"""
import os
import tempfile
import time
import unittest
import uuid

import config
config.COMMAND_LOG_FILE = tempfile.mktemp(suffix=".jsonl")
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")

import command_executor
import command_normalization
from command_handler import process_command


def _clear_command(command_id=None):
    return {
        "command_id": command_id or str(uuid.uuid4()),
        "usv_id": "usv-2",
        "command_type": "MISSION_CLEAR",
        "issued_at": time.time(),
        "expires_at": time.time() + 600,
        "params": {},
        "requested_by": "operator",
    }


def _clear_response(verified=True, cleared=True, accepted=True,
                    pixhawk_item_count=0, error=None):
    return {
        "contract_version": "mission-contract-v1",
        "accepted": accepted,
        "cleared": cleared,
        "verified": verified,
        "observed_pixhawk_item_count": pixhawk_item_count,
        "observed_route_waypoint_count": max(0, (pixhawk_item_count or 0) - 1),
        "empty_representation": "NO_ITEMS" if pixhawk_item_count == 0 else None,
        "acknowledgement": "MAV_MISSION_ACCEPTED",
        "error": error,
    }


class TestCommandSpec(unittest.TestCase):
    def test_mission_clear_is_supported(self):
        self.assertTrue(command_executor.is_supported("MISSION_CLEAR"))

    def test_mission_clear_targets_the_verified_agent_route(self):
        spec = command_executor.ALLOWED_COMMANDS["MISSION_CLEAR"]
        self.assertEqual(spec.method, "POST")
        self.assertEqual(spec.path, "/agent/clear_mission",
                         "must reach the verified clear service, not legacy /nav/clear_mission")

    def test_body_carries_only_the_command_id(self):
        command = _clear_command("clr-1")
        body = command_executor.ALLOWED_COMMANDS["MISSION_CLEAR"].build_body(command)
        self.assertEqual(body, {"command_id": "clr-1"},
                         "a clear takes no force/override parameter by design")

    def test_not_gated_on_home_verification(self):
        """Clearing a mission is not a Home-relative navigation command."""
        self.assertNotIn("MISSION_CLEAR", command_executor.HOME_VERIFICATION_REQUIRED)

    def test_timeout_exceeds_the_flask_side_readback_bound(self):
        spec = command_executor.ALLOWED_COMMANDS["MISSION_CLEAR"]
        self.assertGreaterEqual(spec.timeout, 30.0,
                                "must comfortably exceed the vehicle-side readback bound")


class TestNormalization(unittest.TestCase):
    def test_mission_clear_is_normalized(self):
        self.assertTrue(command_normalization.is_normalized("MISSION_CLEAR"))

    def test_verified_clear_is_executed(self):
        out = command_normalization.normalize("MISSION_CLEAR", _clear_response(verified=True))
        self.assertTrue(out["executed"])
        self.assertTrue(out["verified"])
        self.assertEqual(out["expected_state"], "MISSION_EMPTY")
        self.assertEqual(out["observed_state"], "MISSION_EMPTY")
        self.assertIsNone(out["error"])

    def test_sent_but_unverified_clear_is_not_executed(self):
        """The core guarantee: MISSION_CLEAR_ALL was sent and acked, but the
        readback still shows a mission. That is a failure."""
        out = command_normalization.normalize(
            "MISSION_CLEAR",
            _clear_response(verified=False, cleared=True, pixhawk_item_count=4,
                            error={"code": "VERIFICATION_FAILED",
                                   "message": "4 Pixhawk item(s) remain on the vehicle"}),
        )
        self.assertFalse(out["executed"], "cleared-but-not-verified must never read as success")
        self.assertIsNone(out["observed_state"])
        self.assertIn("remain", out["error"])

    def test_refused_clear_is_not_executed(self):
        out = command_normalization.normalize(
            "MISSION_CLEAR",
            _clear_response(accepted=False, cleared=False, verified=False,
                            error={"code": "VEHICLE_ARMED", "message": "vehicle is armed"}),
        )
        self.assertFalse(out["executed"])
        self.assertIn("armed", out["error"])

    def test_non_dict_response_is_never_success(self):
        out = command_normalization.normalize("MISSION_CLEAR", None)
        self.assertFalse(out["executed"])
        self.assertFalse(out["verified"])


class TestCommandFlow(unittest.TestCase):
    def setUp(self):
        for path in (config.COMMAND_LOG_FILE, config.COMMAND_RESULTS_FILE):
            if os.path.exists(path):
                os.remove(path)
        self._orig_call = command_executor.call_local_endpoint

    def tearDown(self):
        command_executor.call_local_endpoint = self._orig_call

    def test_verified_clear_reports_executed(self):
        command_executor.call_local_endpoint = lambda c, timeout=None: _clear_response(verified=True)
        payload, _event = process_command(_clear_command(), control_authority="OPERATOR")
        self.assertEqual(payload["status"], "executed")
        self.assertTrue(payload["result"]["verified"])

    def test_unverified_clear_reports_failed(self):
        command_executor.call_local_endpoint = lambda c, timeout=None: _clear_response(
            verified=False, pixhawk_item_count=3)
        payload, _event = process_command(_clear_command(), control_authority="OPERATOR")
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["result"]["verified"])

    def test_rejected_without_operator_authority(self):
        """Operator commands require OPERATOR authority -- MISSION_CLEAR is no
        exception, and must not reach the vehicle under LOCAL_AGENT."""
        calls = []

        def spy(c, timeout=None):
            calls.append(c)
            return _clear_response()

        command_executor.call_local_endpoint = spy
        payload, _event = process_command(_clear_command(), control_authority="LOCAL_AGENT")
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("OPERATOR", payload["reason"])
        self.assertEqual(calls, [], "must never reach the vehicle without OPERATOR authority")

    def test_redelivered_clear_resends_stored_result_without_re_executing(self):
        calls = []

        def spy(c, timeout=None):
            calls.append(c["command_id"])
            return _clear_response(verified=True)

        command_executor.call_local_endpoint = spy
        command = _clear_command("clr-dedup")
        first, _ = process_command(command, control_authority="OPERATOR")
        second, _ = process_command(command, control_authority="OPERATOR")

        self.assertEqual(calls, ["clr-dedup"], "a redelivered clear must never re-execute")
        self.assertEqual(second["status"], first["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
