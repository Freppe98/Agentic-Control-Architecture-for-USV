"""
Integration tests for MISSION_UPLOAD redelivery semantics -- the interaction
between the Operator backend's AT-LEAST-ONCE delivery and Scout's bounded
background upload worker. Run directly:

    python3 test_mission_upload_redelivery.py

Why this suite exists
---------------------
The Operator backend keeps redelivering a command in SENT state until a
TERMINAL result arrives. A mission upload legitimately takes tens of seconds
(MISSION_CLEAR_ALL + the full MISSION_REQUEST_INT handshake + a complete fresh
readback), so the operator WILL offer the same command_id again, repeatedly,
while that upload is still perfectly healthy and in progress.

The worker previously answered every "an upload is already running" with BUSY,
without asking WHICH upload. So a redelivery of the in-flight command produced
a terminal BUSY rejection for the very command that was at that moment
succeeding -- and that rejection then raced the real result. The operator
would see a rejected upload whose mission was actually on the vehicle, or two
terminal results for one command_id.

The distinction these tests pin down:

  same command_id, still running   -> IN_FLIGHT: do nothing at all. No second
                                      execution, no BUSY rejection, no second
                                      terminal result. The original run is
                                      still going and delivers the one
                                      terminal result when it finishes.
  different command_id, running    -> BUSY: a real terminal rejection. Only
                                      one upload at a time -- concurrent
                                      mission writes to one Pixhawk are
                                      exactly what must not happen.
  same command_id, already done    -> the stored terminal result is resent,
                                      never re-executed.

Note on the deliberate absence of an intermediate result: live upload progress
is exposed through agent.mission_upload in the periodic status payload, NOT by
posting an intermediate ACCEPTED command_result. Posting one would satisfy the
backend's "a terminal result arrived" condition and switch off the redelivery
that IS the backend's retry mechanism. TestLiveProgressChannel asserts the
progress channel exists so nobody is tempted to add that intermediate result.

These drive the real local_agent._poll_and_execute_commands loop with only the
operator transport and the vehicle Flask call faked, so they exercise the
actual dispatch/dedup/worker glue rather than a re-implementation of it.
"""
import os
import tempfile
import threading
import time
import unittest

import config
config.COMMAND_LOG_FILE = tempfile.mktemp(suffix=".jsonl")
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")
config.BUFFER_FILE = tempfile.mktemp(suffix=".jsonl")

import api_client
import command_executor
import command_history
import command_results
import local_agent
import mission_upload_worker as worker
from command_log import is_duplicate


_ROUTE = [
    {"latitude": 56.6501, "longitude": 12.8701, "loiter_time_s": 0},
    {"latitude": 56.6512, "longitude": 12.8725, "loiter_time_s": 30},
]


def _upload_command(command_id):
    return {
        "command_id": command_id,
        "usv_id": "usv-2",
        "command_type": "MISSION_UPLOAD",
        "issued_at": time.time(),
        "expires_at": time.time() + 600,
        "params": {"waypoints": _ROUTE},
        "requested_by": "operator",
    }


def _verified_upload_response():
    """A mission-contract-v1 verified upload result, as the vehicle Flask
    /agent/upload_mission route returns it."""
    return {
        "contract_version": "mission-contract-v1",
        "accepted": True, "uploaded": True, "verified": True,
        "expected_route_waypoint_count": 2, "observed_route_waypoint_count": 2,
        "expected_pixhawk_item_count": 3, "observed_pixhawk_item_count": 3,
        "expected_route_content_hash": "sha256:abc",
        "observed_route_content_hash": "sha256:abc",
        "acknowledgement": "MAV_MISSION_ACCEPTED",
        "error": None,
    }


class _Base(unittest.TestCase):
    def setUp(self):
        for path in (config.COMMAND_LOG_FILE, config.COMMAND_RESULTS_FILE, config.BUFFER_FILE):
            if os.path.exists(path):
                os.remove(path)
        worker._reset_for_tests()
        command_history.clear() if hasattr(command_history, "clear") else None

        self.posted = []            # every command_result actually sent to the operator
        self.executions = []        # every real vehicle Flask upload call
        self.pending = []           # what the operator offers this poll

        self._orig_get_pending = api_client.get_pending_commands
        self._orig_send = api_client.send_to_operator
        self._orig_call = command_executor.call_local_endpoint

        api_client.get_pending_commands = lambda usv_id: list(self.pending)
        local_agent.get_pending_commands = lambda usv_id: list(self.pending)

        def fake_send(path, message):
            if path == "/agent/command_result":
                self.posted.append(message["payload"])
            return {"status": "ok"}

        api_client.send_to_operator = fake_send
        local_agent.send_to_operator = fake_send

    def tearDown(self):
        api_client.get_pending_commands = self._orig_get_pending
        api_client.send_to_operator = self._orig_send
        command_executor.call_local_endpoint = self._orig_call
        local_agent.get_pending_commands = self._orig_get_pending
        local_agent.send_to_operator = self._orig_send
        worker._reset_for_tests()

    def _poll(self):
        """One iteration of the Local Agent's poll/execute loop."""
        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

    def _wait_idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not worker.is_busy():
                return True
            time.sleep(0.005)
        return False

    def _results_for(self, command_id):
        return [p for p in self.posted if p.get("command_id") == command_id]


class TestSameIdRedeliveryWhileInFlight(_Base):
    """upload A starts once; redelivering A while it is still running must be
    recognised as the same in-flight command."""

    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.started = threading.Event()

        def slow_upload(command, timeout=None):
            self.executions.append(command["command_id"])
            self.started.set()
            self.release.wait(5.0)      # hold the upload "in flight"
            return _verified_upload_response()

        command_executor.call_local_endpoint = slow_upload

    def tearDown(self):
        self.release.set()
        self._wait_idle()
        super().tearDown()

    def test_upload_a_starts_exactly_once(self):
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self.started.wait(2.0), "upload A never started")
        self.assertEqual(self.executions, ["A"])

        status = worker.status()
        self.assertTrue(status["active"])
        self.assertEqual(status["command_id"], "A")

    def test_redelivery_of_a_while_active_does_not_execute_again(self):
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self.started.wait(2.0))

        # The operator redelivers A three more times while it is still running.
        for _ in range(3):
            self._poll()

        self.assertEqual(self.executions, ["A"],
                         "a redelivered in-flight upload must never execute a second time")

    def test_redelivery_of_a_while_active_produces_no_terminal_result(self):
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self.started.wait(2.0))

        for _ in range(3):
            self._poll()

        self.assertEqual(self._results_for("A"), [],
                         "an in-flight upload must not get a terminal result while still running")

    def test_redelivery_of_a_while_active_is_not_a_busy_rejection(self):
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self.started.wait(2.0))
        self._poll()

        rejections = [p for p in self._results_for("A") if p.get("status") == "rejected"]
        self.assertEqual(rejections, [],
                         "redelivering the in-flight command must never produce a BUSY rejection")

    def test_worker_reports_in_flight_for_the_same_id(self):
        """The distinction at the worker boundary itself."""
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self.started.wait(2.0))

        outcome = worker.try_start(_upload_command("A"), run_fn=lambda c: None,
                                   finalize_fn=lambda p: None)
        self.assertEqual(outcome, "IN_FLIGHT")

    def test_different_id_b_while_a_active_gets_a_busy_rejection(self):
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self.started.wait(2.0))

        self.pending = [_upload_command("B")]
        self._poll()

        b_results = self._results_for("B")
        self.assertEqual(len(b_results), 1, "B must get exactly one terminal result")
        self.assertEqual(b_results[0]["status"], "rejected")
        self.assertIn("already in progress", b_results[0]["reason"])
        self.assertEqual(self.executions, ["A"],
                         "a concurrent different upload must never reach the vehicle")

    def test_busy_rejection_for_b_does_not_disturb_a(self):
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self.started.wait(2.0))

        self.pending = [_upload_command("B")]
        self._poll()

        self.release.set()
        self.assertTrue(self._wait_idle(), "A never completed")

        a_results = self._results_for("A")
        self.assertEqual(len(a_results), 1)
        self.assertEqual(a_results[0]["status"], "executed",
                         "A must still succeed despite B being rejected mid-flight")


class TestTerminalResultDeliveredExactlyOnce(_Base):
    def setUp(self):
        super().setUp()

        def fast_upload(command, timeout=None):
            self.executions.append(command["command_id"])
            return _verified_upload_response()

        command_executor.call_local_endpoint = fast_upload

    def test_completed_upload_delivers_one_terminal_result(self):
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self._wait_idle())

        results = self._results_for("A")
        self.assertEqual(len(results), 1, "exactly one terminal result for one upload")
        self.assertEqual(results[0]["status"], "executed")
        self.assertTrue(results[0]["result"]["verified"])
        self.assertEqual(self.executions, ["A"])

    def test_redelivery_after_completion_never_executes_again(self):
        """The safety-critical half: whatever the operator does afterwards, a
        redelivered command_id must never drive the vehicle a second time."""
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self._wait_idle())

        for _ in range(3):
            self._poll()
            self.assertTrue(self._wait_idle())

        self.assertEqual(self.executions, ["A"],
                         "a completed command_id must never be executed again on redelivery")
        self.assertTrue(is_duplicate("A"))

    def test_redelivery_before_ack_resends_the_stored_terminal_result(self):
        """While the operator has NOT yet acknowledged the result (which is
        exactly when at-least-once delivery keeps retrying), redelivery
        resends the stored terminal result verbatim rather than re-judging or
        re-executing it."""
        # Delivery fails, so the authoritative stored result is retained.
        def failing_send(path, message):
            raise RuntimeError("operator unreachable")

        local_agent.send_to_operator = failing_send
        api_client.send_to_operator = failing_send

        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self._wait_idle())

        stored = command_results.get_stored_result("A")
        self.assertIsNotNone(stored, "an undelivered terminal result must be retained for resend")
        self.assertEqual(stored["status"], "executed")

        # Operator comes back; redelivery now resends that exact result.
        local_agent.send_to_operator = self._collecting_send()
        api_client.send_to_operator = local_agent.send_to_operator

        self._poll()
        self.assertTrue(self._wait_idle())

        results = self._results_for("A")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], stored["status"])
        self.assertEqual(results[0]["command_id"], "A")
        self.assertEqual(self.executions, ["A"], "resending a stored result must not re-execute")

    def _collecting_send(self):
        def send(path, message):
            if path == "/agent/command_result":
                self.posted.append(message["payload"])
            return {"status": "ok"}
        return send


class TestLiveProgressChannel(_Base):
    """Live upload state must remain observable WITHOUT posting an
    intermediate command_result -- see the module docstring."""

    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.started = threading.Event()

        def slow_upload(command, timeout=None):
            self.started.set()
            self.release.wait(5.0)
            return _verified_upload_response()

        command_executor.call_local_endpoint = slow_upload

    def tearDown(self):
        self.release.set()
        self._wait_idle()
        super().tearDown()

    def test_mission_upload_status_exposes_active_state_and_elapsed(self):
        self.pending = [_upload_command("A")]
        self._poll()
        self.assertTrue(self.started.wait(2.0))

        status = worker.status()
        self.assertTrue(status["active"])
        self.assertEqual(status["state"], "executing")
        self.assertEqual(status["command_id"], "A")
        self.assertIsNotNone(status["elapsed_s"])
        self.assertGreaterEqual(status["elapsed_s"], 0.0)

        # ...and no intermediate result was posted to get that visibility.
        self.assertEqual(self._results_for("A"), [],
                         "live progress must not be delivered as an intermediate command_result")


if __name__ == "__main__":
    unittest.main(verbosity=2)
