"""Backend tests for the command-result protocol/queue-semantics investigation
triggered by a live incident: a SET_HOME command stuck SENT while Scout's Local Agent
repeatedly (re)polled it, its own attempt to report a terminal failure never landing.

Root cause found in THIS codebase (the Operator Station side; Scout's own timestamp bug
is out of scope, fixed separately): `POST /agent/command_result` — the endpoint the
deployed Scout actually posts results to — answered every single-item request with HTTP
200 `ok:true`, even when the id was unknown or the status/schema was invalid. A Scout
that inspects the HTTP status (not just the body) to decide whether to stop retrying
would see "200 OK" and never notice `applied:false`, so a mismatched id/status field
would leave the command SENT forever, redelivered by `GET /agent/commands` on every poll
within its TTL, indistinguishable from Scout never having reported anything at all. This
pins the fixed contract: a single result now gets an honest 404 (unknown id) or 400
(invalid status/missing id), matching the id-in-path endpoint's existing behavior. A
batch/backlog flush (2+ items) is deliberately unchanged — always 2xx, per-item detail —
so one bad id in a backlog never fails the whole flush.

Also pins the SET_HOME creation contract: the canonical command is `{"mode":
"current_position"}` (Scout picks and verifies its own fix) — a browser-supplied lat/lng
is never authoritative and survives only as audit metadata under `requested_position`.

Run from operator-scripts/:  python -m unittest tests.test_command_result_contract
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2

SUCCESS_RESULT = {
    "accepted": True, "verified": True,
    "requested_position": {"latitude": 56.70000, "longitude": 13.00000},
    "home_position": {"latitude": 56.700001, "longitude": 13.000001, "altitude": 12.0},
    "verification_distance_m": 1.4,
    "ack_result": "MAV_RESULT_ACCEPTED", "error": None,
}


class ContractTestBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"

    def queue_set_home(self, lat=56.6634934, lng=12.8814627):
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "SET_HOME",
            "params": {"lat": lat, "lng": lng}, "confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["command"]["id"]

    def claim(self, usv_id="usv-2"):
        return self.client.get(f"/agent/commands?usv_id={usv_id}").json()["commands"]


class TestSetHomeCanonicalContract(ContractTestBase):
    """Requirement: the canonical SET_HOME command is mode:"current_position"; a
    browser-derived lat/lng is never treated as the authoritative target."""

    def test_created_command_carries_mode_current_position(self):
        cid = self.queue_set_home()
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["params"]["mode"], "current_position")

    def test_browser_lat_lng_survives_only_as_non_authoritative_audit_metadata(self):
        cid = self.queue_set_home(lat=56.6634934, lng=12.8814627)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["params"]["requested_position"],
                         {"lat": 56.6634934, "lng": 12.8814627})
        # The only key besides the audit metadata is the canonical mode — no bare
        # top-level lat/lng that a careless reader could mistake for the target.
        self.assertEqual(set(cmd["params"]), {"mode", "requested_position"})

    def test_no_lat_lng_supplied_still_yields_canonical_mode(self):
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "SET_HOME", "params": {}, "confirm": True})
        cmd = r.json()["command"]
        self.assertEqual(cmd["params"], {"mode": "current_position"})

    def test_scout_sees_the_canonical_params_on_delivery(self):
        """The exact production shape: Scout polls GET /agent/commands and must see
        mode:"current_position" as what it's asked to do, not raw operator coordinates."""
        self.queue_set_home()
        delivered = self.claim()[0]
        self.assertEqual(delivered["params"]["mode"], "current_position")


class TestCommandResultSingleItemHonestStatus(ContractTestBase):
    """POST /agent/command_result with ONE result must return an honest HTTP status —
    never a 200 that hides a result which did not actually apply."""

    def _claimed_set_home(self):
        cid = self.queue_set_home()
        self.claim()
        return cid

    def test_failed_result_is_terminal_and_acknowledged_200(self):
        cid = self._claimed_set_home()
        r = self.client.post("/agent/command_result",
                             json={"command_id": cid, "status": "failed",
                                   "reason": "unexpected local agent error"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["applied"])
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "FAILED")
        self.assertIsNotNone(cmd["completed_at"])
        self.assertEqual(cmd["reason"], "unexpected local agent error")

    def test_rejected_result_is_terminal_and_acknowledged_200(self):
        cid = self._claimed_set_home()
        r = self.client.post("/agent/command_result",
                             json={"command_id": cid, "status": "rejected",
                                   "reason": "no GPS fix"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(main.commands_by_id[cid]["status"], "REJECTED")
        self.assertIsNotNone(main.commands_by_id[cid]["completed_at"])

    def test_successful_set_home_result_is_terminal_and_acknowledged_200(self):
        cid = self._claimed_set_home()
        r = self.client.post("/agent/command_result",
                             json={"command_id": cid, "status": "executed",
                                   "result": SUCCESS_RESULT})
        self.assertEqual(r.status_code, 200, r.text)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "EXECUTED")
        self.assertEqual(cmd["home_result"], "verified")

    def test_terminal_result_removes_the_command_from_pending(self):
        cid = self._claimed_set_home()
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "failed", "reason": "x"})
        self.assertEqual(self.claim(), [], "a terminal command must never be redelivered")

    def test_duplicate_terminal_result_is_idempotent(self):
        cid = self._claimed_set_home()
        first = self.client.post("/agent/command_result",
                                 json={"command_id": cid, "status": "failed", "reason": "x"})
        self.assertTrue(first.json()["applied"])
        second = self.client.post("/agent/command_result",
                                  json={"command_id": cid, "status": "failed", "reason": "y"})
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["applied"], "a replay must not re-apply or flip the reason")
        self.assertEqual(main.commands_by_id[cid]["reason"], "x")

    def test_unknown_command_id_is_a_clear_404_not_a_silent_200(self):
        """THE BUG: this used to return 200 ok:true, applied:false — Scout inspecting
        only the status code would believe its result was accepted."""
        r = self.client.post("/agent/command_result",
                             json={"command_id": "does-not-exist", "status": "failed"})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertFalse(r.json()["ok"])
        self.assertFalse(r.json()["applied"])
        self.assertFalse(r.json()["found"])

    def test_invalid_status_is_a_clear_400_not_a_silent_200(self):
        """THE BUG: an unrecognized status string used to be silently swallowed as 200
        ok:true — the command stayed SENT forever with no signal anything was wrong."""
        cid = self._claimed_set_home()
        r = self.client.post("/agent/command_result",
                             json={"command_id": cid, "status": "kaboom"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertFalse(r.json()["ok"])
        self.assertFalse(r.json()["applied"])
        # ...and the command is provably left exactly as it was — still SENT, still
        # redelivered — which is the honest state a 400 is telling Scout about.
        self.assertEqual(main.commands_by_id[cid]["status"], "SENT")
        self.assertEqual(self.claim(), [main.agent_command_view(main.commands_by_id[cid])])

    def test_missing_command_id_is_a_clear_400(self):
        r = self.client.post("/agent/command_result", json={"status": "failed"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertFalse(r.json()["ok"])

    def test_garbage_body_is_a_clear_400(self):
        r = self.client.post("/agent/command_result", json={"nonsense": True})
        self.assertEqual(r.status_code, 400, r.text)


class TestCommandResultBatchStillAlways2xx(ContractTestBase):
    """A flushed backlog (2+ items) is a deliberately different contract: it must stay
    2xx so one bad/unknown id never fails the whole flush — only the single-item path
    (the common case, and the one the incident hit) gained honest per-call status codes."""

    def test_batch_with_one_unknown_id_is_still_200_with_per_item_detail(self):
        cid = self._queue_and_claim()
        r = self.client.post("/agent/command_result", json={"results": [
            {"command_id": cid, "status": "failed", "reason": "x"},
            {"command_id": "unknown-id", "status": "failed"},
        ]})
        self.assertEqual(r.status_code, 200, r.text)
        results = r.json()["results"]
        self.assertTrue(results[0]["applied"])
        self.assertFalse(results[1]["found"])

    def _queue_and_claim(self):
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "SET_HOME",
            "params": {"lat": 56.7, "lng": 13.0}, "confirm": True})
        cid = r.json()["command"]["id"]
        self.client.get(f"/agent/commands?usv_id=usv-2")
        return cid


class TestRedeliveryIsExplicitlyBoundedByTTL(ContractTestBase):
    """Requirement: a claimed SENT command must not be handed out as fresh-forever work
    unless the bound is explicit. Here the bound IS explicit and documented: at-least-once
    redelivery of a non-terminal command continues only until COMMAND_TTL_S elapses (see
    agent_commands()'s docstring) or a terminal result arrives — never indefinitely."""

    def test_sent_command_stops_being_redelivered_once_past_its_ttl(self):
        from datetime import datetime, timedelta, timezone
        cid = self.queue_set_home()
        self.assertEqual([c["command_id"] for c in self.claim()], [cid])
        self.assertEqual(main.commands_by_id[cid]["status"], "SENT")

        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        main.commands_by_id[cid]["expires_at"] = past.isoformat()

        self.assertEqual(self.claim(), [], "past its TTL, a SENT command must stop being redelivered")
        self.assertEqual(main.commands_by_id[cid]["status"], "EXPIRED")


if __name__ == "__main__":
    unittest.main()
