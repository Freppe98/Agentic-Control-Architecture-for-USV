"""
At-least-once mission delivery ACROSS A PROCESS RESTART. Run directly:

    python3 test_mission_restart_redelivery.py

What this covers that test_mission_upload_redelivery.py does not
----------------------------------------------------------------
That suite proves redelivery is handled correctly within ONE process
lifetime: the same command_id offered again while the upload is still running
is recognised as IN_FLIGHT and not re-executed. It never stops the process.

But the Operator backend's at-least-once delivery does not care about process
boundaries. It keeps redelivering a SENT command until a terminal result
arrives, so the redelivery that matters most is the one that arrives AFTER the
Local Agent has restarted -- exactly when the in-memory worker state that made
IN_FLIGHT possible is gone. Two cases exist and they must be answered very
differently:

  1. The operation FINISHED and its terminal result was persisted before
     delivery. The vehicle work is done. Redelivery must resend that stored
     result verbatim and perform NO new MAVLink operation. Re-uploading here
     would rewrite a mission that is already verified on the vehicle.

  2. The operation was INTERRUPTED mid-transaction. The vehicle may hold
     nothing, a complete mission, or a partial one; this process cannot tell
     which, because the sequence the vehicle had reached was only ever in the
     memory of the process that died. Redelivery must fail with a structured
     UNKNOWN_AFTER_RESTART and demand a fresh command_id. It must NOT resume:
     continuing a MISSION_ITEM_INT exchange from an unproven sequence writes
     waypoints blind into a mission of unknown content.

A "restart" here is simulated the way the real thing behaves: the on-disk
state (command_log.jsonl, command_results.json, mission_operation_state.json)
is kept exactly as it was, while every in-memory structure -- the bounded
upload worker's active slot above all -- is discarded.
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
import command_executor
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
        for path in (config.COMMAND_LOG_FILE, config.COMMAND_RESULTS_FILE,
                     config.BUFFER_FILE, config.MISSION_OPERATION_STATE_FILE):
            if os.path.exists(path):
                os.remove(path)
        worker._reset_for_tests()
        mission_operation_status._reset_for_tests(config.MISSION_OPERATION_STATE_FILE)

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

        self._reachable_send = fake_send
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

    def _restart(self):
        """Simulate a Local Agent process restart.

        On-disk state is untouched -- that is the whole point of persisting it.
        In-memory state is discarded: the worker's active slot is cleared,
        exactly as it would be by a new process, so nothing can answer
        IN_FLIGHT from memory. Then the real startup hook runs.
        """
        worker._reset_for_tests()
        return mission_operation_status.recover_after_restart()

    def _results_for(self, command_id):
        return [p for p in self.posted if p.get("command_id") == command_id]


class TestTerminalResultSurvivesRestart(_Base):
    """Case 1: the operation finished and its result was persisted. Redelivery
    after a restart resends that result and touches no MAVLink."""

    def setUp(self):
        super().setUp()

        def fast_upload(command, timeout=None):
            self.executions.append(command["command_id"])
            return _verified_upload_response()

        command_executor.call_local_endpoint = fast_upload

    def test_stored_result_is_returned_and_no_mavlink_repeated(self):
        # The link drops exactly at delivery time: the upload completes on the
        # vehicle and its terminal result is persisted, but the operator never
        # receives it. That is precisely the situation at-least-once redelivery
        # exists for -- and the reason the stored result must be retained.
        def failing_send(path, message):
            raise RuntimeError("operator unreachable")

        local_agent.send_to_operator = failing_send

        self.pending = [_upload_command("R1")]
        self._poll()
        self.assertTrue(self._wait_idle(), "upload should finish")
        self.assertEqual(self.executions, ["R1"], "exactly one real upload")

        # Terminal result persisted even though delivery failed.
        stored = command_results.get_stored_result("R1")
        self.assertIsNotNone(stored, "the terminal result must be persisted before delivery")
        self.assertEqual(stored["status"], "executed")

        self._restart()

        # The link is back for the redelivery.
        local_agent.send_to_operator = self._reachable_send

        # Same command_id redelivered after the restart.
        self.posted.clear()
        self.pending = [_upload_command("R1")]
        self._poll()
        self.assertTrue(self._wait_idle())

        self.assertEqual(self.executions, ["R1"],
                         "the vehicle upload must NOT be repeated after a restart")
        resent = self._results_for("R1")
        self.assertEqual(len(resent), 1, "exactly one terminal result resent")
        self.assertEqual(resent[0]["status"], "executed")
        self.assertEqual(resent[0], stored,
                         "the stored terminal result must be resent verbatim")

    def test_command_id_stays_marked_processed_across_restart(self):
        self.pending = [_upload_command("R2")]
        self._poll()
        self.assertTrue(self._wait_idle())

        self._restart()

        self.assertTrue(is_duplicate("R2"),
                        "the processed-id log is persisted, so dedup survives a restart")

    def test_completed_operation_is_not_failed_by_recovery(self):
        """A record whose outcome was already determined must not be rewritten
        as UNKNOWN_AFTER_RESTART -- that would report a verified upload as
        indeterminate and demand a pointless re-upload."""
        self.pending = [_upload_command("R3")]
        self._poll()
        self.assertTrue(self._wait_idle())

        record = self._restart()

        self.assertIn(record["state"], ("COMPLETED", "FAILED"))
        self.assertNotEqual((record.get("error") or {}).get("code"),
                            "UNKNOWN_AFTER_RESTART")


class TestInterruptedUploadFailsSafely(_Base):
    """Case 2: the process died mid-transaction with no terminal result. The
    vehicle-side outcome is unknowable, so it must fail closed."""

    def test_interrupted_upload_is_recorded_non_terminally(self):
        """Drives the REAL upload path and checks the on-disk record while the
        upload is genuinely mid-flight. This is what makes the constructed
        states in the tests below faithful rather than invented."""
        started = threading.Event()
        hold = threading.Event()

        def hanging_upload(command, timeout=None):
            self.executions.append(command["command_id"])
            started.set()
            hold.wait(5.0)
            return _verified_upload_response()

        command_executor.call_local_endpoint = hanging_upload
        self.pending = [_upload_command("X0")]
        self._poll()
        self.assertTrue(started.wait(5.0), "upload should have started")

        state = mission_operation_status.get()["state"]
        hold.set()
        self._wait_idle()

        self.assertIn(state, mission_operation_status.INTERRUPTIBLE_STATES,
                      "an in-flight upload must leave a non-terminal record on disk")

    def _interrupted_at(self, command_id, state=None):
        """The exact on-disk state a process killed mid-transaction leaves: a
        record opened and advanced to EXECUTING, and NO terminal result.

        Constructed directly rather than by abandoning a live worker thread --
        a real kill stops the thread instantly, whereas an abandoned test
        thread keeps running and races to write the terminal result this case
        is defined by the absence of. test_interrupted_upload_is_recorded_non_
        terminally above pins that the real path does reach this same state.
        """
        state = state or mission_operation_status.STATE_EXECUTING
        mission_operation_status.begin(command_id, "MISSION_UPLOAD",
                                       expected_route_waypoint_count=2,
                                       expected_pixhawk_item_count=3)
        mission_operation_status.set_state(state, command_id)
        self.assertIsNone(command_results.get_stored_result(command_id),
                          "this case is defined by having NO terminal result")

    def test_restart_fails_interrupted_upload_with_unknown_after_restart(self):
        self._interrupted_at("X1")

        record = self._restart()

        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["error"]["code"], "UNKNOWN_AFTER_RESTART")
        self.assertTrue(record["error"]["requires_fresh_retry"])
        self.assertEqual(record["error"]["interrupted_state"], "EXECUTING")

    def test_interrupted_upload_is_not_silently_resumed(self):
        """The decisive property: recovery must not continue the transfer."""
        self._interrupted_at("X2")

        before = list(self.executions)
        self._restart()

        self.assertEqual(self.executions, before,
                         "recovery must issue NO MAVLink operation -- a partial "
                         "upload cannot be resumed from an unproven sequence")

    def test_accepted_but_never_executed_also_fails_closed(self):
        """Killed after admission but before the transaction started. Nothing
        may have reached the vehicle -- but 'may' is not 'did not', so this
        fails closed too rather than assuming the vehicle is untouched."""
        self._interrupted_at("X4", mission_operation_status.STATE_ACCEPTED)

        record = self._restart()

        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["error"]["code"], "UNKNOWN_AFTER_RESTART")
        self.assertEqual(record["error"]["interrupted_state"], "ACCEPTED")

    def test_interrupted_command_id_is_reported(self):
        self._interrupted_at("X3")
        self._restart()

        self.assertEqual(mission_operation_status.interrupted_command_id(), "X3",
                         "the interrupted id must be identifiable so its redelivery "
                         "gets the real reason, not a bare 'duplicate'")


class TestDeliveryInterruptionIsNotUnknown(_Base):
    """DELIVERING_RESULT is not an interruptible state: the outcome was already
    determined and persisted, so only the delivery was lost."""

    def test_crash_during_delivery_restores_the_terminal_outcome(self):
        mission_operation_status.begin("D1", "MISSION_UPLOAD",
                                       expected_route_waypoint_count=2,
                                       expected_pixhawk_item_count=3)
        mission_operation_status.finish("D1", succeeded=True,
                                        observed_route_waypoint_count=2,
                                        observed_pixhawk_item_count=3,
                                        acknowledgement="MAV_MISSION_ACCEPTED")
        mission_operation_status.set_state(
            mission_operation_status.STATE_DELIVERING_RESULT, "D1")

        record = self._restart()

        self.assertEqual(record["state"], "COMPLETED",
                         "a completed upload interrupted during DELIVERY is still completed")
        self.assertNotEqual((record.get("error") or {}).get("code"),
                            "UNKNOWN_AFTER_RESTART")

    def test_failed_outcome_interrupted_during_delivery_stays_failed(self):
        mission_operation_status.begin("D2", "MISSION_CLEAR")
        mission_operation_status.finish("D2", succeeded=False,
                                        error={"code": "VERIFICATION_FAILED",
                                               "message": "route remains"})
        mission_operation_status.set_state(
            mission_operation_status.STATE_DELIVERING_RESULT, "D2")

        record = self._restart()

        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["error"]["code"], "VERIFICATION_FAILED",
                         "the real failure reason must survive, not be replaced")


class TestOperationRecordContract(_Base):
    def test_idle_record_has_every_field(self):
        record = mission_operation_status.get()
        for field in ("command_id", "command_type", "state", "started_at", "updated_at",
                      "elapsed_s", "expected_route_waypoint_count",
                      "expected_pixhawk_item_count", "expected_route_content_hash",
                      "observed_route_waypoint_count", "observed_pixhawk_item_count",
                      "observed_route_content_hash", "acknowledgement",
                      "empty_representation", "error"):
            self.assertIn(field, record)
        self.assertEqual(record["state"], "IDLE")

    def test_record_survives_a_restart_for_later_fetch(self):
        """The point of persisting: an operator whose link dropped during the
        upload can still fetch the terminal details afterwards."""
        mission_operation_status.begin("P1", "MISSION_UPLOAD",
                                       expected_route_waypoint_count=2,
                                       expected_pixhawk_item_count=3,
                                       expected_route_content_hash="sha256:abc")
        mission_operation_status.finish("P1", succeeded=True,
                                        observed_route_waypoint_count=2,
                                        observed_pixhawk_item_count=3,
                                        observed_route_content_hash="sha256:abc",
                                        acknowledgement="MAV_MISSION_ACCEPTED")

        self._restart()
        record = mission_operation_status.get()

        self.assertEqual(record["command_id"], "P1")
        self.assertEqual(record["state"], "COMPLETED")
        self.assertEqual(record["observed_route_content_hash"], "sha256:abc")
        self.assertEqual(record["acknowledgement"], "MAV_MISSION_ACCEPTED")

    def test_late_write_from_superseded_operation_is_ignored(self):
        mission_operation_status.begin("OLD", "MISSION_UPLOAD")
        mission_operation_status.begin("NEW", "MISSION_UPLOAD")

        mission_operation_status.finish("OLD", succeeded=False,
                                        error={"code": "UPLOAD_FAILED", "message": "late"})

        record = mission_operation_status.get()
        self.assertEqual(record["command_id"], "NEW")
        self.assertEqual(record["state"], "ACCEPTED",
                         "a superseded operation's late result must not overwrite "
                         "the current operation's record")


if __name__ == "__main__":
    unittest.main(verbosity=2)
