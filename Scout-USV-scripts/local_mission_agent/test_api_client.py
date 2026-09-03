"""
Standalone tests for api_client.send_to_operator's failure-mode handling
(requirement 7 of the buffering/dedup fix): an HTTP 400 (or other 4xx) from
a reachable operator must be reported as a protocol rejection with the
response body, never folded into the generic "No operator reachable"
message used for genuine connectivity failures. Run directly:

    python3 test_api_client.py
"""
import unittest

import requests

import config
config.OPERATOR_URLS = ["http://operator-a:8200"]

import api_client


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    @property
    def text(self):
        return str(self._body)


class TestSendToOperatorFailureModes(unittest.TestCase):
    def setUp(self):
        api_client._last_good_url = None
        self._orig_post = requests.post
        self._orig_urls = api_client.OPERATOR_URLS

    def tearDown(self):
        requests.post = self._orig_post
        api_client.OPERATOR_URLS = self._orig_urls

    def test_http_400_is_reported_as_protocol_rejection_with_body(self):
        body = {"error": "malformed command_result: missing command_id"}
        requests.post = lambda url, json=None, timeout=None: _Resp(400, body)

        with self.assertRaises(RuntimeError) as ctx:
            api_client.send_to_operator("/agent/command_result", {"payload": {}})

        message = str(ctx.exception)
        self.assertNotIn("No operator reachable", message)
        self.assertIn("400", message)
        self.assertIn("missing command_id", message)

    def test_connection_failure_is_still_reported_as_unreachable(self):
        def raise_connection_error(url, json=None, timeout=None):
            raise requests.exceptions.ConnectionError("connection refused")

        requests.post = raise_connection_error

        with self.assertRaises(RuntimeError) as ctx:
            api_client.send_to_operator("/agent/command_result", {"payload": {}})

        self.assertIn("No operator reachable", str(ctx.exception))

    def test_successful_2xx_still_succeeds(self):
        requests.post = lambda url, json=None, timeout=None: _Resp(200, {"ok": True})

        result = api_client.send_to_operator("/agent/command_result", {"payload": {}})
        self.assertTrue(result["ok"])


class TestOutboundCallsAreBounded(unittest.TestCase):
    """
    E3 critical-safety check: an unreachable Operator must never stall the
    Local Agent's main loop indefinitely (task: "verify that communication
    reporting to the Operator cannot block these local loops"). Every
    outbound Operator call uses a finite (connect, read) timeout tuple built
    from config.OPERATOR_CONNECT_TIMEOUT/OPERATOR_READ_TIMEOUT -- never
    `timeout=None` (no timeout at all) and never a single scalar that would
    silently reuse the connect bound for reads too.
    """

    def setUp(self):
        api_client._last_good_url = None
        self._orig_post = requests.post
        self._orig_get = requests.get

    def tearDown(self):
        requests.post = self._orig_post
        requests.get = self._orig_get

    def test_send_to_operator_uses_bounded_connect_and_read_timeout(self):
        seen = {}

        def fake_post(url, json=None, timeout=None):
            seen["timeout"] = timeout
            return _Resp(200, {"ok": True})

        requests.post = fake_post
        api_client.send_to_operator("/agent/status", {"payload": {}})

        self.assertIsInstance(seen["timeout"], tuple)
        connect_timeout, read_timeout = seen["timeout"]
        self.assertEqual(connect_timeout, api_client.OPERATOR_CONNECT_TIMEOUT)
        self.assertEqual(read_timeout, api_client.OPERATOR_READ_TIMEOUT)
        self.assertGreater(connect_timeout, 0)
        self.assertGreater(read_timeout, 0)

    def test_get_pending_commands_uses_bounded_timeout_and_never_raises(self):
        def fake_get(url, params=None, timeout=None):
            raise requests.exceptions.ConnectTimeout("simulated Operator outage")

        requests.get = fake_get
        # Must degrade to "nothing to report" rather than propagate -- see
        # get_pending_commands' own docstring (comm DISCONNECTED relies on
        # backend queueing, not a raised exception the main loop must catch).
        result = api_client.get_pending_commands("usv-2")
        self.assertEqual(result, [])

    def test_get_pending_commands_passes_bounded_timeout(self):
        seen = {}

        def fake_get(url, params=None, timeout=None):
            seen["timeout"] = timeout
            return _Resp(200, {"commands": []})

        requests.get = fake_get
        api_client.get_pending_commands("usv-2")
        self.assertIsInstance(seen["timeout"], tuple)
        self.assertTrue(all(t > 0 for t in seen["timeout"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
