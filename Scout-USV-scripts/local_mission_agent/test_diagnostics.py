"""
Standalone tests for diagnostics.py (GET /agent/diagnostics and
POST /agent/system_check logic). No pytest dependency -- run directly:

    python3 test_diagnostics.py

Monkeypatches api_client/communication/runtime_status entirely -- no live
vehicle Flask service, mavlink2rest, or operator backend required.
"""
import time
import unittest
from unittest.mock import patch

import diagnostics
import runtime_status


def _flask_diag_ok():
    return {
        "mavlink": {"status": "OK"},
        "pixhawk": {"status": "OK", "message": "heartbeat received"},
        "gps": {"status": "OK"},
        "battery": {"status": "OK"},
        "rc_receiver": {"status": "UNKNOWN"},
        "camera": {"status": "OK"},
        "mission_service": {"status": "OK"},
        "storage": {"status": "OK"},
        "cpu": {"status": "OK"},
        "memory": {"status": "OK"},
    }


class TestBuildDiagnosticsLocalComponents(unittest.TestCase):
    def setUp(self):
        runtime_status._last_alive_ts = None

    def test_connected_comm_state_is_ok(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()):
            diag = diagnostics.build_diagnostics()
        self.assertEqual(diag["communication"]["status"], "OK")

    def test_disconnected_comm_state_is_fail(self):
        with patch("diagnostics.communication.get_comm_state", return_value="DISCONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()):
            diag = diagnostics.build_diagnostics()
        self.assertEqual(diag["communication"]["status"], "FAIL")

    def test_local_agent_unknown_before_first_loop_iteration(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()):
            diag = diagnostics.build_diagnostics()
        self.assertEqual(diag["local_agent"]["status"], "UNKNOWN")

    def test_local_agent_ok_after_recent_heartbeat(self):
        runtime_status.mark_alive()
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()):
            diag = diagnostics.build_diagnostics()
        self.assertEqual(diag["local_agent"]["status"], "OK")

    def test_local_agent_fail_after_stale_heartbeat(self):
        runtime_status._last_alive_ts = time.time() - 60
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()):
            diag = diagnostics.build_diagnostics()
        self.assertEqual(diag["local_agent"]["status"], "FAIL")

    def test_local_agent_evidence_has_alive_and_cpu_percent_keys(self):
        runtime_status.mark_alive()
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()):
            diag = diagnostics.build_diagnostics()
        self.assertIn("alive", diag["local_agent"])
        self.assertIn("cpu_percent", diag["local_agent"])
        self.assertIs(diag["local_agent"]["alive"], True)

    def test_local_agent_alive_false_before_first_iteration(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()):
            diag = diagnostics.build_diagnostics()
        self.assertIs(diag["local_agent"]["alive"], False)

    def test_network_ok_when_internet_reachable(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics.communication.internet_ok", return_value=True), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()):
            diag = diagnostics.build_diagnostics()
        self.assertEqual(diag["network"]["status"], "OK")

    def test_network_fail_when_no_internet_and_no_vpn(self):
        with patch("diagnostics.communication.get_comm_state", return_value="DISCONNECTED"), \
             patch("diagnostics.communication.internet_ok", return_value=False), \
             patch("diagnostics.communication.vpn_ok", return_value=False), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()):
            diag = diagnostics.build_diagnostics()
        self.assertEqual(diag["network"]["status"], "FAIL")

    def test_every_locally_computed_component_has_measured_at(self):
        """
        communication/local_agent/network/authority are computed by this
        module's own _status() helper. The _FLASK_KEYS components are
        copied through verbatim from the vehicle Flask API's response
        (see test_diagnostics_service.py for that side's measured_at
        coverage) -- _flask_diag_ok() here is a hand-built stand-in that
        doesn't carry it, so it's excluded from this assertion.
        """
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()), \
             patch("diagnostics.get_control_authority", return_value="OPERATOR"):
            diag = diagnostics.build_diagnostics()
        for key in ("communication", "local_agent", "network", "authority"):
            self.assertIn("measured_at", diag[key], f"{key} missing measured_at")

    def test_authority_reported_when_reachable(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()), \
             patch("diagnostics.get_control_authority", return_value="LOCAL_AGENT"):
            diag = diagnostics.build_diagnostics()
        self.assertEqual(diag["authority"]["status"], "OK")
        self.assertIn("LOCAL_AGENT", diag["authority"]["message"])

    def test_authority_unknown_not_fake_when_unreachable(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()), \
             patch("diagnostics.get_control_authority", side_effect=RuntimeError("connection refused")):
            diag = diagnostics.build_diagnostics()
        self.assertEqual(diag["authority"]["status"], "UNKNOWN")


class TestBuildDiagnosticsFlaskUnreachable(unittest.TestCase):
    def test_flask_unreachable_reports_unknown_not_fake_values(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", side_effect=RuntimeError("connection refused")):
            diag = diagnostics.build_diagnostics()
        for key in diagnostics._FLASK_KEYS:
            self.assertEqual(diag[key]["status"], "UNKNOWN", f"{key} should be UNKNOWN, not invented")


class TestBuildSystemCheck(unittest.TestCase):
    def setUp(self):
        runtime_status.mark_alive()

    def test_all_healthy_is_pass(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()), \
             patch("diagnostics.get_vehicle_state", return_value={"telemetry": {"lat": 1.0, "lng": 2.0}}), \
             patch("diagnostics.get_control_authority", return_value="OPERATOR"):
            result = diagnostics.build_system_check()
        self.assertEqual(result["overall"], "PASS")
        names = [c["name"] for c in result["checks"]]
        self.assertIn("MAVLink2Rest Reachability", names)
        self.assertIn("Pixhawk Heartbeat", names)
        self.assertIn("Local Agent", names)
        self.assertIn("Telemetry", names)
        self.assertIn("GPS", names)
        self.assertIn("Authority Service", names)

    def test_reports_start_end_and_duration(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()), \
             patch("diagnostics.get_vehicle_state", return_value={"telemetry": {"lat": 1.0, "lng": 2.0}}), \
             patch("diagnostics.get_control_authority", return_value="OPERATOR"):
            result = diagnostics.build_system_check()
        self.assertIn("started_at", result)
        self.assertIn("finished_at", result)
        self.assertIn("duration_seconds", result)
        self.assertGreaterEqual(result["finished_at"], result["started_at"])
        self.assertGreaterEqual(result["duration_seconds"], 0)

    def test_missing_gps_position_fails_telemetry_not_pass(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()), \
             patch("diagnostics.get_vehicle_state", return_value={"telemetry": {"lat": None, "lng": None, "armed": False, "mode": 0}}), \
             patch("diagnostics.get_control_authority", return_value="OPERATOR"):
            result = diagnostics.build_system_check()
        telemetry_check = next(c for c in result["checks"] if c["name"] == "Telemetry")
        self.assertEqual(telemetry_check["status"], "FAIL")

    def test_missing_heartbeat_fails_overall(self):
        flask_diag = _flask_diag_ok()
        flask_diag["pixhawk"] = {"status": "FAIL", "message": "no heartbeat"}
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=flask_diag), \
             patch("diagnostics.get_vehicle_state", return_value={"telemetry": {"lat": 1.0, "lng": 2.0}}), \
             patch("diagnostics.get_control_authority", return_value="OPERATOR"):
            result = diagnostics.build_system_check()
        self.assertEqual(result["overall"], "FAIL")

    def test_authority_service_unreachable_fails_that_check_only(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()), \
             patch("diagnostics.get_vehicle_state", return_value={"telemetry": {"lat": 1.0, "lng": 2.0}}), \
             patch("diagnostics.get_control_authority", side_effect=RuntimeError("unreachable")):
            result = diagnostics.build_system_check()
        authority_check = next(c for c in result["checks"] if c["name"] == "Authority Service")
        self.assertEqual(authority_check["status"], "FAIL")
        self.assertEqual(result["overall"], "FAIL")

    def test_telemetry_error_reported_not_invented(self):
        with patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()), \
             patch("diagnostics.get_vehicle_state", return_value={"telemetry": {"error": "no fix"}}), \
             patch("diagnostics.get_control_authority", return_value="OPERATOR"):
            result = diagnostics.build_system_check()
        telemetry_check = next(c for c in result["checks"] if c["name"] == "Telemetry")
        self.assertEqual(telemetry_check["status"], "FAIL")

    def test_never_calls_a_write_endpoint(self):
        """
        system_check must be read-only: it must never touch a /nav/* write
        endpoint or command_executor. Patch command_executor.call_local_endpoint
        to raise, then confirm build_system_check() still succeeds -- proving
        that code path was never exercised.
        """
        import command_executor

        def _fail_if_called(command_type, timeout=5.0):
            raise AssertionError("system_check must never call a write endpoint")

        with patch.object(command_executor, "call_local_endpoint", _fail_if_called), \
             patch("diagnostics.communication.get_comm_state", return_value="CONNECTED"), \
             patch("diagnostics._fetch_flask_diagnostics", return_value=_flask_diag_ok()), \
             patch("diagnostics.get_vehicle_state", return_value={"telemetry": {"lat": 1.0, "lng": 2.0}}), \
             patch("diagnostics.get_control_authority", return_value="OPERATOR"):
            result = diagnostics.build_system_check()
        self.assertEqual(result["overall"], "PASS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
