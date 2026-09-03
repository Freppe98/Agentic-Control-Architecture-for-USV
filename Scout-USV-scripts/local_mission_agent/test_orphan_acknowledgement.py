"""
Terminal ORPHAN acknowledgement of a command_result. Run directly:

    python3 test_orphan_acknowledgement.py

Operator commit 6a9214b changed POST /agent/command_result for a present but
unknown command_id. It no longer 4xx-rejects; it answers HTTP 200 with

    {"ok": true, "found": false, "applied": false,
     "orphaned": true, "error": "unknown command id"}

meaning: not applied to any current Operator command, archived instead as an
orphaned historical audit record, acknowledgement terminal, stop retrying.

The cross-system contract that follows (see OUTBOUND_BUFFER_REVIEW.md §4):

  * Terminal ack  -> the result leaves BOTH stores: the agent_buffer.jsonl
    line (via normal successful-flush behaviour -- flush_buffer keeps only
    what raised) and the retained command_results.json entry (via
    clear_result). Both already followed from the 200 alone; these tests pin
    that down so a future change to the 2xx path cannot regress it silently.
  * orphaned != applied. The disposition is classified and logged
    distinctly; an orphan is never reported as an applied result.
  * Retryable failures are untouched: 400 malformed, 500, timeout and
    connection failure all keep the result buffered AND retained.
  * Nothing is ever dropped for age or retry count.
"""
import os
import tempfile
import unittest

import requests

import config
# Throwaway temp files, set before importing the modules under test. buffer.py
# snapshots BUFFER_FILE at import time, so this override must happen first --
# the real agent_buffer.jsonl / command_results.json are never touched.
config.BUFFER_FILE = tempfile.mktemp(suffix=".jsonl")
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")

import api_client
import buffer
import command_results
import local_agent


def tearDownModule():
    for path in (config.BUFFER_FILE, config.COMMAND_RESULTS_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


ORPHAN_BODY = {
    "ok": True,
    "found": False,
    "applied": False,
    "orphaned": True,
    "error": "unknown command id",
}
APPLIED_BODY = {"ok": True, "found": True, "applied": True}


def _ack(body):
    """A successful send_to_operator() return carrying `body`."""
    return {"ok": True, "operator": "http://operator-a:8200", "response": body}


def _result_payload(command_id, status="executed"):
    return {
        "command_id": command_id,
        "usv_id": "usv-1",
        "command_type": "SET_HOME",
        "status": status,
        "timestamp": 1_700_000_000.0,
    }


def _buffered_result_message(command_id):
    return {
        "message_type": "command_result",
        "source": "usv-1",
        "target": "operator",
        "payload": _result_payload(command_id),
    }


class _Base(unittest.TestCase):
    def setUp(self):
        for path in (config.BUFFER_FILE, config.COMMAND_RESULTS_FILE):
            if os.path.exists(path):
                os.remove(path)
        self._orig_send = local_agent.send_to_operator

    def tearDown(self):
        local_agent.send_to_operator = self._orig_send

    def _stub_send(self, response=None, raises=None):
        """Replace the operator POST used by BOTH delivery paths."""
        self.sent = []

        def _send(endpoint, message):
            self.sent.append((endpoint, message))
            if raises is not None:
                raise raises
            return response

        local_agent.send_to_operator = _send


class TestAckClassification(_Base):
    """api_client.classify_command_result_ack -- disposition is read only from
    an affirmative Operator statement, never inferred."""

    def test_orphaned_and_not_found_is_terminal_orphan(self):
        self.assertEqual(
            api_client.classify_command_result_ack(_ack(ORPHAN_BODY)),
            api_client.ACK_TERMINAL_ORPHAN,
        )

    def test_applied_is_applied(self):
        self.assertEqual(
            api_client.classify_command_result_ack(_ack(APPLIED_BODY)),
            api_client.ACK_APPLIED,
        )

    def test_orphan_is_never_classified_as_applied(self):
        """Requirement 3. Also covers a contradictory body carrying both
        flags -- orphan is tested first, so applied can never win."""
        self.assertNotEqual(
            api_client.classify_command_result_ack(_ack(ORPHAN_BODY)),
            api_client.ACK_APPLIED,
        )
        both = dict(ORPHAN_BODY, applied=True)
        self.assertEqual(
            api_client.classify_command_result_ack(_ack(both)),
            api_client.ACK_TERMINAL_ORPHAN,
        )

    def test_partial_or_unknown_bodies_are_not_inferred_as_orphan(self):
        """A body that merely omits the flags, carries only one of the pair,
        or isn't a JSON object is an ordinary accepted delivery. Terminal
        orphan requires BOTH orphaned=true and found=false."""
        for body in (
            {"ok": True},                                  # older Operator
            {"ok": True, "found": False},                  # half the pair
            {"ok": True, "orphaned": True},                # half the pair
            {"ok": True, "orphaned": "true", "found": "false"},  # strings
            "accepted",                                    # non-JSON body
            None,
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    api_client.classify_command_result_ack(_ack(body)),
                    api_client.ACK_ACCEPTED,
                )


class TestRetainedResultCleared(_Base):
    """command_results.json -- the authoritative retained result."""

    def test_applied_clears_retained_result(self):
        command_results.store_result("cid-applied", _result_payload("cid-applied"))
        self._stub_send(response=_ack(APPLIED_BODY))

        local_agent._deliver_command_result(_result_payload("cid-applied"))

        self.assertIsNone(command_results.get_stored_result("cid-applied"))

    def test_terminal_orphan_clears_retained_result(self):
        command_results.store_result("cid-orphan", _result_payload("cid-orphan"))
        self._stub_send(response=_ack(ORPHAN_BODY))

        local_agent._deliver_command_result(_result_payload("cid-orphan"))

        self.assertIsNone(command_results.get_stored_result("cid-orphan"))
        self.assertEqual(len(self.sent), 1, "delivered exactly once, not retried")

    def test_unknown_body_is_cleared_as_an_ordinary_accepted_delivery(self):
        """A 2xx has always meant "stop retaining" (OUTBOUND_BUFFER_REVIEW.md
        §4) and that known-command semantic is deliberately unchanged. What
        must NOT happen is the unknown body being *classified* as a terminal
        orphan -- i.e. it is cleared as a plain accepted delivery, not
        silently treated as one the Operator abandoned."""
        command_results.store_result("cid-plain", _result_payload("cid-plain"))
        response = _ack({"ok": True})
        self._stub_send(response=response)

        local_agent._deliver_command_result(_result_payload("cid-plain"))

        self.assertEqual(
            api_client.classify_command_result_ack(response),
            api_client.ACK_ACCEPTED,
        )
        self.assertIsNone(command_results.get_stored_result("cid-plain"))

    def test_duplicate_orphan_acknowledgement_is_harmless(self):
        """The same orphan ack arriving twice (a redelivery racing a flush, a
        restart replaying a buffered line) must not raise and must leave the
        stores in the same already-cleared state -- clear_result on a missing
        id is a no-op."""
        command_results.store_result("cid-dup", _result_payload("cid-dup"))
        self._stub_send(response=_ack(ORPHAN_BODY))

        local_agent._deliver_command_result(_result_payload("cid-dup"))
        local_agent._deliver_command_result(_result_payload("cid-dup"))

        self.assertIsNone(command_results.get_stored_result("cid-dup"))
        self.assertEqual(buffer.read_buffered_messages(), [])


class TestOutboundBuffer(_Base):
    """agent_buffer.jsonl -- removal happens through normal successful-flush
    behaviour, not a special-cased drop."""

    def test_orphan_acknowledgement_removes_the_buffered_result(self):
        buffer.buffer_message(_buffered_result_message("cid-buffered"))
        command_results.store_result("cid-buffered", _result_payload("cid-buffered"))
        self._stub_send(response=_ack(ORPHAN_BODY))

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 1, "remaining": 0})
        self.assertEqual(buffer.read_buffered_messages(), [])
        self.assertIsNone(command_results.get_stored_result("cid-buffered"))

    def test_http_400_malformed_stays_buffered_and_retained(self):
        """Requirement 5: a malformed-payload rejection is a diagnosable bug
        on this side, not a terminal disposition. Nothing is dropped."""
        buffer.buffer_message(_buffered_result_message("cid-400"))
        command_results.store_result("cid-400", _result_payload("cid-400"))
        self._stub_send(raises=RuntimeError(
            "Operator rejected request: HTTP 400 protocol rejection: "
            "{'error': 'malformed command_result: missing command_id'}"))

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 0, "remaining": 1})
        self.assertEqual(len(buffer.read_buffered_messages()), 1)
        self.assertIsNotNone(command_results.get_stored_result("cid-400"))

    def test_http_500_stays_buffered_and_retained(self):
        buffer.buffer_message(_buffered_result_message("cid-500"))
        command_results.store_result("cid-500", _result_payload("cid-500"))
        self._stub_send(raises=RuntimeError("No operator reachable: 500 Server Error"))

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 0, "remaining": 1})
        self.assertEqual(len(buffer.read_buffered_messages()), 1)
        self.assertIsNotNone(command_results.get_stored_result("cid-500"))

    def test_timeout_stays_buffered_and_retained(self):
        buffer.buffer_message(_buffered_result_message("cid-timeout"))
        command_results.store_result("cid-timeout", _result_payload("cid-timeout"))
        self._stub_send(raises=requests.exceptions.ReadTimeout("read timed out"))

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 0, "remaining": 1})
        self.assertEqual(len(buffer.read_buffered_messages()), 1)
        self.assertIsNotNone(command_results.get_stored_result("cid-timeout"))

    def test_connection_failure_stays_buffered_and_retained(self):
        buffer.buffer_message(_buffered_result_message("cid-conn"))
        command_results.store_result("cid-conn", _result_payload("cid-conn"))
        self._stub_send(raises=requests.exceptions.ConnectionError("connection refused"))

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 0, "remaining": 1})
        self.assertEqual(len(buffer.read_buffered_messages()), 1)
        self.assertIsNotNone(command_results.get_stored_result("cid-conn"))

    def test_retryable_failure_survives_many_retries_without_being_dropped(self):
        """Requirement 6: nothing is dropped on age or retry count. Only an
        affirmative Operator disposition ends retention."""
        buffer.buffer_message(_buffered_result_message("cid-persistent"))
        command_results.store_result("cid-persistent", _result_payload("cid-persistent"))
        self._stub_send(raises=RuntimeError("No operator reachable: connection refused"))

        for _ in range(50):
            buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(len(buffer.read_buffered_messages()), 1)
        self.assertIsNotNone(command_results.get_stored_result("cid-persistent"))

        # ...and the moment the Operator answers, it leaves both stores.
        self._stub_send(response=_ack(ORPHAN_BODY))
        buffer.flush_buffer(local_agent._send_buffered)
        self.assertEqual(buffer.read_buffered_messages(), [])
        self.assertIsNone(command_results.get_stored_result("cid-persistent"))


class TestStatusMessagesUnaffected(_Base):
    """Requirement 4, other direction: only command_result messages consult
    the ack disposition -- a status message flushes exactly as before."""

    def test_status_message_flush_is_unchanged_by_orphan_body(self):
        buffer.buffer_message({"message_type": "status", "payload": {"mode": "AUTO"}})
        self._stub_send(response=_ack(ORPHAN_BODY))

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 1, "remaining": 0})
        self.assertEqual(self.sent[0][0], "/agent/status")


if __name__ == "__main__":
    unittest.main(verbosity=2)
