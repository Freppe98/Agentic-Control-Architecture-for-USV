"""
Integration tests for local_agent.py's _poll_and_execute_commands -- the
actual glue between "operator backend queued a command" and "Local Agent
posts a command_result back", which command_handler.py/command_executor.py's
own tests (test_command_handler.py) do not exercise: those monkeypatch
command_executor.call_local_endpoint directly and never touch
api_client.get_pending_commands / api_client.send_to_operator / buffer.py,
so a bug in the polling/posting loop itself (as opposed to the validation/
execution logic) would be invisible to them.

This is the exact SET_HOME symptom this suite was written to catch: a real
operator command left permanently SENT (claimed_at set, completed_at/result
never set) with an empty Scout diagnostics command history, despite the
vehicle Flask service being fully healthy. Root cause: nothing outside
command_handler.process_command()'s own internal try/except guarded the
_poll_and_execute_commands loop, so *any* exception outside that guarded
block (a bug in validation/dedup/history code, not the vehicle Flask call
itself) would propagate out of local_agent.py's main() while-loop entirely,
killing the whole process before a command_result was ever posted or a
command_history record was ever made -- and a deliver-once operator queue
never re-offers the same command_id, so that command stays SENT forever.

Run directly: python3 test_local_agent_command_flow.py
"""
import os
import tempfile
import time
import unittest
import uuid

import config
config.COMMAND_LOG_FILE = tempfile.mktemp(suffix=".jsonl")
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")
config.BUFFER_FILE = tempfile.mktemp(suffix=".jsonl")

import api_client
import buffer
import command_executor
import command_history
import command_results
import local_agent


def _make_command(command_type="SET_HOME", command_id=None, params=None, expires_in=60):
    return {
        "command_id": command_id or str(uuid.uuid4()),
        "usv_id": "usv-2",
        "command_type": command_type,
        "issued_at": time.time(),
        "expires_at": time.time() + expires_in if expires_in is not None else None,
        "params": params if params is not None else {},
        "requested_by": "operator",
    }


class SetHomeCommandFlowTests(unittest.TestCase):
    """
    Drives local_agent._poll_and_execute_commands end to end with
    api_client.get_pending_commands/send_to_operator monkeypatched (no real
    operator backend or Flask needed) but command_executor.call_local_endpoint
    left real, only requests.request underneath it mocked -- so these tests
    prove the exact request body SET_HOME sends and the exact command_result
    posted back, not just command_handler's internal return value.
    """

    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        if os.path.exists(config.BUFFER_FILE):
            os.remove(config.BUFFER_FILE)
        command_history._history.clear()
        self._orig_get_pending = api_client.get_pending_commands
        self._orig_send = api_client.send_to_operator
        self._orig_request = command_executor.requests.request
        self._orig_home_verified = command_executor.home_verified
        command_executor.home_verified = lambda: True
        self.posted = []

        def fake_send(endpoint, message):
            self.posted.append((endpoint, message))
            return {"ok": True, "operator": "http://fake", "response": {"ok": True}}

        api_client.send_to_operator = fake_send
        local_agent.send_to_operator = fake_send

    def tearDown(self):
        api_client.get_pending_commands = self._orig_get_pending
        api_client.send_to_operator = self._orig_send
        local_agent.get_pending_commands = self._orig_get_pending
        local_agent.send_to_operator = self._orig_send
        command_executor.requests.request = self._orig_request
        command_executor.home_verified = self._orig_home_verified

    def _serve_commands(self, commands):
        def fake_get_pending(usv_id):
            due, commands[:] = list(commands), []
            return due
        api_client.get_pending_commands = fake_get_pending
        local_agent.get_pending_commands = fake_get_pending

    def _fake_flask_response(self, body, status_ok=True):
        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured.update(kwargs)

            class _Resp:
                def raise_for_status(self):
                    if not status_ok:
                        import requests
                        raise requests.HTTPError("500 Server Error")

                def json(self):
                    return body

            return _Resp()

        captured = {}
        command_executor.requests.request = fake_request
        return captured

    def test_correct_command_id_and_mode_reach_flask(self):
        """Requirement 1/2: the exact body {"command_id", "mode": "current_position"}
        reaches Flask, with the operator's own command_id preserved end to end --
        even when the operator also supplied a legacy/browser lat/lng, which must
        never be forwarded (requirement 3/4)."""
        captured = self._fake_flask_response({
            "accepted": True, "verified": True, "command_id": "op-cmd-001",
            "requested_position": {"latitude": 56.66, "longitude": 12.88},
            "home_position": {"latitude": 56.66, "longitude": 12.88, "altitude": 0.7},
            "verification_distance_m": 0.0, "ack_result": "MAV_RESULT_ACCEPTED", "error": None,
        })
        cmd = _make_command(
            command_type="SET_HOME", command_id="op-cmd-001",
            params={"lat": 56.0, "lng": 12.0},  # legacy/browser-supplied, must be ignored
        )
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(captured["json"], {"command_id": "op-cmd-001", "mode": "current_position"})
        self.assertNotIn("lat", captured["json"])
        self.assertNotIn("lng", captured["json"])

    def test_successful_result_posted_back_to_operator(self):
        """Requirement 9: the raw Flask result lands unchanged in the posted
        command_result's payload.result."""
        flask_body = {
            "accepted": True, "verified": True, "command_id": "op-cmd-002",
            "requested_position": {"latitude": 56.66, "longitude": 12.88},
            "home_position": {"latitude": 56.66, "longitude": 12.88, "altitude": 0.7},
            "verification_distance_m": 0.0, "ack_result": "MAV_RESULT_ACCEPTED", "error": None,
        }
        self._fake_flask_response(flask_body)
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-002")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(len(self.posted), 1)
        endpoint, message = self.posted[0]
        self.assertEqual(endpoint, "/agent/command_result")
        self.assertEqual(message["message_type"], "command_result")
        payload = message["payload"]
        self.assertEqual(payload["command_id"], "op-cmd-002")
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(payload["result"], flask_body)

    def test_accepted_false_posted_back_faithfully(self):
        """Requirement 7: a well-formed but rejected Flask attempt
        (accepted=false/verified=false) is still posted back to the
        operator with that failure body intact -- not swallowed, not
        silently upgraded to success."""
        flask_body = {
            "accepted": False, "verified": False, "command_id": "op-cmd-003",
            "requested_position": None, "home_position": None,
            "verification_distance_m": None, "ack_result": None,
            "error": {"code": "POSITION_STALE", "message": "stale"},
        }
        self._fake_flask_response(flask_body)
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-003")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(len(self.posted), 1)
        payload = self.posted[0][1]["payload"]
        self.assertEqual(payload["command_id"], "op-cmd-003")
        self.assertFalse(payload["result"]["accepted"])
        self.assertFalse(payload["result"]["verified"])
        self.assertEqual(payload["result"]["error"]["code"], "POSITION_STALE")

    def test_non_2xx_response_becomes_terminal_failure(self):
        self._fake_flask_response({"error": "boom"}, status_ok=False)
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-004")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        payload = self.posted[0][1]["payload"]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(len(command_history.get_recent()), 2)
        self.assertEqual(command_history.get_recent()[-1]["status"], "failed")

    def test_invalid_json_response_becomes_terminal_failure(self):
        def fake_request(method, url, **kwargs):
            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    raise ValueError("not json")

            return _Resp()

        command_executor.requests.request = fake_request
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-005")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        payload = self.posted[0][1]["payload"]
        self.assertEqual(payload["status"], "failed")
        self.assertIn("not json", payload["reason"])

    def test_connection_error_becomes_terminal_failure(self):
        import requests

        def fake_request(method, url, **kwargs):
            raise requests.exceptions.ConnectionError("connection refused")

        command_executor.requests.request = fake_request
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-006")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        payload = self.posted[0][1]["payload"]
        self.assertEqual(payload["status"], "failed")
        self.assertIn("connection refused", payload["reason"])

    def test_timeout_becomes_terminal_failure(self):
        """Requirement: HTTP timeout must never hang the loop and must
        always resolve to a terminal failed result, not an indefinite
        SENT."""
        import requests

        def fake_request(method, url, **kwargs):
            raise requests.exceptions.Timeout("timed out")

        command_executor.requests.request = fake_request
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-007")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        payload = self.posted[0][1]["payload"]
        self.assertEqual(payload["status"], "failed")
        self.assertIn("timed out", payload["reason"])

    def test_missing_params_cannot_hang_and_default_to_current_position(self):
        """Requirement: missing/legacy params must never hang the flow --
        SET_HOME with no params at all still produces a terminal result
        with mode defaulted to current_position."""
        captured = self._fake_flask_response({"accepted": True, "verified": True})
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-008", params=None)
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(captured["json"]["mode"], "current_position")
        self.assertEqual(self.posted[0][1]["payload"]["status"], "executed")

    def test_command_history_records_start_and_finish(self):
        """Requirement 5: history shows both the in-flight "executing"
        record and the terminal record, not just the terminal one."""
        self._fake_flask_response({"accepted": True, "verified": True})
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-009")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        recent = command_history.get_recent()
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["status"], "executing")
        self.assertEqual(recent[1]["status"], "executed")
        self.assertTrue(all(r["command_id"] == "op-cmd-009" for r in recent))

    def test_duplicate_command_remains_idempotent_after_successful_ack(self):
        """A redelivered command_id must never re-trigger the vehicle Flask
        call. Here the first post to the operator succeeds immediately, so
        per requirement 6 the stored authoritative result is cleared right
        after that ack -- a genuine redelivery of the same command_id after
        that point (the operator's deliver-once queue should never do this,
        but must still be handled safely) has nothing left to resend and
        falls back to a plain terminal "rejected: duplicate", same as
        command_handler.py always did for an id with no stored result."""
        call_count = {"n": 0}

        def fake_request(method, url, **kwargs):
            call_count["n"] += 1

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"accepted": True, "verified": True}

            return _Resp()

        command_executor.requests.request = fake_request
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-dup-1")
        self._serve_commands([cmd])
        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self._serve_commands([dict(cmd)])
        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(call_count["n"], 1, "redelivered command_id must never re-reach Flask")
        self.assertEqual(len(self.posted), 2)
        self.assertEqual(self.posted[0][1]["payload"]["status"], "executed")
        self.assertEqual(self.posted[1][1]["payload"]["status"], "rejected")
        self.assertIn("duplicate", self.posted[1][1]["payload"]["reason"])

    def test_duplicate_command_before_any_successful_ack_resends_original_result(self):
        """The scenario requirements 2/4/8 actually target: the operator
        redelivers a command_id whose result was never successfully acked
        (still buffered, per test_eight_duplicate_polls_... above). Here
        that's driven directly through _poll_and_execute_commands with
        send_to_operator failing throughout, confirming the stored result
        (not a fresh rejection) is what gets posted-and-buffered on every
        redelivery up to the point of a successful ack."""
        call_count = {"n": 0}

        def fake_request(method, url, **kwargs):
            call_count["n"] += 1

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"accepted": True, "verified": True}

            return _Resp()

        command_executor.requests.request = fake_request

        def failing_send(endpoint, message):
            raise RuntimeError("no operator reachable")

        api_client.send_to_operator = failing_send
        local_agent.send_to_operator = failing_send

        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-dup-2")
        self._serve_commands([cmd])
        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self._serve_commands([dict(cmd)])
        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(call_count["n"], 1, "redelivered command_id must never re-reach Flask")
        self.assertEqual(len(self.posted), 0, "every send attempt failed, nothing reaches the operator directly")
        self.assertEqual(command_results.get_stored_result("op-cmd-dup-2")["status"], "executed")

    def test_eight_duplicate_polls_buffer_one_authoritative_entry_and_later_flush_resends_original(self):
        """Reproduces the live SET_HOME symptom this whole fix exists for:
        the operator backend redelivers the same claimed command_id many
        times (e.g. because it never got an acknowledged response), while
        every attempt to post the result back fails. Before this fix, each
        of those redeliveries produced its own distinct "duplicate"
        command_result, and every one of the 9 total attempts (1 executed +
        8 duplicate-rejected) got buffered as a separate line. Now: exactly
        one buffered entry for this command_id survives, it is the original
        nested accepted=false/verified=false/ack_result=MAV_RESULT_FAILED
        executed result, and a later successful flush sends that exact
        payload -- not a rejection."""
        flask_body = {
            "accepted": False, "verified": False, "command_id": "op-cmd-dup-9",
            "requested_position": None, "home_position": None,
            "verification_distance_m": None,
            "ack_result": "MAV_RESULT_FAILED",
            "error": {"code": "ACK_REJECTED", "message": "Pixhawk rejected MAV_CMD_DO_SET_HOME: MAV_RESULT_FAILED"},
        }
        call_count = {"n": 0}

        def fake_request(method, url, **kwargs):
            call_count["n"] += 1

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return flask_body

            return _Resp()

        command_executor.requests.request = fake_request

        def failing_send(endpoint, message):
            raise RuntimeError("no operator reachable")

        api_client.send_to_operator = failing_send
        local_agent.send_to_operator = failing_send

        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-dup-9")
        for _ in range(9):
            self._serve_commands([dict(cmd)])
            local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(call_count["n"], 1, "9 redeliveries of one command_id must reach the Flask endpoint only once")

        buffered = buffer.read_buffered_messages()
        command_results_buffered = [
            m for m in buffered
            if m.get("message_type") == "command_result" and m["payload"]["command_id"] == "op-cmd-dup-9"
        ]
        self.assertEqual(len(command_results_buffered), 1,
                          "9 redeliveries of one command_id must leave exactly one buffered entry")
        payload = command_results_buffered[0]["payload"]
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(payload["result"]["accepted"], False)
        self.assertEqual(payload["result"]["verified"], False)
        self.assertEqual(payload["result"]["ack_result"], "MAV_RESULT_FAILED")
        self.assertEqual(payload["result"]["error"]["code"], "ACK_REJECTED")

        # A later successful flush resends this exact original payload, not
        # a rejection, and clears it from the buffer and command_results.
        sent = []

        def succeeding_send(endpoint, message):
            sent.append((endpoint, message))
            return {"ok": True}

        api_client.send_to_operator = succeeding_send
        local_agent.send_to_operator = succeeding_send
        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result["sent"], 1)
        self.assertEqual(sent[0][1]["payload"], payload)
        self.assertIsNone(command_results.get_stored_result("op-cmd-dup-9"))

    def test_no_claimed_command_remains_sent_indefinitely_even_on_unexpected_exception(self):
        """Root-cause regression test for the reported bug: an unexpected
        exception *outside* command_executor.call_local_endpoint's own
        guarded block (simulated here by a broken command_log write) must
        still produce and post a terminal failed command_result, never
        silently crash the poll loop and leave the operator's command
        claimed forever with no result."""
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-crash-1")
        self._serve_commands([cmd])

        import command_handler
        orig_mark_processed = command_handler.mark_processed

        def broken_mark_processed(command_id):
            raise OSError("simulated disk failure writing command_log.jsonl")

        command_handler.mark_processed = broken_mark_processed
        try:
            local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])
        finally:
            command_handler.mark_processed = orig_mark_processed

        self.assertEqual(len(self.posted), 1, "a terminal command_result must still be posted")
        payload = self.posted[0][1]["payload"]
        self.assertEqual(payload["command_id"], "op-cmd-crash-1")
        self.assertEqual(payload["status"], "failed")
        self.assertIn("simulated disk failure", payload["reason"])

    def test_send_to_operator_failure_buffers_result_instead_of_dropping(self):
        """If posting the command_result itself fails (operator
        unreachable), the result must be buffered for later retry, not
        dropped -- still not left as a silent SENT with no eventual
        completion."""
        self._fake_flask_response({"accepted": True, "verified": True})

        def failing_send(endpoint, message):
            raise RuntimeError("no operator reachable")

        api_client.send_to_operator = failing_send
        local_agent.send_to_operator = failing_send

        buffered = []
        orig_buffer_message = local_agent.buffer_message
        local_agent.buffer_message = lambda message: buffered.append(message)
        try:
            cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-010")
            self._serve_commands([cmd])
            local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])
        finally:
            local_agent.buffer_message = orig_buffer_message

        self.assertEqual(len(buffered), 1)
        self.assertEqual(buffered[0]["payload"]["command_id"], "op-cmd-010")

    def test_set_home_executes_while_control_authority_is_operator(self):
        """The actual root cause this suite exists to catch: a queued
        SET_HOME command must execute (and post a command_result) while
        authority is OPERATOR, the state in which the operator command
        queue is explicit operator intent and every supported command_type
        executes -- SET_HOME included, no exemption (strict model, see
        README "Authority model"). Previously this was gated off at the
        polling level in local_agent.py itself, which left a queued
        SET_HOME command permanently pending with no command_result ever
        posted -- the "Set Home stuck on Setting..." symptom."""
        self._fake_flask_response({"accepted": True, "verified": True})
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-011")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(len(self.posted), 1)
        payload = self.posted[0][1]["payload"]
        self.assertEqual(payload["command_id"], "op-cmd-011")
        self.assertEqual(payload["status"], "executed")

    def test_set_home_rejected_with_explicit_reason_while_control_authority_is_local_agent(self):
        """The strict model: SET_HOME is not authority-exempt. While
        authority is LOCAL_AGENT, the operator queue is not the Local
        Agent's source of vehicle-control intent (its own autonomous
        writes are, gated separately by autonomy_gate.py) -- a queued
        SET_HOME is claimed (deliver-once) and explicitly rejected, not
        executed and not left silently pending."""
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-011c")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "LOCAL_AGENT", [])

        self.assertEqual(len(self.posted), 1)
        payload = self.posted[0][1]["payload"]
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("OPERATOR control authority", payload["reason"])

    def test_motion_command_rejected_with_explicit_reason_while_control_authority_is_local_agent(self):
        """A motion/mode command_type is blocked the same way SET_HOME now
        is -- a uniform gate, not a per-command_type exemption. Polling
        always happens (see local_agent._poll_and_execute_commands), so a
        blocked command is explicitly rejected with a reason rather than
        left silently pending in the operator's deliver-once queue."""
        cmd = _make_command(command_type="SET_MODE_HOLD", command_id="op-cmd-011b")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("CONNECTED", "LOCAL_AGENT", [])

        self.assertEqual(len(self.posted), 1)
        payload = self.posted[0][1]["payload"]
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("OPERATOR control authority", payload["reason"])

    def test_gated_off_while_disconnected(self):
        cmd = _make_command(command_type="SET_HOME", command_id="op-cmd-012")
        self._serve_commands([cmd])

        local_agent._poll_and_execute_commands("DISCONNECTED", "LOCAL_AGENT", [])

        self.assertEqual(len(self.posted), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
