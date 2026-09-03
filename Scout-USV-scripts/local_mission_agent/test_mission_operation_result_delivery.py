"""
Local mission-operation completion vs. Operator result-DELIVERY -- these are
two different questions and must be answered independently. Run directly:

    python3 test_mission_operation_result_delivery.py

Why this suite exists
----------------------
mission_operation_status.finish() persists the vehicle-proven terminal
outcome (COMPLETED/FAILED) of a MISSION_UPLOAD/MISSION_CLEAR from a fresh
readback. Previously, local_agent._record_mission_operation immediately
overwrote that terminal `state` with STATE_DELIVERING_RESULT before handing
the result to _deliver_command_result -- and nothing in the live process ever
moved it back. Only mission_operation_status.recover_after_restart(), run
once at startup, ever resolved DELIVERING_RESULT back to a terminal state.
The result: a fully verified, EXACT_MATCH upload -- Pixhawk readback proving
14/14 waypoints, hash match, MAV_MISSION_ACCEPTED -- left GET
/agent/mission_operation reporting DELIVERING_RESULT forever in a live
process, for as long as it took (or failed) to notify the operator, even
though the vehicle-side outcome was already fully known and unchangeable.

`state` now becomes terminal in finish() and stays there permanently, no
matter what happens to delivery afterwards. Whether the operator has been
told is tracked separately in the record's `delivery` sub-status (PENDING ->
DELIVERING -> ACKNOWLEDGED, see mission_operation_status.py), driven by
_deliver_command_result/_send_buffered in local_agent.py. This suite proves
the two no longer move together.

These drive the real local_agent._poll_and_execute_commands loop with only
the operator transport and the vehicle Flask call faked, same harness as
test_mission_upload_redelivery.py and test_mission_restart_redelivery.py, so
dedup/redelivery/worker glue is exercised for real, not re-implemented here.
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
config.MISSION_OPERATION_STATE_FILE = tempfile.mktemp(suffix=".json")

import api_client
import buffer
import command_executor
import command_history
import command_results
import local_agent
import mission_operation_status
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
    """A mission-contract-v1 EXACT_MATCH verified upload, as the vehicle Flask
    /agent/upload_mission route returns it -- mirrors the reported incident's
    live evidence (route_waypoint_count 14/14, pixhawk_item_count 15,
    identical route_content_hash, MAV_MISSION_ACCEPTED)."""
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


def _unverified_upload_response():
    """Items sent + acked but the fresh readback did NOT match -- must still
    fail closed. Used to prove this patch does not weaken verification."""
    return {
        "contract_version": "mission-contract-v1",
        "accepted": True, "uploaded": True, "verified": False,
        "expected_route_waypoint_count": 2, "observed_route_waypoint_count": 1,
        "expected_pixhawk_item_count": 3, "observed_pixhawk_item_count": 2,
        "expected_route_content_hash": "sha256:abc",
        "observed_route_content_hash": "sha256:xyz",
        "acknowledgement": "MAV_MISSION_ACCEPTED",
        "error": {"code": "VERIFICATION_FAILED",
                  "message": "route waypoint count mismatch (expected 2, observed 1)"},
    }


class _Base(unittest.TestCase):
    def setUp(self):
        for path in (config.COMMAND_LOG_FILE, config.COMMAND_RESULTS_FILE,
                     config.BUFFER_FILE, config.MISSION_OPERATION_STATE_FILE):
            if os.path.exists(path):
                os.remove(path)
        worker._reset_for_tests()
        mission_operation_status._reset_for_tests(config.MISSION_OPERATION_STATE_FILE)
        command_history.clear() if hasattr(command_history, "clear") else None

        self.posted = []        # command_results actually delivered to the operator
        self.executions = []    # every real vehicle Flask mission call
        self.pending = []

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

    def _fail_delivery(self):
        def failing_send(path, message):
            raise RuntimeError("operator unreachable")
        local_agent.send_to_operator = failing_send
        api_client.send_to_operator = failing_send


# ── 1. successful upload + Operator reachable ────────────────────────────────

class TestOperatorReachable(_Base):
    def setUp(self):
        super().setUp()
        command_executor.call_local_endpoint = lambda c, timeout=None: (
            self.executions.append(c["command_id"]) or _verified_upload_response()
        )

    def test_reaches_completed_and_acknowledged(self):
        self.pending = [_upload_command("A1")]
        self._poll()
        self.assertTrue(self._wait_idle())

        record = mission_operation_status.get()
        self.assertEqual(record["state"], "COMPLETED")
        self.assertEqual(record["delivery"]["status"], "ACKNOWLEDGED")
        self.assertIsNotNone(record["delivery"]["acknowledged_at"])

        results = self._results_for("A1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "executed")

        # Acknowledged -- the authoritative resend copy is no longer retained.
        self.assertIsNone(command_results.get_stored_result("A1"))


# ── 2/9. successful upload + Operator temporarily unreachable ───────────────
# This is the exact regression: verified EXACT_MATCH, delivery cannot
# complete, mission_operation must still reach terminal COMPLETED instead of
# sitting at DELIVERING_RESULT indefinitely.

class TestOperatorUnreachable(_Base):
    def setUp(self):
        super().setUp()
        command_executor.call_local_endpoint = lambda c, timeout=None: (
            self.executions.append(c["command_id"]) or _verified_upload_response()
        )

    def test_local_completion_survives_unreachable_operator(self):
        self._fail_delivery()

        self.pending = [_upload_command("A2")]
        self._poll()
        self.assertTrue(self._wait_idle(), "upload should finish locally regardless of delivery")
        self.assertEqual(self.executions, ["A2"])

        record = mission_operation_status.get()
        self.assertEqual(record["state"], "COMPLETED",
                         "a vehicle-verified upload must reach terminal COMPLETED even "
                         "when the operator cannot be reached")
        self.assertNotEqual(record["state"], "DELIVERING_RESULT",
                            "must never be left non-terminal because delivery failed")
        self.assertEqual(record["observed_route_content_hash"], "sha256:abc")
        self.assertEqual(record["acknowledgement"], "MAV_MISSION_ACCEPTED")

        # Delivery, separately, is pending retry -- not acknowledged, not lost.
        self.assertEqual(record["delivery"]["status"], "PENDING")
        self.assertGreaterEqual(record["delivery"]["attempts"], 1)
        self.assertIsNotNone(record["delivery"]["last_error"])

        # The regression scenario's exact assertion: even indefinitely (we
        # simulate "indefinitely" as "still true well after completion"),
        # state never reverts to non-terminal.
        time.sleep(0.05)
        self.assertEqual(mission_operation_status.get()["state"], "COMPLETED")

    def test_pending_result_remains_separately_visible_and_retriable(self):
        self._fail_delivery()

        self.pending = [_upload_command("A3")]
        self._poll()
        self.assertTrue(self._wait_idle())

        stored = command_results.get_stored_result("A3")
        self.assertIsNotNone(stored, "an undelivered terminal result must be retained for resend")
        self.assertEqual(stored["status"], "executed")

        buffered = buffer.read_buffered_messages()
        buffered_ids = [(m.get("payload") or {}).get("command_id") for m in buffered]
        self.assertIn("A3", buffered_ids, "the undelivered result must be queued for retry")

        record = mission_operation_status.get()
        self.assertEqual(record["state"], "COMPLETED")
        self.assertEqual(record["delivery"]["status"], "PENDING")


# ── 3/8. acknowledgement delayed, then Operator reconnects ──────────────────

class TestDelayedAcknowledgement(_Base):
    def setUp(self):
        super().setUp()
        command_executor.call_local_endpoint = lambda c, timeout=None: (
            self.executions.append(c["command_id"]) or _verified_upload_response()
        )

    def test_result_eventually_redelivers_after_operator_reconnects(self):
        self._fail_delivery()

        self.pending = [_upload_command("A4")]
        self._poll()
        self.assertTrue(self._wait_idle())

        record = mission_operation_status.get()
        self.assertEqual(record["state"], "COMPLETED", "already terminal while delivery is stuck")
        self.assertEqual(record["delivery"]["status"], "PENDING")

        # Operator comes back; the main loop's buffer flush is what redelivers.
        api_client.send_to_operator = None  # unused by flush path directly
        def reachable_send(path, message):
            if path == "/agent/command_result":
                self.posted.append(message["payload"])
            return {"status": "ok"}
        local_agent.send_to_operator = reachable_send

        flush_result = buffer.flush_buffer(local_agent._send_buffered)
        self.assertEqual(flush_result["remaining"], 0)

        results = self._results_for("A4")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "executed")

        record = mission_operation_status.get()
        self.assertEqual(record["state"], "COMPLETED",
                         "state must not have moved at all across the whole delivery saga")
        self.assertEqual(record["delivery"]["status"], "ACKNOWLEDGED")
        self.assertIsNone(command_results.get_stored_result("A4"))
        self.assertEqual(buffer.read_buffered_messages(), [])


# ── 4/5. duplicate redelivery -- dedup, no second Pixhawk write ─────────────

class TestDuplicateRedelivery(_Base):
    def setUp(self):
        super().setUp()
        command_executor.call_local_endpoint = lambda c, timeout=None: (
            self.executions.append(c["command_id"]) or _verified_upload_response()
        )

    def test_duplicate_redelivery_after_completion_causes_no_second_upload(self):
        self.pending = [_upload_command("A5")]
        self._poll()
        self.assertTrue(self._wait_idle())
        self.assertEqual(self.executions, ["A5"])

        for _ in range(3):
            self._poll()
            self.assertTrue(self._wait_idle())

        self.assertEqual(self.executions, ["A5"],
                         "a completed command_id must never reach the Pixhawk a second time")
        self.assertTrue(is_duplicate("A5"))

        record = mission_operation_status.get()
        self.assertEqual(record["state"], "COMPLETED")

    def test_duplicate_redelivery_while_delivery_still_pending_causes_no_second_upload(self):
        self._fail_delivery()
        self.pending = [_upload_command("A6")]
        self._poll()
        self.assertTrue(self._wait_idle())
        self.assertEqual(self.executions, ["A6"])

        # Operator keeps redelivering because it never got a terminal result.
        for _ in range(3):
            self._poll()
            self.assertTrue(self._wait_idle())

        self.assertEqual(self.executions, ["A6"],
                         "redelivery while the result is undelivered must still not repeat "
                         "the Pixhawk write -- the stored result is resent, not re-executed")
        record = mission_operation_status.get()
        self.assertEqual(record["state"], "COMPLETED")


# ── 6. terminal local result persists BEFORE remote acknowledgement ─────────

class TestTerminalWritePrecedesDelivery(_Base):
    def setUp(self):
        super().setUp()
        command_executor.call_local_endpoint = lambda c, timeout=None: (
            self.executions.append(c["command_id"]) or _verified_upload_response()
        )
        self.send_started = threading.Event()
        self.send_release = threading.Event()

        def blocking_send(path, message):
            if path == "/agent/command_result":
                self.send_started.set()
                self.send_release.wait(5.0)
                self.posted.append(message["payload"])
            return {"status": "ok"}

        local_agent.send_to_operator = blocking_send

    def tearDown(self):
        self.send_release.set()
        self._wait_idle()
        super().tearDown()

    def test_state_is_already_completed_while_delivery_is_still_in_flight(self):
        self.pending = [_upload_command("A7")]
        self._poll()
        self.assertTrue(self.send_started.wait(2.0), "delivery attempt never started")

        # The vehicle-proven outcome is durable at this instant, before the
        # operator has been reached at all.
        record = mission_operation_status.get()
        self.assertEqual(record["state"], "COMPLETED")
        self.assertEqual(record["delivery"]["status"], "DELIVERING")

        self.send_release.set()
        self.assertTrue(self._wait_idle())


# ── 7. restart while result delivery is pending ──────────────────────────────

class TestRestartWhileDeliveryPending(_Base):
    def test_in_flight_delivery_attempt_resets_to_pending_not_lost(self):
        # Constructed directly (not via the worker thread) to model exactly
        # what is on disk when a process dies mid-delivery-attempt -- same
        # style as test_mission_restart_redelivery.py's
        # TestDeliveryInterruptionIsNotUnknown.
        mission_operation_status.begin("R1", "MISSION_UPLOAD",
                                       expected_route_waypoint_count=2,
                                       expected_pixhawk_item_count=3)
        mission_operation_status.finish("R1", succeeded=True,
                                        observed_route_waypoint_count=2,
                                        observed_pixhawk_item_count=3,
                                        observed_route_content_hash="sha256:abc",
                                        acknowledgement="MAV_MISSION_ACCEPTED")
        mission_operation_status.mark_delivery_attempt("R1")

        pre = mission_operation_status.get()
        self.assertEqual(pre["delivery"]["status"], "DELIVERING")

        record = mission_operation_status.recover_after_restart()

        self.assertEqual(record["state"], "COMPLETED",
                         "restart must not revert the already-proven local outcome")
        self.assertEqual(record["delivery"]["status"], "PENDING",
                         "an interrupted delivery attempt must become retriable, not stuck "
                         "showing DELIVERING forever with nothing left running to move it")
        self.assertEqual(record["delivery"]["attempts"], 1,
                         "the attempt count is preserved, not reset")

    def test_failed_local_outcome_also_survives_restart_with_pending_delivery(self):
        mission_operation_status.begin("R2", "MISSION_CLEAR")
        mission_operation_status.finish("R2", succeeded=False,
                                        error={"code": "VERIFICATION_FAILED",
                                               "message": "route remains"})
        mission_operation_status.mark_delivery_attempt("R2")

        record = mission_operation_status.recover_after_restart()

        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["error"]["code"], "VERIFICATION_FAILED")
        self.assertEqual(record["delivery"]["status"], "PENDING")


# ── fail-closed: verification failure must still FAIL, never COMPLETED ──────

class TestVerificationFailureStillFailsClosed(_Base):
    """This patch changes when/whether `state` reflects delivery -- it must
    not touch verification itself. An unverified upload must still be
    terminal FAILED, and its delivery lifecycle behaves identically to a
    successful one (independent of `state`)."""

    def setUp(self):
        super().setUp()
        command_executor.call_local_endpoint = lambda c, timeout=None: (
            self.executions.append(c["command_id"]) or _unverified_upload_response()
        )

    def test_unverified_upload_is_terminal_failed_not_completed(self):
        self.pending = [_upload_command("A8")]
        self._poll()
        self.assertTrue(self._wait_idle())

        record = mission_operation_status.get()
        self.assertEqual(record["state"], "FAILED")
        # command_normalization.py reduces the structured {"code","message"}
        # error to its human-readable message text -- see _error_text().
        self.assertIn("route waypoint count mismatch", record["error"])
        self.assertEqual(record["delivery"]["status"], "ACKNOWLEDGED")

        results = self._results_for("A8")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
