"""
Non-JSON successful acknowledgements from the Operator. Run directly:

    python3 test_non_json_acknowledgement.py

send_to_operator used to build its return with a bare r.json(), so a 2xx
carrying an empty body (204) or a plain-text body raised ValueError out of the
*success* path. The caller could not tell that from a real failure: it buffered
an actually-delivered result and retried it forever
(OUTBOUND_BUFFER_REVIEW.md §6.4).

The contract pinned here:

  * Every successful HTTP 2xx is a terminal acknowledgement. The status, not
    the body, is what says "stop retaining".
  * A valid JSON body still classifies as ACK_APPLIED / ACK_TERMINAL_ORPHAN /
    ACK_ACCEPTED exactly as before.
  * An empty or non-JSON 2xx body is ACK_ACCEPTED -- never APPLIED, never
    TERMINAL_ORPHAN. Disposition is never inferred from response text.
  * The retained text is bounded, in both the return value and the log line.
  * 4xx / 5xx / timeout / connection failure remain retryable and retained.
"""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import requests

import config
# Throwaway temp files, set before importing the modules under test. buffer.py
# snapshots BUFFER_FILE at import time, so this override must happen first --
# the real agent_buffer.jsonl / command_results.json are never touched.
config.BUFFER_FILE = tempfile.mktemp(suffix=".jsonl")
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")
config.OPERATOR_URLS = ["http://operator-a:8200"]

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


_NO_JSON = object()


class _Resp:
    """A real-ish response: .json() raises ValueError on a non-JSON body,
    exactly as requests' does."""

    def __init__(self, status_code, text, json_body=_NO_JSON):
        self.status_code = status_code
        self.text = text
        self._json_body = json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        if self._json_body is _NO_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_body


def _json_resp(status_code, body):
    return _Resp(status_code, text="<json>", json_body=body)


def _non_json_resp(status_code, text):
    return _Resp(status_code, text=text)


def _stub_post(response):
    requests.post = lambda url, json=None, timeout=None: response


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
        api_client._last_good_url = None
        self._orig_post = requests.post
        self._orig_send = local_agent.send_to_operator

    def tearDown(self):
        requests.post = self._orig_post
        local_agent.send_to_operator = self._orig_send

    def _deliver(self, command_id, response):
        """Drive the real send_to_operator through both stores: buffer the
        result AND retain it, then flush. Proves the end-to-end path, not just
        the classifier."""
        buffer.buffer_message(_buffered_result_message(command_id))
        command_results.store_result(command_id, _result_payload(command_id))
        _stub_post(response)
        return buffer.flush_buffer(local_agent._send_buffered)

    def assertBothStoresCleared(self, command_id, flush_result):
        self.assertEqual(flush_result, {"sent": 1, "remaining": 0})
        self.assertEqual(buffer.read_buffered_messages(), [])
        self.assertIsNone(command_results.get_stored_result(command_id))


class TestSuccessfulJsonBodiesUnchanged(_Base):
    """The pre-existing JSON dispositions must survive the fix untouched."""

    def test_200_applied_json_is_applied_and_clears_both_stores(self):
        result = self._deliver("cid-applied", _json_resp(200, APPLIED_BODY))

        self.assertBothStoresCleared("cid-applied", result)
        response = api_client.send_to_operator("/agent/command_result", {})
        self.assertEqual(
            api_client.classify_command_result_ack(response),
            api_client.ACK_APPLIED,
        )

    def test_200_orphan_json_is_terminal_orphan_and_clears_both_stores(self):
        result = self._deliver("cid-orphan", _json_resp(200, ORPHAN_BODY))

        self.assertBothStoresCleared("cid-orphan", result)
        response = api_client.send_to_operator("/agent/command_result", {})
        self.assertEqual(
            api_client.classify_command_result_ack(response),
            api_client.ACK_TERMINAL_ORPHAN,
        )


class TestNonJsonSuccessIsTerminal(_Base):
    """The fix: a 2xx that isn't JSON is a delivery, not a failure."""

    def test_204_empty_body_is_accepted_and_clears_both_stores(self):
        result = self._deliver("cid-204", _non_json_resp(204, ""))

        self.assertBothStoresCleared("cid-204", result)

    def test_200_plain_text_is_accepted_and_clears_both_stores(self):
        result = self._deliver("cid-text", _non_json_resp(200, "OK"))

        self.assertBothStoresCleared("cid-text", result)

    def test_non_json_2xx_does_not_raise_out_of_the_success_path(self):
        """The defect itself: r.json() raising made a delivered result look
        like a failed send."""
        _stub_post(_non_json_resp(204, ""))

        response = api_client.send_to_operator("/agent/command_result", {})

        self.assertTrue(response["ok"])
        self.assertEqual(response["operator"], "http://operator-a:8200")

    def test_non_json_body_is_classified_accepted_never_applied_or_orphan(self):
        """Disposition is read only from an affirmative JSON statement --
        never inferred from response text, however suggestive that text is."""
        for text in ("", "OK", "applied", "orphaned", "applied=true",
                     "<html><body>202 Accepted</body></html>"):
            with self.subTest(text=text):
                _stub_post(_non_json_resp(200, text))
                response = api_client.send_to_operator("/agent/command_result", {})
                self.assertEqual(
                    api_client.classify_command_result_ack(response),
                    api_client.ACK_ACCEPTED,
                )

    def test_non_json_body_is_marked_and_bounded_in_the_return_value(self):
        oversized = "x" * (api_client.MAX_NON_JSON_BODY_CHARS * 10)
        _stub_post(_non_json_resp(200, oversized))

        response = api_client.send_to_operator("/agent/command_result", {})

        body = response["response"]
        self.assertEqual(body["body_format"], "non_json")
        self.assertEqual(len(body["text"]), api_client.MAX_NON_JSON_BODY_CHARS)
        self.assertTrue(oversized.startswith(body["text"]))

    def test_non_json_body_is_bounded_in_the_log_line(self):
        oversized = "y" * (api_client.MAX_NON_JSON_BODY_CHARS * 10)
        _stub_post(_non_json_resp(200, oversized))

        out = io.StringIO()
        with redirect_stdout(out):
            api_client.send_to_operator("/agent/command_result", {})

        logged = out.getvalue()
        self.assertIn("non-JSON", logged)
        self.assertNotIn("y" * (api_client.MAX_NON_JSON_BODY_CHARS + 1), logged)

    def test_empty_body_keeps_an_empty_string_not_none(self):
        _stub_post(_non_json_resp(204, ""))

        response = api_client.send_to_operator("/agent/command_result", {})

        self.assertEqual(response["response"]["text"], "")


class TestRetryableFailuresUnchanged(_Base):
    """4xx/5xx/timeout/connection failure must still retain in BOTH stores."""

    def _assert_retained(self, command_id):
        self.assertEqual(len(buffer.read_buffered_messages()), 1)
        self.assertIsNotNone(command_results.get_stored_result(command_id))

    def test_http_400_with_non_json_body_still_retains(self):
        result = self._deliver("cid-400", _non_json_resp(400, "Bad Request"))

        self.assertEqual(result, {"sent": 0, "remaining": 1})
        self._assert_retained("cid-400")

    def test_http_500_with_non_json_body_still_retains(self):
        result = self._deliver("cid-500", _non_json_resp(500, "Internal Server Error"))

        self.assertEqual(result, {"sent": 0, "remaining": 1})
        self._assert_retained("cid-500")

    def test_timeout_still_retains(self):
        buffer.buffer_message(_buffered_result_message("cid-timeout"))
        command_results.store_result("cid-timeout", _result_payload("cid-timeout"))

        def _timeout(url, json=None, timeout=None):
            raise requests.exceptions.ReadTimeout("read timed out")

        requests.post = _timeout

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 0, "remaining": 1})
        self._assert_retained("cid-timeout")

    def test_connection_failure_still_retains(self):
        buffer.buffer_message(_buffered_result_message("cid-conn"))
        command_results.store_result("cid-conn", _result_payload("cid-conn"))

        def _refused(url, json=None, timeout=None):
            raise requests.exceptions.ConnectionError("connection refused")

        requests.post = _refused

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 0, "remaining": 1})
        self._assert_retained("cid-conn")


class TestStatusMessagesUnaffected(_Base):
    """A status message flushes on a non-JSON 2xx exactly as on a JSON one --
    it never consults the ack disposition."""

    def test_status_message_flushes_on_non_json_2xx(self):
        buffer.buffer_message({"message_type": "status", "payload": {"mode": "AUTO"}})
        _stub_post(_non_json_resp(204, ""))

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 1, "remaining": 0})
        self.assertEqual(buffer.read_buffered_messages(), [])

    def test_status_message_still_retained_on_failure(self):
        buffer.buffer_message({"message_type": "status", "payload": {"mode": "AUTO"}})
        _stub_post(_non_json_resp(500, "Internal Server Error"))

        result = buffer.flush_buffer(local_agent._send_buffered)

        self.assertEqual(result, {"sent": 0, "remaining": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
