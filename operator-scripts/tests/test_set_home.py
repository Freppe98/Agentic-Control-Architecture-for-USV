"""Backend tests for SET_HOME as a normal queued command AND for the Home-verification
state model.

Run from operator-scripts/:  python -m unittest tests.test_set_home  (no pytest needed).

Two independent things are tested here — keep them independent, they must never merge:

1. SET_HOME's TRANSPORT. It flows through the exact same command infrastructure as
   AUTO/RTL/LOITER/ARM/DISARM/PAUSE/RESUME: POST /api/commands (type SET_HOME) ->
   QUEUED -> GET /api/commands/pending/{id} (the Local Agent's claim, SENT) ->
   POST /agent/command_result or POST /api/commands/{id}/result (EXECUTED/FAILED/
   REJECTED). No direct HTTP call to Scout is ever made for it (TestSetHomeIsAQueuedCommand).

2. Home VERIFICATION STATE. Command-protocol status EXECUTED means only "the Local
   Agent successfully called Scout Flask" — it is NOT proof Set Home succeeded. The
   command's own nested Scout result (result.accepted / result.verified /
   result.home_position / result.verification_distance_m / result.error) only drives an
   immediate click-feedback classification (main._annotate_set_home_result ->
   cmd["home_result"]) — never a permanent record (TestSetHomeResultClassification).
   The PERMANENT verified/not-verified state comes SOLELY from Scout's own
   continuously-reported payload.agent.home_status (main.home_block), exercised here by
   constructing fleet-payload dicts directly — it is never reconstructed or latched from
   a command result (TestHomeBlockScoutOwnedTruth).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2  # Scout's canonical id; its route lives in VEHICLE_API_BASE (SAR-001 is 3)

# A fully successful Scout Set Home result, per the real contract.
SUCCESS_RESULT = {
    "accepted": True, "verified": True, "command_id": "irrelevant-here",
    "requested_position": {"latitude": 56.70000, "longitude": 13.00000},
    "home_position": {"latitude": 56.700001, "longitude": 13.000001, "altitude": 12.0},
    "verification_distance_m": 1.4,
    "ack_result": "MAV_RESULT_ACCEPTED", "error": None,
}


class SetHomeTestBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        # Isolate per-test global state.
        main.commands.clear()
        main.commands_by_id.clear()
        main.last_known_agent.clear()
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"  # fresh link by default

    def create(self, vid=SCOUT_VID, lat=56.70000, lng=13.00000, confirm=True, **extra):
        body = {"vehicle_id": vid, "type": "SET_HOME", "params": {"lat": lat, "lng": lng}, **extra}
        if confirm is not None:
            body["confirm"] = confirm
        return self.client.post("/api/commands", json=body)

    def _queued_id(self, **kw):
        return self.create(**kw).json()["command"]["id"]

    def report(self, command_id, status, result=None, reason=None, path="body"):
        """Report a result the way the Local Agent would — either via the id-in-path
        endpoint or the id-in-body endpoint (both share process_command_result)."""
        if path == "path":
            return self.client.post(f"/api/commands/{command_id}/result",
                                     json={"status": status, "result": result, "reason": reason})
        return self.client.post("/agent/command_result",
                                 json={"command_id": command_id, "status": status,
                                       "result": result, "reason": reason})


class TestSetHomeIsAQueuedCommand(SetHomeTestBase):
    def test_set_home_is_a_recognized_command_type(self):
        self.assertIn("SET_HOME", main.COMMAND_TYPES)

    def test_requires_confirmation_like_arm_disarm(self):
        self.assertIn("SET_HOME", main.CONFIRM_REQUIRED_TYPES)
        r = self.create(confirm=None)  # no confirm flag at all
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["needs_confirmation"])

    def test_confirmed_request_queues_through_the_normal_pipeline(self):
        r = self.create()
        self.assertEqual(r.status_code, 200)
        cmd = r.json()["command"]
        self.assertEqual(cmd["type"], "SET_HOME")
        self.assertEqual(cmd["status"], "QUEUED")
        self.assertIn("id", cmd)  # same uuid id / dedup key every command type gets
        self.assertIn("expires_at", cmd)
        self.assertEqual(main.commands[-1]["id"], cmd["id"])

    def test_unknown_vehicle_404(self):
        r = self.create(vid=999)
        self.assertEqual(r.status_code, 404)

    def test_J_no_synchronous_scout_call_is_made(self):
        # Swap main.requests for a stub that fails any call; queuing SET_HOME (or
        # reporting its result) must never touch it — that is the whole point of the
        # refactor, and it must not silently regress back to a direct proxy.
        class Boom:
            def post(self, *a, **k): raise AssertionError("SET_HOME must not call Scout directly")
            def get(self, *a, **k): raise AssertionError("SET_HOME must not call Scout directly")
        orig = main.requests
        main.requests = Boom()
        try:
            r = self.create()
            self.assertEqual(r.status_code, 200)
            cid = r.json()["command"]["id"]
            self.report(cid, "EXECUTED", result=SUCCESS_RESULT)
            self.assertEqual(main.commands_by_id[cid]["home_result"], "verified")
        finally:
            main.requests = orig

    def test_disconnected_link_needs_confirmation_same_as_any_command(self):
        main.comms_state_by_id[SCOUT_VID] = "DISCONNECTED"
        r = self.create(confirm=False)
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["needs_confirmation"])

    def test_J_old_dedicated_endpoint_is_gone(self):
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/commands/set-home",
                              json={"lat": 56.7, "lng": 13.0, "confirm": True})
        # No FastAPI route matches any more — it falls through to the "/" static mount,
        # which answers 404 (no such file) or 405 (POST unsupported there); either is
        # proof the dedicated route itself no longer exists.
        self.assertIn(r.status_code, (404, 405))


class TestSetHomeResultClassification(SetHomeTestBase):
    """main._annotate_set_home_result: what a SET_HOME command's OWN nested Scout
    result means for immediate feedback. Never touches the permanent Home record —
    see TestHomeBlockScoutOwnedTruth for that."""

    def test_pending_fetch_claims_the_command_like_any_other(self):
        cid = self._queued_id()
        r = self.client.get(f"/api/commands/pending/{SCOUT_VID}")
        pending_ids = [c["id"] for c in r.json()["pending"]]
        self.assertIn(cid, pending_ids)
        self.assertEqual(main.commands_by_id[cid]["status"], "SENT")

    def test_E_success_with_home_position_and_verification_distance_m_is_recognized(self):
        cid = self._queued_id()
        r = self.report(cid, "EXECUTED", result=SUCCESS_RESULT)
        self.assertEqual(r.status_code, 200)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "EXECUTED")
        self.assertEqual(cmd["home_result"], "verified")

    def test_A_outer_executed_with_verified_false_is_not_a_success(self):
        cid = self._queued_id()
        result = {**SUCCESS_RESULT, "verified": False,
                  "error": {"code": "READBACK_TIMEOUT", "message": "Home did not read back in time."}}
        self.report(cid, "EXECUTED", result=result)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "EXECUTED")       # transport succeeded...
        self.assertEqual(cmd["home_result"], "failed")    # ...Set Home itself did not
        self.assertIn("Home did not read back in time.", cmd["reason"])

    def test_B_outer_executed_with_accepted_false_is_not_a_success(self):
        cid = self._queued_id()
        result = {"accepted": False, "verified": False, "home_position": None,
                  "verification_distance_m": None,
                  "error": {"code": "REJECTED", "message": "Pixhawk rejected the command."}}
        self.report(cid, "EXECUTED", result=result)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["home_result"], "failed")
        self.assertIn("Pixhawk rejected the command.", cmd["reason"])

    def test_C_ack_timeout_error_surfaces_the_real_failure(self):
        cid = self._queued_id()
        result = {"accepted": False, "verified": False, "home_position": None,
                  "verification_distance_m": None, "error": {"code": "ACK_TIMEOUT", "message": None}}
        self.report(cid, "EXECUTED", result=result)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["home_result"], "failed")
        self.assertEqual(cmd["reason"], "ACK_TIMEOUT")  # falls back to the code when no message

    def test_D_missing_home_position_does_not_fall_back_to_requested_coords(self):
        cid = self._queued_id()  # requested (56.70000, 13.00000)
        result = {"accepted": True, "verified": True, "home_position": None,
                  "verification_distance_m": None, "error": None}
        self.report(cid, "EXECUTED", result=result)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["home_result"], "failed")
        self.assertIn("home_position", cmd["reason"])
        # Decisively: the requested params (56.70000, 13.00000) never leak into the
        # command record as if they were Pixhawk's confirmed Home.
        self.assertNotIn("56.7", cmd["reason"])

    def test_out_of_tolerance_readback_is_not_a_success(self):
        cid = self._queued_id()
        result = {"accepted": True, "verified": True,
                  "home_position": {"latitude": 56.71, "longitude": 13.0},
                  "verification_distance_m": 1100.0, "error": None}
        self.report(cid, "EXECUTED", result=result)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["home_result"], "failed")
        self.assertIn("tolerance", cmd["reason"])

    def test_old_field_names_home_and_distance_m_are_not_honored(self):
        # No backward compatibility is deliberately supported for the old (pre-fix)
        # field names — result.home / result.distance_m must be ignored outright.
        cid = self._queued_id()
        result = {"accepted": True, "verified": True,
                  "home": {"lat": 56.7, "lng": 13.0}, "distance_m": 0.2}
        self.report(cid, "EXECUTED", result=result)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["home_result"], "failed")

    def test_I_failed_result_never_gets_a_home_result_classification(self):
        cid = self._queued_id()
        self.report(cid, "FAILED", reason="No valid GPS fix")
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "FAILED")
        self.assertEqual(cmd["reason"], "No valid GPS fix")
        self.assertNotIn("home_result", cmd)  # classification only applies to EXECUTED

    def test_result_is_idempotent_by_command_id(self):
        cid = self._queued_id()
        self.report(cid, "EXECUTED", result=SUCCESS_RESULT)
        r2 = self.report(cid, "FAILED", reason="late duplicate")
        self.assertFalse(r2.json()["applied"])  # already terminal — no-op
        self.assertEqual(main.commands_by_id[cid]["status"], "EXECUTED")  # unchanged
        self.assertEqual(main.commands_by_id[cid]["home_result"], "verified")  # unchanged

    def test_result_reported_via_id_in_path_endpoint_also_works(self):
        cid = self._queued_id()
        r = self.report(cid, "EXECUTED", result=SUCCESS_RESULT, path="path")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(main.commands_by_id[cid]["home_result"], "verified")

    def test_command_appears_in_history_once_terminal(self):
        cid = self._queued_id()
        self.report(cid, "EXECUTED", result=SUCCESS_RESULT)
        r = self.client.get(f"/api/commands/history/{SCOUT_VID}")
        ids = [c["id"] for c in r.json()["commands"]]
        self.assertIn(cid, ids)


class TestHomeBlockScoutOwnedTruth(SetHomeTestBase):
    """main.home_block(): the PERMANENT verified/not-verified state, sourced only from
    payload.agent.home_status — never from a SET_HOME command result."""

    def test_absent_home_status_is_honestly_unavailable(self):
        block = main.home_block(SCOUT_VID, {}, {})
        self.assertFalse(block["available"])
        self.assertFalse(block["verified"])
        self.assertFalse(block["stale"])  # "never reported" is distinct from "stale"
        self.assertIn("does not report Home status", block["reason"])

    def test_verified_true_from_scout_is_reflected_verbatim(self):
        payload = {"agent": {"home_status": {
            "verified": True, "verified_at": "2026-07-15T10:00:00+00:00",
            "verification_method": "READBACK", "verification_distance_m": 1.2,
            "ready_for_auto": True, "ready_for_rtl": True, "reachable": True,
            "home_position": {"latitude": 56.7, "longitude": 13.0, "altitude": 10.0},
            "reason": None,
        }}}
        block = main.home_block(SCOUT_VID, payload, {})
        self.assertTrue(block["verified"])
        self.assertAlmostEqual(block["lat"], 56.7)
        self.assertAlmostEqual(block["lng"], 13.0)
        self.assertEqual(block["verification_distance_m"], 1.2)
        self.assertEqual(block["verification_method"], "READBACK")
        self.assertTrue(block["ready_for_auto"])
        self.assertTrue(block["ready_for_rtl"])
        self.assertFalse(block["stale"])
        self.assertEqual(block["home_position"], payload["agent"]["home_status"]["home_position"])

    def test_F_scout_verified_false_overrides_any_earlier_command_success(self):
        # A SET_HOME command whose own result classifies as "verified"...
        cid = self._queued_id()
        self.report(cid, "EXECUTED", result=SUCCESS_RESULT)
        self.assertEqual(main.commands_by_id[cid]["home_result"], "verified")

        # ...must have ZERO effect on home_block: only Scout's own continuous status
        # decides the permanent state, and here Scout itself says NOT verified.
        payload = {"agent": {"home_status": {
            "verified": False, "home_position": None, "verification_distance_m": None,
            "reason": "Pixhawk HOME not confirmed.",
        }}}
        block = main.home_block(SCOUT_VID, payload, {})
        self.assertFalse(block["verified"])
        self.assertEqual(block["reason"], "Pixhawk HOME not confirmed.")

    def test_G_restart_invalidated_home_status_clears_verified_state(self):
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"
        verified_payload = {"agent": {"home_status": {
            "verified": True, "home_position": {"latitude": 56.7, "longitude": 13.0},
            "verification_distance_m": 0.4,
        }}}
        self.assertTrue(main.home_block(SCOUT_VID, verified_payload, {})["verified"])

        # Scout/Pixhawk restarts — the very next packet reports Home lost. Nothing
        # about the PREVIOUS packet's "verified" may linger.
        restart_payload = {"agent": {"home_status": {
            "verified": False, "home_position": None, "verification_distance_m": None,
            "reason": "Pixhawk restarted — Home is not set.",
        }}}
        block = main.home_block(SCOUT_VID, restart_payload, {})
        self.assertFalse(block["verified"])
        self.assertFalse(block["available"])
        self.assertFalse(block["stale"])  # this is fresh, current truth — not a stale fallback

    def test_H_stale_last_known_home_status_is_never_shown_as_verified(self):
        # Scout previously reported verified:true (captured the way receive_agent_status
        # already captures the agent block for last-known fallback)...
        main.last_known_agent[SCOUT_VID] = {"home_status": {
            "verified": True, "home_position": {"latitude": 56.7, "longitude": 13.0},
            "verification_distance_m": 0.5, "verified_at": "2026-07-15T09:00:00+00:00",
        }}
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"
        # ...but THIS packet carries no agent group at all (Scout stopped reporting it).
        block = main.home_block(SCOUT_VID, {}, {})
        self.assertTrue(block["stale"])
        self.assertFalse(block["verified"])       # never silently trust the cached true
        self.assertIsNone(block["verified_at"])   # suppressed while stale

    def test_H_disconnected_vehicle_treats_a_fresh_verified_packet_as_stale_too(self):
        main.comms_state_by_id[SCOUT_VID] = "DISCONNECTED"
        payload = {"agent": {"home_status": {
            "verified": True, "home_position": {"latitude": 56.7, "longitude": 13.0},
            "verification_distance_m": 0.2,
        }}}
        block = main.home_block(SCOUT_VID, payload, {})
        self.assertTrue(block["stale"])
        self.assertFalse(block["verified"])

    def test_I_a_command_failure_never_touches_scouts_own_truth(self):
        payload = {"agent": {"home_status": {
            "verified": True, "home_position": {"latitude": 56.7, "longitude": 13.0},
            "verification_distance_m": 0.3,
        }}}
        self.assertTrue(main.home_block(SCOUT_VID, payload, {})["verified"])

        cid = self._queued_id()
        self.report(cid, "FAILED", reason="ack timeout")
        self.assertNotIn("home_result", main.commands_by_id[cid])

        # Scout's own truth, read independently, is exactly as it was.
        self.assertTrue(main.home_block(SCOUT_VID, payload, {})["verified"])

    def test_no_permanent_command_latched_home_store_exists(self):
        # Locks in point 7 of the safety review: there is no backend-owned permanent
        # Home-verification store any more — home_block reads Scout's status only.
        self.assertFalse(hasattr(main, "home_verification_by_id"))


if __name__ == "__main__":
    unittest.main()
