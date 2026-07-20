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


class RoverModeNormalizationTests(unittest.TestCase):
    """The canonical ArduPilot Rover custom_mode normalizer + comparison — the ONE mapping
    command verification, event rendering and the live-mode display all share. Regression
    guard for the field-test bug where a numeric custom_mode 11 was compared against the
    string 'RTL' and produced the false 'Pixhawk reported mode 11, not RTL.'"""

    def test_numeric_custom_modes_map_to_canonical_names(self):
        self.assertEqual(main.normalize_rover_mode(5), "LOITER")
        self.assertEqual(main.normalize_rover_mode(10), "AUTO")
        self.assertEqual(main.normalize_rover_mode(11), "RTL")

    def test_numeric_string_custom_modes_map_to_canonical_names(self):
        self.assertEqual(main.normalize_rover_mode("5"), "LOITER")
        self.assertEqual(main.normalize_rover_mode("10"), "AUTO")
        self.assertEqual(main.normalize_rover_mode("11"), "RTL")

    def test_canonical_names_pass_through_case_insensitively(self):
        for name in ("LOITER", "AUTO", "RTL", "MANUAL"):
            self.assertEqual(main.normalize_rover_mode(name), name)
            self.assertEqual(main.normalize_rover_mode(name.lower()), name)

    def test_unknown_and_empty_values_do_not_invent_a_mode(self):
        for bad in (None, "", "   ", 99, "99", "NOT_A_MODE", True, False):
            self.assertIsNone(main.normalize_rover_mode(bad))

    def test_smart_rtl_is_not_rtl(self):
        # 12 is SMART_RTL — a distinct recovery mode that must NOT be canonicalized to RTL.
        self.assertEqual(main.normalize_rover_mode(12), "SMART_RTL")
        self.assertFalse(main._mode_is_rtl(12))
        self.assertTrue(main._mode_is_rtl(11))


class ModeVerifyMatchTests(unittest.TestCase):
    """verify_mode_match compares canonical expected vs observed, distinguishing a genuine
    mismatch (a different known mode) from an unrecognised representation, so a normalization
    gap can never masquerade as a vehicle rejection."""

    def test_expected_rtl_observed_numeric_11_verifies(self):
        self.assertEqual(main.verify_mode_match("RTL", 11), main.MODE_VERIFY_VERIFIED)

    def test_expected_rtl_observed_string_11_verifies(self):
        self.assertEqual(main.verify_mode_match("RTL", "11"), main.MODE_VERIFY_VERIFIED)

    def test_expected_auto_observed_numeric_10_verifies(self):
        self.assertEqual(main.verify_mode_match("AUTO", 10), main.MODE_VERIFY_VERIFIED)

    def test_expected_loiter_observed_numeric_5_verifies(self):
        self.assertEqual(main.verify_mode_match("LOITER", 5), main.MODE_VERIFY_VERIFIED)

    def test_canonical_name_strings_still_verify(self):
        self.assertEqual(main.verify_mode_match("RTL", "RTL"), main.MODE_VERIFY_VERIFIED)
        self.assertEqual(main.verify_mode_match("AUTO", "AUTO"), main.MODE_VERIFY_VERIFIED)

    def test_genuinely_different_known_modes_fail(self):
        # Both sides resolve to a KNOWN mode and they differ — a real, reportable mismatch.
        self.assertEqual(main.verify_mode_match("RTL", "MANUAL"), main.MODE_VERIFY_FAILED)
        self.assertEqual(main.verify_mode_match("RTL", 0), main.MODE_VERIFY_FAILED)

    def test_unknown_observed_is_representation_gap_not_a_rejection(self):
        # An unrecognised observed value is UNKNOWN_MODE_REPRESENTATION, never FAILED — the
        # operator must not read it as a vehicle rejection.
        self.assertEqual(main.verify_mode_match("RTL", 99), main.MODE_VERIFY_UNKNOWN)
        self.assertEqual(main.verify_mode_match("RTL", "wat"), main.MODE_VERIFY_UNKNOWN)

    def test_no_observed_value_is_unverified_not_failed(self):
        self.assertEqual(main.verify_mode_match("RTL", None), main.MODE_VERIFY_UNVERIFIED)
        self.assertEqual(main.verify_mode_match("RTL", ""), main.MODE_VERIFY_UNVERIFIED)


class RtlNumericModeVerificationTests(unittest.TestCase):
    """End-to-end (through _annotate_rtl_result + build_command_verification): the field-test
    case where Scout reports an EXECUTED+verified RTL whose observed_mode is the numeric
    custom_mode 11. It must be VERIFIED, never FAILED — Scout's verified=true authority is
    not overturned by a mere representation difference (11 vs 'RTL')."""

    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()
        main.comms_state_by_id[VID] = "CONNECTED"

    def _executed_rtl(self, result):
        cid = self.client.post("/api/commands",
                               json={"vehicle_id": VID, "type": "RTL"}).json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "executed", "result": result})
        return main.commands_by_id[cid]

    def test_observed_numeric_11_is_verified_not_failed(self):
        cmd = self._executed_rtl({
            "accepted": True, "verified": True, "requested_mode": "RTL",
            "previous_mode": "MANUAL", "observed_mode": 11, "error": None})
        self.assertEqual(cmd["rtl_result"], "confirmed")
        self.assertTrue(cmd["verification"]["verified"])
        self.assertEqual(cmd["verification"]["outcome"], "VERIFIED")
        # Observed renders canonically as RTL, but the raw custom_mode is retained.
        self.assertEqual(cmd["verification"]["observed"], "RTL")
        self.assertEqual(cmd["observed_raw"], 11)
        self.assertEqual(cmd["verification"]["observed_raw"], 11)

    def test_observed_string_11_is_verified_not_failed(self):
        cmd = self._executed_rtl({
            "accepted": True, "verified": True, "requested_mode": "RTL",
            "observed_mode": "11", "error": None})
        self.assertEqual(cmd["rtl_result"], "confirmed")
        self.assertEqual(cmd["verification"]["outcome"], "VERIFIED")
        self.assertEqual(cmd["observed_raw"], "11")

    def test_scout_verified_true_not_overturned_by_representation_mismatch(self):
        # The exact field-test contradiction: Scout says EXECUTED + verified, observed 11.
        # The operator must NOT overturn that into a failure over 11-vs-"RTL".
        cmd = self._executed_rtl({
            "accepted": True, "verified": True, "requested_mode": "RTL", "observed_mode": 11})
        self.assertNotEqual(cmd["rtl_result"], "failed")
        self.assertIsNot(cmd["verification"]["verified"], False)
        self.assertNotIn("not RTL", cmd.get("reason") or "")

    def test_genuinely_different_mode_still_fails(self):
        # Scout verified=true but the read-back is a DIFFERENT known mode — a real mismatch.
        cmd = self._executed_rtl({
            "accepted": True, "verified": True, "requested_mode": "RTL", "observed_mode": 0})
        self.assertEqual(cmd["rtl_result"], "failed")
        self.assertFalse(cmd["verification"]["verified"])
        self.assertIn("not RTL", cmd["reason"])

    def test_unknown_observed_mode_is_not_a_vehicle_rejection(self):
        # Unknown numeric mode with Scout's verified=true: EXECUTED_UNVERIFIED, NOT failed,
        # NOT a green success — the raw value is preserved for debugging.
        cmd = self._executed_rtl({
            "accepted": True, "verified": True, "requested_mode": "RTL", "observed_mode": 99})
        self.assertEqual(cmd["rtl_result"], "unverified")
        self.assertIsNone(cmd["verification"]["verified"])   # not False → not a FAILED/rejection
        self.assertEqual(cmd["verification"]["outcome"], "EXECUTED")
        self.assertEqual(cmd["observed_raw"], 99)


if __name__ == "__main__":
    unittest.main()
