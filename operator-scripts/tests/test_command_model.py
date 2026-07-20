"""Backend tests for the stabilized command lifecycle: normalized source, the type-
agnostic `verification` block + `lifecycle` array, and the MISSION_UPLOAD / MISSION_CLEAR
read-back-verified workflow. Run:  python -m unittest tests.test_command_model

These pin the parts of the operator command model the frontend depends on:
  • source is SERVER-owned on POST /api/commands — always OPERATOR, never client-settable —
    and is forwarded to Scout in the agent-facing view.
  • every command carries a normalized verification.outcome; EXECUTED + verified:false is
    reported as a verification FAILURE, never a success.
  • a plain mode command with no verification reported stays a plain EXECUTED success.
  • MISSION_UPLOAD (mission-contract-v1) is verified only by a matching read-back —
    accepted + verified + route waypoint count (N) + Pixhawk item count (N+1, Home
    included) + route content hash — never by transport success alone; a mismatch /
    rejection / timeout is a failure.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import mission_contract  # noqa: E402
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
    """`source` is SERVER-owned on the browser-facing endpoint. It used to be taken from
    the request body, which meant any caller could mint a record attributing its own
    command to the autonomy — and the provenance trail the thesis's authority analysis
    rests on would not survive that. Spoofing is covered in depth in
    tests/test_mission_contract.py TestCommandSourceIsServerOwned."""

    def test_default_source_is_operator(self):
        cmd = self.create("SET_MODE_LOITER").json()["command"]
        self.assertEqual(cmd["source"], "OPERATOR")

    def test_body_supplied_source_is_ignored(self):
        cmd = self.create("SET_MODE_LOITER", source="MISSION_AGENT").json()["command"]
        self.assertEqual(cmd["source"], "OPERATOR")

    def test_source_is_forwarded_to_scout_in_agent_command_view(self):
        self.create("SET_MODE_AUTO")
        cmd = self.client.get(f"/agent/commands?usv_id=usv-{VID}").json()["commands"][0]
        self.assertEqual(cmd["source"], "OPERATOR")

    def test_normalize_source_still_serves_trusted_backend_callers(self):
        # The normalizer is unchanged — only the browser endpoint stopped consulting the
        # request body. A trusted internal path can still author an autonomy record.
        self.assertEqual(main.normalize_source("MISSION_AGENT"), "MISSION_AGENT")
        self.assertEqual(main.normalize_source("local_agent"), "LOCAL_AGENT")


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
    """MISSION_UPLOAD through the real queue, under mission-contract-v1: the request is a
    ROUTE (no seq-0 Home), and the backend derives expected_route_waypoint_count = N /
    expected_pixhawk_item_count = N+1. Contract validation itself is covered in
    tests/test_mission_contract.py; these pin the lifecycle around it."""

    # Three ROUTE waypoints → 3 route / 4 Pixhawk items after Scout prepends Home.
    GOOD = {"contract_version": "mission-contract-v1", "waypoints": [
        {"latitude": 56.70, "longitude": 13.00, "loiter_time_s": 0},
        {"latitude": 56.71, "longitude": 13.01, "loiter_time_s": 0},
        {"latitude": 56.72, "longitude": 13.02, "loiter_time_s": 0},
    ]}

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

    def test_backend_derives_both_expected_counts_from_the_route(self):
        params = self.create("MISSION_UPLOAD", confirm=True,
                             params=self.GOOD).json()["command"]["params"]
        self.assertEqual(params["expected_route_waypoint_count"], 3)
        self.assertEqual(params["expected_pixhawk_item_count"], 4)   # + Scout's Home

    def test_verified_upload_matches_both_counts_and_the_route_hash(self):
        # The observed hash is the one the backend itself computed for this route — i.e.
        # a Scout that read back exactly what was sent. All three axes must agree.
        cmd = self.executed("MISSION_UPLOAD", {
            "accepted": True, "uploaded": True, "verified": True,
            "observed_route_waypoint_count": 3, "observed_pixhawk_item_count": 4,
            "observed_route_content_hash": mission_contract.route_content_hash(
                self.GOOD["waypoints"])},
            confirm=True, params=self.GOOD)
        self.assertEqual(cmd["mission_result"], "verified")
        self.assertEqual(cmd["verification"]["verified"], True)
        self.assertEqual(cmd["verification"]["outcome"], "VERIFIED")

    def test_route_count_mismatch_is_a_failure_not_a_success(self):
        cmd = self.executed("MISSION_UPLOAD", {
            "accepted": True, "verified": True,
            "observed_route_waypoint_count": 2, "observed_pixhawk_item_count": 3},
            confirm=True, params=self.GOOD)
        self.assertEqual(cmd["status"], "EXECUTED")           # transport worked...
        self.assertEqual(cmd["mission_result"], "failed")     # ...upload did not match
        self.assertEqual(cmd["verification"]["outcome"], "FAILED")
        self.assertIn("expected 3", cmd["reason"])

    def test_pixhawk_item_count_mismatch_is_a_failure(self):
        # Correct route, but Home missing from the flight controller: 3 items, not 4.
        cmd = self.executed("MISSION_UPLOAD", {
            "accepted": True, "verified": True,
            "observed_route_waypoint_count": 3, "observed_pixhawk_item_count": 3},
            confirm=True, params=self.GOOD)
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertIn("expected 4", cmd["reason"])

    def test_route_content_hash_mismatch_is_a_failure(self):
        cid = self.create("MISSION_UPLOAD", confirm=True, params=self.GOOD).json()["command"]["id"]
        # Simulate the day Scout's canonicalization lands: an expected route hash exists.
        main.commands_by_id[cid]["params"]["expected_route_content_hash"] = "sha256:aaa"
        self.client.get(f"/agent/commands?usv_id=usv-{VID}")
        self.client.post("/agent/command_result", json={"command_id": cid, "status": "executed", "result": {
            "accepted": True, "verified": True,
            "observed_route_waypoint_count": 3, "observed_pixhawk_item_count": 4,
            "observed_route_content_hash": "sha256:bbb"}})
        cmd = main.commands_by_id[cid]
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

    def test_mission_clear_is_queueable(self):
        # Scout ships POST /agent/clear_mission with a result contract carrying the
        # independent empty read-back a clear is judged by.
        # Full coverage in tests/test_mission_contract.py TestMissionClear.
        r = self.create("MISSION_CLEAR", confirm=True, params={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(main.commands), 1)


if __name__ == "__main__":
    unittest.main()
