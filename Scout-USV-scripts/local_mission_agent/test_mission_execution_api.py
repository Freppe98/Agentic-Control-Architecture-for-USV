"""
Standalone tests for the mission-execution HTTP surface: mission_execution_api.py
(operation layer) and the agent_server.py routing for it.

    python3 test_mission_execution_api.py

Exercises the API through a live stdlib HTTP server on an ephemeral port with a
registered controller backed by a fake gateway -- proving the real routes, the
structured error/idempotency contract, the canonical status schema, and that the
existing /agent/replan/* surface is untouched (no regression).
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import agent_server
import mission_execution_config as me_cfg
import mission_execution_controller as mec
import mission_execution_runtime
import planning_package as pp
import write_arbiter
from test_mission_execution_controller import (
    FakeGateway, _HOME, _ROUTE, _cfg, _store_verified_package)


def _http(method, path, body=None, port=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class _ServerBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), agent_server.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        write_arbiter._reset_for_tests()
        self.dir = tempfile.mkdtemp()
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        route_hash = _store_verified_package("m1")
        self.gw = FakeGateway()
        self.gw.pixhawk_route_hash = route_hash
        self.ctrl = mec.MissionExecutionController(cfg=_cfg(), gateway=self.gw)
        mission_execution_runtime.register(self.ctrl)
        # Prove readiness (usable package + package/Pixhawk hash match +
        # LOCAL_AGENT authority + fresh state) so READY/can_start holds.
        self.ctrl.refresh_readiness()

    def tearDown(self):
        write_arbiter._reset_for_tests()
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))


class TestRoutes(_ServerBase):
    def test_status_route(self):
        code, body = _http("GET", "/agent/mission_execution/status", port=self.port)
        self.assertEqual(code, 200)
        self.assertTrue(body["supported"])
        self.assertIn("can_start", body)
        self.assertTrue(body["can_start"])

    def test_start_route(self):
        code, body = _http("POST", "/agent/mission_execution/start", {"mission_id": "m1"}, port=self.port)
        self.assertEqual(code, 200)
        self.assertEqual(body["outcome"], mec.RUNNING)
        self.assertEqual(body["verified_mode"], "AUTO")
        self.assertTrue(body["final"])

    def test_start_then_pause_then_resume(self):
        _http("POST", "/agent/mission_execution/start", {"mission_id": "m1"}, port=self.port)
        code, body = _http("POST", "/agent/mission_execution/pause", {}, port=self.port)
        self.assertEqual(code, 200)
        self.assertEqual(body["outcome"], mec.PAUSED)
        code, body = _http("POST", "/agent/mission_execution/resume", {}, port=self.port)
        self.assertEqual(code, 200)
        self.assertEqual(body["outcome"], mec.RUNNING)

    def test_pause_idempotent(self):
        _http("POST", "/agent/mission_execution/start", {"mission_id": "m1"}, port=self.port)
        _http("POST", "/agent/mission_execution/pause", {}, port=self.port)
        code, body = _http("POST", "/agent/mission_execution/pause", {}, port=self.port)
        self.assertEqual(code, 200)
        self.assertTrue(body.get("idempotent"))

    def test_start_duplicate_reports_running(self):
        _http("POST", "/agent/mission_execution/start", {"mission_id": "m1"}, port=self.port)
        code, body = _http("POST", "/agent/mission_execution/start", {"mission_id": "m1"}, port=self.port)
        self.assertEqual(code, 200)
        self.assertTrue(body.get("idempotent"))
        self.assertEqual(body["current_state"], mec.RUNNING)

    def test_start_mission_id_mismatch_structured_error(self):
        code, body = _http("POST", "/agent/mission_execution/start",
                           {"mission_id": "wrong"}, port=self.port)
        self.assertEqual(code, 200)  # accepted request, vehicle-level failure in body
        self.assertEqual(body["error"]["code"], "MISSION_ID_MISMATCH")

    def test_rearm_route(self):
        self.gw.loiter_verified = False
        _http("POST", "/agent/mission_execution/start", {"mission_id": "m1"}, port=self.port)  # FAILED
        code, body = _http("POST", "/agent/mission_execution/rearm", {}, port=self.port)
        self.assertEqual(code, 200)
        self.assertTrue(body["accepted"])

    def test_pause_rejected_returns_409(self):
        # Not running -> pause is a precondition rejection -> 409.
        code, body = _http("POST", "/agent/mission_execution/pause", {}, port=self.port)
        self.assertEqual(code, 409)
        self.assertFalse(body["accepted"])
        self.assertEqual(body["error"]["code"], "NOT_PAUSABLE")

    def test_status_schema_over_http(self):
        code, body = _http("GET", "/agent/mission_execution/status", port=self.port)
        for key in ("state", "sequence", "replanning", "return_completion",
                    "can_start", "can_pause", "can_resume", "verified_home"):
            self.assertIn(key, body)

    def test_reprove_binding_route(self):
        # setUp already ran refresh_readiness(), so this call is idempotent.
        code, body = _http("POST", "/agent/mission_execution/reprove_binding", {}, port=self.port)
        self.assertEqual(code, 200)
        self.assertTrue(body["accepted"])
        self.assertIn(body["outcome"], ("REPROVED", "ALREADY_PROVEN"))
        self.assertTrue(body["read_only"])
        self.assertIsNotNone(body["verified_route_hash"])
        self.assertTrue(body["can_start"])

    def test_reprove_binding_route_no_body(self):
        # mission_id in the body is optional (an expectation constraint only).
        code, body = _http("POST", "/agent/mission_execution/reprove_binding", None, port=self.port)
        self.assertEqual(code, 200)
        self.assertTrue(body["accepted"])

    def test_reprove_binding_route_mission_id_mismatch(self):
        code, body = _http("POST", "/agent/mission_execution/reprove_binding",
                           {"mission_id": "not-m1"}, port=self.port)
        self.assertEqual(code, 200)  # accepted request, structured mismatch in body
        self.assertEqual(body["outcome"], "MISSION_ID_MISMATCH")
        self.assertEqual(body["expected_mission_id"], "not-m1")

    def test_reprove_binding_never_writes_to_vehicle(self):
        _http("POST", "/agent/mission_execution/reprove_binding", {}, port=self.port)
        self.assertEqual(self.gw.write_calls, [])

    def test_reprove_binding_get_is_404(self):
        code, _ = _http("GET", "/agent/mission_execution/reprove_binding", port=self.port)
        self.assertEqual(code, 404)


class TestNoRegression(_ServerBase):
    def test_replan_status_still_served(self):
        # The replanning surface must be unaffected by the new routes.
        code, body = _http("GET", "/agent/replan/status", port=self.port)
        self.assertIn(code, (200, 503))  # 503 only if no replan controller registered

    def test_mission_operation_route_not_shadowed(self):
        # "/agent/mission_execution/status" must not be swallowed by the
        # "/agent/mission" prefix branch, and vice-versa.
        code, _ = _http("GET", "/agent/mission_execution/status", port=self.port)
        self.assertEqual(code, 200)
        code2, _ = _http("GET", "/agent/mission_operation", port=self.port)
        self.assertEqual(code2, 200)

    def test_unknown_route_404(self):
        code, _ = _http("GET", "/agent/mission_execution/nope", port=self.port)
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
