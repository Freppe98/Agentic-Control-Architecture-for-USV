"""
Standalone tests for mission_execution_gateway.py -- the HTTP adapter to the
vehicle Flask service.

    python3 test_mission_execution_gateway.py

Runs a fake Flask-shaped stdlib HTTP server that records the requests the
gateway makes, proving each gateway method hits the correct existing endpoint
with the correct method/body and relays the structured result unchanged. No real
Flask, no MAVLink, no Pixhawk.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mission_execution_gateway as meg

_RECORDED = []
_RESPONSES = {}


class _FakeFlask(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode()) if raw else None
        _RECORDED.append({"method": self.command, "path": self.path, "body": body})
        return body

    def do_GET(self):
        self._record()
        self._send(_RESPONSES.get(self.path, {"ok": True}))

    def do_POST(self):
        self._record()
        self._send(_RESPONSES.get(self.path, {"ok": True}))


class TestGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeFlask)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.gw = meg.FlaskMissionExecutionGateway(base_url=f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _RECORDED.clear()
        _RESPONSES.clear()

    def test_current_authority(self):
        _RESPONSES["/agent/control_authority"] = {"authority": "LOCAL_AGENT"}
        self.assertEqual(self.gw.current_authority(), "LOCAL_AGENT")
        self.assertEqual(_RECORDED[0]["method"], "GET")
        self.assertEqual(_RECORDED[0]["path"], "/agent/control_authority")

    def test_read_vehicle_state(self):
        _RESPONSES["/agent/state"] = {"telemetry": {"mode_name": "AUTO"}}
        out = self.gw.read_vehicle_state()
        self.assertEqual(out["telemetry"]["mode_name"], "AUTO")
        self.assertEqual(_RECORDED[0]["path"], "/agent/state")

    def test_pixhawk_mission_readback(self):
        _RESPONSES["/agent/pixhawk_mission"] = {
            "reachable": True, "partial": False, "mission_valid": True,
            "route_content_hash": "sha256:" + "a" * 64, "route_waypoint_count": 40}
        out = self.gw.pixhawk_mission_readback()
        self.assertEqual(out["route_waypoint_count"], 40)
        self.assertEqual(_RECORDED[0]["method"], "GET")
        self.assertEqual(_RECORDED[0]["path"], "/agent/pixhawk_mission")

    def test_home_status_fails_safe(self):
        # Point at a dead port -> unreachable -> unverified stub, never raises.
        gw = meg.FlaskMissionExecutionGateway(base_url="http://127.0.0.1:1")
        out = gw.home_status()
        self.assertFalse(out["verified"])
        self.assertFalse(out["ready_for_auto"])

    def test_command_loiter(self):
        _RESPONSES["/nav/loiter"] = {"verified": True, "observed_mode": 5}
        out = self.gw.command_loiter()
        self.assertTrue(out["verified"])
        self.assertEqual(_RECORDED[0]["method"], "POST")
        self.assertEqual(_RECORDED[0]["path"], "/nav/loiter")

    def test_command_auto(self):
        _RESPONSES["/nav/AutoModeOn"] = {"verified": True, "observed_mode": 10}
        out = self.gw.command_auto()
        self.assertTrue(out["verified"])
        self.assertEqual(_RECORDED[0]["path"], "/nav/AutoModeOn")

    def test_command_arm(self):
        _RESPONSES["/nav/ArmOn"] = {"accepted": True, "verified": True, "armed": True,
                                    "ack_result": "MAV_RESULT_ACCEPTED", "error": None}
        out = self.gw.command_arm()
        self.assertTrue(out["verified"])
        self.assertTrue(out["armed"])
        self.assertEqual(_RECORDED[0]["method"], "POST")
        self.assertEqual(_RECORDED[0]["path"], "/nav/ArmOn")

    def test_set_home_body(self):
        _RESPONSES["/agent/set_home"] = {"accepted": True, "verified": True,
                                         "verification_distance_m": 1.0}
        out = self.gw.set_home(command_id="op-1", tolerance_m=5.0, freshness_s=5.0)
        self.assertTrue(out["verified"])
        rec = _RECORDED[0]
        self.assertEqual(rec["method"], "POST")
        self.assertEqual(rec["path"], "/agent/set_home")
        self.assertEqual(rec["body"]["command_id"], "op-1")
        self.assertEqual(rec["body"]["mode"], "current_position")
        self.assertEqual(rec["body"]["tolerance_m"], 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
