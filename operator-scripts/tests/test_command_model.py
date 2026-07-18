"""Backend tests for the stabilized command lifecycle: normalized source, the type-
agnostic `verification` block + `lifecycle` array, and the MISSION_UPLOAD / MISSION_CLEAR
read-back-verified workflow. Run:  python -m unittest tests.test_command_model

These pin the parts of the operator command model the frontend depends on:
  • source is normalized (OPERATOR / LOCAL_AGENT / MISSION_AGENT) and forwarded to Scout.
  • every command carries a normalized verification.outcome; EXECUTED + verified:false is
    reported as a verification FAILURE, never a success.
  • a plain mode command with no verification reported stays a plain EXECUTED success.
  • MISSION_UPLOAD is verified only by a matching read-back (accepted+verified+count/hash),
    never by transport success alone; a mismatch / rejection / timeout is a failure.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VID = 2


class CommandModelBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()
        main.comms_state_by_id[VID] = "CONNECTED"

    def create(self, ctype, **extra):
        body = {"vehicle_id": VID, "type": ctype, **extra}
        return self.client.post("/api/commands", json=body)

    def executed(self, ctype, result, status="executed", confirm=False, params=None):
        body = {"vehicle_id": VID, "type": ctype, "confirm": confirm}
        if params is not None:
            body["params"] = params
        cid = self.client.post("/api/commands", json=body).json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")   # claim
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": status, "result": result})
        return main.commands_by_id[cid]


class SourceTests(CommandModelBase):
    def test_default_source_is_operator(self):
        cmd = self.create("SET_MODE_LOITER").json()["command"]
        self.assertEqual(cmd["source"], "OPERATOR")

    def test_explicit_mission_agent_source_is_preserved(self):
        cmd = self.create("SET_MODE_LOITER", source="MISSION_AGENT").json()["command"]
        self.assertEqual(cmd["source"], "MISSION_AGENT")

    def test_local_agent_source_normalizes(self):
        cmd = self.create("SET_MODE_LOITER", source="local_agent").json()["command"]
        self.assertEqual(cmd["source"], "LOCAL_AGENT")

    def test_source_is_forwarded_to_scout_in_agent_command_view(self):
        self.create("SET_MODE_AUTO", source="MISSION_AGENT")
        cmd = self.client.get(f"/agent/commands?usv_id=usv-{VID}").json()["commands"][0]
        self.assertEqual(cmd["source"], "MISSION_AGENT")


class NormalizedVerificationTests(CommandModelBase):
    def test_queued_command_has_pending_verification_and_lifecycle(self):
        cmd = self.create("SET_MODE_LOITER").json()["command"]
        self.assertEqual(cmd["verification"]["outcome"], "PENDING")
        self.assertIsNone(cmd["verification"]["verified"])
        self.assertEqual(cmd["lifecycle"][0]["stage"], "QUEUED")

    def test_plain_mode_command_executed_is_plain_success(self):
        cmd = self.executed("SET_MODE_LOITER", {"mode": "LOITER"})
        self.assertEqual(cmd["status"], "EXECUTED")
        self.assertIsNone(cmd["verification"]["verified"])       # no separate verification
        self.assertEqual(cmd["verification"]["outcome"], "EXECUTED")

    def test_mode_command_with_scout_verified_true_is_verified(self):
        cmd = self.executed("SET_MODE_AUTO", {
            "verified": True, "requested_mode": "AUTO", "observed_mode": "AUTO"})
        self.assertEqual(cmd["verification"]["verified"], True)
        self.assertEqual(cmd["verification"]["outcome"], "VERIFIED")
        self.assertEqual(cmd["verification"]["expected"], "AUTO")
        self.assertEqual(cmd["verification"]["observed"], "AUTO")

    def test_executed_with_verified_false_renders_as_failed(self):
        cmd = self.executed("SET_MODE_AUTO", {
            "verified": False, "requested_mode": "AUTO", "observed_mode": "MANUAL",
            "error": {"code": "MODE_UNCHANGED", "message": "Mode did not change"}})
        self.assertEqual(cmd["status"], "EXECUTED")               # transport worked...
        self.assertEqual(cmd["verification"]["verified"], False)  # ...action did not
        self.assertEqual(cmd["verification"]["outcome"], "FAILED")
        self.assertEqual(cmd["verification"]["reason"], "Mode did not change")
        self.assertEqual(cmd["verification"]["observed"], "MANUAL")

    def test_lifecycle_merges_scout_stages_and_terminal(self):
        cmd = self.executed("SET_MODE_AUTO", {
            "verified": True, "observed_mode": "AUTO",
            "lifecycle": [{"stage": "ACCEPTED", "ts": "t-a"}, {"stage": "EXECUTING", "ts": "t-e"}]})
        stages = [s["stage"] for s in cmd["lifecycle"]]
        self.assertIn("QUEUED", stages)
        self.assertIn("SENT", stages)
        self.assertIn("EXECUTING", stages)
        self.assertEqual(stages[-1], "EXECUTED")   # terminal stage last

    def test_structured_error_is_retained_on_the_record(self):
        cmd = self.executed("SET_MODE_AUTO", {
            "verified": False, "error": {"code": "NO_ACK", "message": "No ack"}})
        self.assertEqual(cmd["error"], {"code": "NO_ACK", "message": "No ack"})


class MissionUploadTests(CommandModelBase):
    GOOD = {"expected_count": 3, "expected_hash": "wpm1:abc123",
            "waypoints": [{"seq": 0, "lat": 56.7, "lng": 13.0}]}

    def test_mission_upload_is_a_recognized_confirm_required_type(self):
        self.assertIn("MISSION_UPLOAD", main.COMMAND_TYPES)
        self.assertIn("MISSION_UPLOAD", main.CONFIRM_REQUIRED_TYPES)

    def test_upload_requires_confirmation(self):
        r = self.create("MISSION_UPLOAD", params=self.GOOD)
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["needs_confirmation"])

    def test_accepted_is_pending_not_verified(self):
        cid = self.create("MISSION_UPLOAD", confirm=True, params=self.GOOD).json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")
        self.client.post("/agent/command_result", json={"command_id": cid, "status": "accepted"})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "ACCEPTED")
        self.assertEqual(cmd["verification"]["outcome"], "PENDING")

    def test_verified_upload_matches_count_and_hash(self):
        cmd = self.executed("MISSION_UPLOAD", {
            "accepted": True, "verified": True,
            "observed_count": 3, "observed_hash": "wpm1:abc123"},
            confirm=True, params=self.GOOD)
        self.assertEqual(cmd["mission_result"], "verified")
        self.assertEqual(cmd["verification"]["verified"], True)
        self.assertEqual(cmd["verification"]["outcome"], "VERIFIED")

    def test_count_mismatch_is_a_failure_not_a_success(self):
        cmd = self.executed("MISSION_UPLOAD", {
            "accepted": True, "verified": True,
            "observed_count": 2, "observed_hash": "wpm1:abc123"},
            confirm=True, params=self.GOOD)
        self.assertEqual(cmd["status"], "EXECUTED")           # transport worked...
        self.assertEqual(cmd["mission_result"], "failed")     # ...upload did not match
        self.assertEqual(cmd["verification"]["outcome"], "FAILED")
        self.assertIn("expected 3", cmd["reason"])

    def test_hash_mismatch_is_a_failure(self):
        cmd = self.executed("MISSION_UPLOAD", {
            "accepted": True, "verified": True,
            "observed_count": 3, "observed_hash": "wpm1:DIFFERENT"},
            confirm=True, params=self.GOOD)
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertIn("does not match", cmd["reason"])

    def test_not_accepted_is_a_failure_even_if_status_executed(self):
        cmd = self.executed("MISSION_UPLOAD", {
            "accepted": False, "verified": False,
            "error": {"code": "BUSY", "message": "Vehicle busy uploading"}},
            confirm=True, params=self.GOOD)
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertEqual(cmd["reason"], "Vehicle busy uploading")

    def test_transport_executed_with_no_verification_is_not_success(self):
        # An EXECUTED with no accepted/verified flags must NOT read as a verified upload.
        cmd = self.executed("MISSION_UPLOAD", {"status": "uploaded"}, confirm=True, params=self.GOOD)
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertEqual(cmd["verification"]["outcome"], "FAILED")

    def test_rejected_upload_is_terminal_failure(self):
        cid = self.create("MISSION_UPLOAD", confirm=True, params=self.GOOD).json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "rejected", "reason": "Not in a writable mode"})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["verification"]["outcome"], "REJECTED")
        self.assertEqual(cmd["reason"], "Not in a writable mode")

    def test_timeout_result_aliases_to_a_failure(self):
        cid = self.create("MISSION_UPLOAD", confirm=True, params=self.GOOD).json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")
        self.client.post("/agent/command_result", json={"command_id": cid, "status": "timeout"})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "FAILED")
        self.assertEqual(cmd["verification"]["outcome"], "FAILED")

    def test_mission_clear_verified_when_readback_empty(self):
        cmd = self.executed("MISSION_CLEAR", {
            "accepted": True, "verified": True, "observed_count": 0}, confirm=True, params={})
        self.assertEqual(cmd["mission_result"], "verified")
        self.assertEqual(cmd["verification"]["outcome"], "VERIFIED")

    def test_mission_clear_failed_when_waypoints_remain(self):
        cmd = self.executed("MISSION_CLEAR", {
            "accepted": True, "verified": True, "observed_count": 2}, confirm=True, params={})
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertIn("still holds 2", cmd["reason"])


if __name__ == "__main__":
    unittest.main()
