"""Backend tests for the Pixhawk mode commands, focused on the LOITER-as-primary-safety
change. Run:  python -m unittest tests.test_mode_commands  (no pytest needed).

Confirms the command ROUTING is unchanged by the UI reshuffle: LOITER routes as
SET_MODE_LOITER with no forced confirmation (quick access), and SET_MODE_HOLD is still
accepted for backend compatibility.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VID = 2


class ModeCommandTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.comms_state_by_id[VID] = "CONNECTED"  # fresh link so nothing needs confirm

    def create(self, ctype, **extra):
        return self.client.post("/api/commands", json={"vehicle_id": VID, "type": ctype, **extra})

    def test_loiter_is_a_valid_command_type(self):
        self.assertIn("SET_MODE_LOITER", main.COMMAND_TYPES)

    def test_loiter_routes_as_set_mode_loiter_and_queues(self):
        r = self.create("SET_MODE_LOITER")
        self.assertEqual(r.status_code, 200)
        cmd = r.json()["command"]
        self.assertEqual(cmd["type"], "SET_MODE_LOITER")
        self.assertEqual(cmd["status"], "QUEUED")

    def test_loiter_needs_no_confirmation_quick_access(self):
        # LOITER is a safety hold — it must be quickly accessible (not confirm-gated).
        self.assertNotIn("SET_MODE_LOITER", main.CONFIRM_REQUIRED_TYPES)
        r = self.create("SET_MODE_LOITER")  # no confirm flag
        self.assertEqual(r.status_code, 200)

    def test_hold_still_supported_for_compatibility(self):
        self.assertIn("SET_MODE_HOLD", main.COMMAND_TYPES)  # kept, not removed
        r = self.create("SET_MODE_HOLD")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["command"]["type"], "SET_MODE_HOLD")

    def test_loiter_and_hold_are_distinct_types(self):
        a = self.create("SET_MODE_LOITER").json()["command"]["type"]
        b = self.create("SET_MODE_HOLD").json()["command"]["type"]
        self.assertNotEqual(a, b)


class RtlResultClassificationTests(unittest.TestCase):
    """RTL's outer status EXECUTED means only that the Local Agent completed the attempt.
    _annotate_rtl_result classifies the nested Scout result into rtl_result
    'confirmed'/'failed' so the UI never shows a green success on transport alone. Mirrors
    the SET_HOME home_result contract."""

    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()
        main.comms_state_by_id[VID] = "CONNECTED"

    def _executed_rtl(self, result):
        """Queue → claim → report EXECUTED with the given nested result; return the record."""
        cid = self.client.post("/api/commands",
                               json={"vehicle_id": VID, "type": "RTL"}).json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")   # claim
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "executed", "result": result})
        return main.commands_by_id[cid]

    def test_executed_verified_observed_rtl_is_confirmed(self):
        cmd = self._executed_rtl({
            "accepted": True, "verified": True, "requested_mode": "RTL",
            "previous_mode": "MANUAL", "observed_mode": "RTL",
            "ack_result": "MAV_RESULT_ACCEPTED", "error": None})
        self.assertEqual(cmd["status"], "EXECUTED")
        self.assertEqual(cmd["rtl_result"], "confirmed")
        # The full structured result is preserved unchanged.
        self.assertEqual(cmd["result"]["observed_mode"], "RTL")

    def test_executed_but_verified_false_is_failed(self):
        cmd = self._executed_rtl({
            "accepted": True, "verified": False, "requested_mode": "RTL",
            "previous_mode": "MANUAL", "observed_mode": None,
            "error": {"code": "VERIFY_TIMEOUT", "message": "RTL verification timed out"}})
        self.assertEqual(cmd["status"], "EXECUTED")   # transport worked...
        self.assertEqual(cmd["rtl_result"], "failed")  # ...RTL did not
        self.assertEqual(cmd["reason"], "RTL verification timed out")

    def test_executed_but_observed_manual_is_failed(self):
        cmd = self._executed_rtl({
            "accepted": True, "verified": False, "requested_mode": "RTL",
            "previous_mode": "MANUAL", "observed_mode": "MANUAL", "error": None})
        self.assertEqual(cmd["rtl_result"], "failed")
        self.assertEqual(cmd["reason"], "Pixhawk remained in MANUAL")

    def test_executed_reverted_from_rtl_is_failed_with_revert_reason(self):
        cmd = self._executed_rtl({
            "accepted": True, "verified": False, "requested_mode": "RTL",
            "previous_mode": "RTL", "observed_mode": "MANUAL", "error": None})
        self.assertEqual(cmd["rtl_result"], "failed")
        self.assertEqual(cmd["reason"], "Mode reverted from RTL to MANUAL")

    def test_rejected_rtl_is_terminal_failure_with_reason(self):
        cid = self.client.post("/api/commands",
                               json={"vehicle_id": VID, "type": "RTL"}).json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "rejected",
                               "reason": "MAVLink rejected mode change"})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "REJECTED")
        self.assertEqual(cmd["reason"], "MAVLink rejected mode change")
        self.assertNotEqual(cmd.get("rtl_result"), "confirmed")

    def test_failed_rtl_is_terminal_failure(self):
        cid = self.client.post("/api/commands",
                               json={"vehicle_id": VID, "type": "RTL"}).json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "failed", "reason": "mavlink link down"})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "FAILED")
        self.assertEqual(cmd["reason"], "mavlink link down")

    def test_error_message_is_surfaced_verbatim(self):
        cmd = self._executed_rtl({
            "accepted": False, "verified": False, "requested_mode": "RTL",
            "observed_mode": None,
            "error": {"code": "NO_ACK", "message": "No ack from the Pixhawk within 5 s"}})
        self.assertEqual(cmd["rtl_result"], "failed")
        self.assertEqual(cmd["reason"], "No ack from the Pixhawk within 5 s")

    def test_legacy_returning_home_string_is_not_verified_success(self):
        """The old optimistic {"status":"Returning home"} shape (a route that returned 200
        with no accepted/verified flags) must be classified failed, never confirmed."""
        cmd = self._executed_rtl({"status": "Returning home"})
        self.assertEqual(cmd["status"], "EXECUTED")
        self.assertEqual(cmd["rtl_result"], "failed")
        self.assertEqual(cmd["reason"], "MAVLink rejected the RTL mode change.")


class LoiterResultClassificationTests(unittest.TestCase):
    """LOITER is a plain mode command: EXECUTED IS success (Scout reporting the mode
    change is the confirmation), and it never gets a home/rtl verification annotation or
    a Home-verification gate."""

    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()
        main.comms_state_by_id[VID] = "CONNECTED"

    def _claim_loiter(self):
        cid = self.client.post("/api/commands",
                               json={"vehicle_id": VID, "type": "SET_MODE_LOITER"}).json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")
        return cid

    def test_executed_loiter_is_plain_success(self):
        cid = self._claim_loiter()
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "executed", "result": {"mode": "LOITER"}})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "EXECUTED")
        # No per-type verification annotation — EXECUTED already means confirmed.
        self.assertIsNone(cmd.get("rtl_result"))
        self.assertIsNone(cmd.get("home_result"))
        self.assertEqual(cmd["result"], {"mode": "LOITER"})

    def test_rejected_loiter_surfaces_scout_reason_verbatim(self):
        cid = self._claim_loiter()
        reason = "unsupported command_type: SET_MODE_LOITER"
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "rejected", "reason": reason})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "REJECTED")
        self.assertEqual(cmd["reason"], reason)

    def test_loiter_never_requires_home_verification(self):
        # No home_status is reported for VID here, yet LOITER queues and executes fine —
        # the backend queue does not gate LOITER on Home (the UI interlock explicitly
        # exempts it; see lib/home.js commandGate).
        self.assertNotIn("SET_MODE_LOITER", main.CONFIRM_REQUIRED_TYPES)
        cid = self._claim_loiter()
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "executed", "result": {"mode": "LOITER"}})
        self.assertEqual(main.commands_by_id[cid]["status"], "EXECUTED")


if __name__ == "__main__":
    unittest.main()
