"""Backend tests for the Local Agent-facing command API — the endpoints the DEPLOYED
Scout actually speaks:

    GET  /agent/commands?usv_id=usv-2   (delivery + claim)
    POST /agent/command_result          (terminal result)

Run from operator-scripts/:  python -m unittest tests.test_agent_commands  (no pytest).

The integration bug these pin: the Scout's local_mission_agent polls
`GET /agent/commands?usv_id=usv-2`, which did not exist — the backend answered
`{"detail":"Not Found"}`, so no command was ever claimed and every SET_HOME sat QUEUED
until its TTL. `/api/commands/{id}` worked, but that is the operator/UI view, not the
Agent delivery path.

Delivery contract pinned here:
  - `usv-2` (and `2`) map to internal vehicle id 2.
  - The FIRST fetch is the claim: QUEUED → SENT, claimed_at stamped.
  - Delivery is AT-LEAST-ONCE: a non-terminal command is redelivered on every poll until
    a terminal result arrives or it expires, so a delivery response dropped by an
    intermittent link never loses the command. The Scout dedups by command_id (it records
    processed ids and rejects redeliveries without re-executing), so a repeat is harmless.
  - A redelivery is inert: claimed_at keeps the ORIGINAL claim time and no second "sent to
    Scout" event is logged.
  - Terminal/expired commands are never delivered; the TTL is what bounds redelivery.
  - Only Scout-facing fields are exposed (command_id/command_type/params/expires_at) —
    never the internal record's operator-side bookkeeping.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2

# A fully successful Scout Set Home result, per the contract in verification/set-home.md.
SUCCESS_RESULT = {
    "accepted": True, "verified": True,
    "requested_position": {"latitude": 56.70000, "longitude": 13.00000},
    "home_position": {"latitude": 56.700001, "longitude": 13.000001, "altitude": 12.0},
    "verification_distance_m": 1.4,
    "ack_result": "MAV_RESULT_ACCEPTED", "error": None,
}


class AgentCommandsBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()      # event counts below must not see other tests' events
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"

    def _sent_events(self):
        """How many 'sent to Scout' claim events the log holds (the claim is logged once,
        never per redelivery)."""
        return len([e for e in main.event_log
                    if e.get("type") == "command" and "sent to" in str(e.get("message", ""))])

    def queue(self, ctype="SET_HOME", params=None, vid=SCOUT_VID):
        body = {"vehicle_id": vid, "type": ctype,
                "params": params if params is not None else {"lat": 56.7, "lng": 13.0},
                "confirm": True}
        r = self.client.post("/api/commands", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["command"]["id"]

    def poll(self, usv_id="usv-2"):
        return self.client.get(f"/agent/commands?usv_id={usv_id}")


class TestAgentCommandDelivery(AgentCommandsBase):
    def test_queued_set_home_is_delivered_and_claimed(self):
        """The headline path: queued SET_HOME → GET → returned, SENT, claimed_at set."""
        cid = self.queue("SET_HOME")
        self.assertEqual(main.commands_by_id[cid]["status"], "QUEUED")
        self.assertIsNone(main.commands_by_id[cid]["claimed_at"])

        r = self.poll()
        self.assertEqual(r.status_code, 200, r.text)
        cmds = r.json()["commands"]
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["command_id"], cid)
        self.assertEqual(cmds[0]["command_type"], "SET_HOME")
        self.assertEqual(cmds[0]["params"], {"lat": 56.7, "lng": 13.0})
        self.assertTrue(cmds[0]["expires_at"])

        # The fetch is the claim.
        self.assertEqual(main.commands_by_id[cid]["status"], "SENT")
        self.assertIsNotNone(main.commands_by_id[cid]["claimed_at"])

    def test_delivered_shape_is_exactly_the_scout_contract(self):
        """Only the Agent's own fields — no internal/operator bookkeeping leaks. `source`
        is forwarded (OPERATOR/LOCAL_AGENT/MISSION_AGENT) per the stabilized contract."""
        self.queue("SET_HOME")
        cmd = self.poll().json()["commands"][0]
        self.assertEqual(set(cmd), {"command_id", "command_type", "source", "params", "expires_at"})
        self.assertEqual(cmd["source"], "OPERATOR")
        for leaked in ("created_by", "requested_comm_state", "warning", "vehicle", "status"):
            self.assertNotIn(leaked, cmd)

    def test_params_is_always_an_object_never_null(self):
        self.queue("SET_MODE_LOITER", params={})
        self.assertEqual(self.poll().json()["commands"][0]["params"], {})

    def test_second_poll_redelivers_the_same_sent_command(self):
        """At-least-once: a SENT command with no terminal result is handed out again, so
        a delivery response lost on an intermittent link never loses the command. The
        Scout dedups by command_id and does not re-execute."""
        cid = self.queue("SET_HOME")
        first = self.poll().json()["commands"]
        self.assertEqual([c["command_id"] for c in first], [cid])
        self.assertEqual(main.commands_by_id[cid]["status"], "SENT")

        second = self.poll().json()["commands"]
        self.assertEqual([c["command_id"] for c in second], [cid],
                         "a SENT command with no result must be redelivered")
        self.assertEqual(second, first, "a redelivery is the identical record")

        # ...and keeps being redelivered until a result or expiry.
        self.assertEqual([c["command_id"] for c in self.poll().json()["commands"]], [cid])

    def test_redelivery_preserves_the_original_claim_timestamp(self):
        """claimed_at is when the Agent FIRST took the command, not when it last saw it."""
        cid = self.queue("SET_HOME")
        self.poll()
        first_claim = main.commands_by_id[cid]["claimed_at"]
        self.assertIsNotNone(first_claim)
        self.poll()
        self.poll()
        self.assertEqual(main.commands_by_id[cid]["claimed_at"], first_claim,
                         "a redelivery must not rewrite claimed_at")

    def test_redelivery_does_not_re_log_a_sent_event(self):
        """A polling Agent must not flood the event log — the claim is logged once."""
        self.queue("SET_HOME")
        self.poll()
        self.assertEqual(self._sent_events(), 1)
        self.poll()
        self.poll()
        self.assertEqual(self._sent_events(), 1,
                         "redelivery must not emit another 'sent to Scout' event")

    def test_an_accepted_command_is_still_redelivered_until_terminal(self):
        """ACCEPTED is an intermediate ack, not a terminal result."""
        cid = self.queue("SET_HOME")
        self.poll()
        self.client.post("/agent/command_result", json={"command_id": cid, "status": "accepted"})
        self.assertEqual(main.commands_by_id[cid]["status"], "ACCEPTED")
        self.assertEqual([c["command_id"] for c in self.poll().json()["commands"]], [cid],
                         "an acknowledged-but-unfinished command must still be redelivered")

    def test_no_queued_commands_returns_empty_list(self):
        r = self.poll()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["commands"], [])

    def test_terminal_command_is_never_delivered(self):
        cid = self.queue("SET_HOME")
        self.poll()
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "executed", "result": SUCCESS_RESULT})
        self.assertEqual(main.commands_by_id[cid]["status"], "EXECUTED")
        self.assertEqual(self.poll().json()["commands"], [],
                         "a terminal command must never be redelivered")

    def test_expired_queued_command_is_not_delivered(self):
        """A command past its TTL expires instead of being handed over late."""
        cid = self.queue("SET_HOME")
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        main.commands_by_id[cid]["expires_at"] = past.isoformat()
        r = self.poll()
        self.assertEqual(r.json()["commands"], [])
        self.assertEqual(main.commands_by_id[cid]["status"], "EXPIRED")
        self.assertIsNone(main.commands_by_id[cid]["claimed_at"],
                          "an expired command must never be claimed")

    def test_expired_SENT_command_stops_being_redelivered(self):
        """The TTL is what bounds at-least-once: redelivery of a claimed-but-unanswered
        command continues only until it expires, never forever."""
        cid = self.queue("SET_HOME")
        self.assertEqual([c["command_id"] for c in self.poll().json()["commands"]], [cid])
        self.assertEqual(main.commands_by_id[cid]["status"], "SENT")   # claimed, no result

        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        main.commands_by_id[cid]["expires_at"] = past.isoformat()

        self.assertEqual(self.poll().json()["commands"], [],
                         "an expired SENT command must not be redelivered")
        self.assertEqual(main.commands_by_id[cid]["status"], "EXPIRED")

    def test_only_the_addressed_vehicle_s_commands_are_delivered(self):
        other = next(u["id"] for u in main.FLEET_TEMPLATE if u["id"] != SCOUT_VID)
        mine = self.queue("SET_HOME", vid=SCOUT_VID)
        self.queue("SET_MODE_LOITER", params={}, vid=other)
        cmds = self.poll("usv-2").json()["commands"]
        self.assertEqual([c["command_id"] for c in cmds], [mine])

    def test_commands_are_delivered_in_queue_order(self):
        first = self.queue("SET_MODE_LOITER", params={})
        second = self.queue("SET_HOME")
        cmds = self.poll().json()["commands"]
        self.assertEqual([c["command_id"] for c in cmds], [first, second])


class TestUsvIdMapping(AgentCommandsBase):
    def test_usv_2_maps_to_internal_vehicle_id_2(self):
        cid = self.queue("SET_HOME")
        r = self.poll("usv-2")
        self.assertEqual(r.json()["vehicle_id"], 2)
        self.assertEqual(r.json()["usv_id"], "usv-2")
        self.assertEqual(r.json()["commands"][0]["command_id"], cid)

    def test_bare_numeric_and_uppercase_forms_also_map(self):
        for form in ("2", "USV-2", "Usv-2"):
            main.commands.clear(); main.commands_by_id.clear()
            cid = self.queue("SET_HOME")
            r = self.poll(form)
            self.assertEqual(r.status_code, 200, f"{form}: {r.text}")
            self.assertEqual(r.json()["vehicle_id"], 2, form)
            self.assertEqual(r.json()["commands"][0]["command_id"], cid, form)

    def test_unknown_usv_id_is_a_loud_404_not_an_empty_list(self):
        """A misconfigured USV_ID must never look like 'no work to do' forever."""
        r = self.poll("usv-99")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["error"], "unknown vehicle")
        self.assertEqual(r.json()["usv_id"], "usv-99")

    def test_unparseable_usv_id_is_rejected(self):
        r = self.poll("banana")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["error"], "unknown vehicle")

    def test_missing_usv_id_is_a_400_with_the_expected_form(self):
        r = self.client.get("/agent/commands")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error"], "missing usv_id")
        self.assertIn("usv_id=usv-2", r.json()["expected"])


class TestAgentCommandResult(AgentCommandsBase):
    """POST /agent/command_result — the matching result path (already existed; pinned
    here as part of the round trip the Scout actually performs)."""

    def _claimed(self, ctype="SET_HOME"):
        cid = self.queue(ctype)
        self.poll()
        return cid

    def test_lowercase_executed_with_verified_home(self):
        cid = self._claimed()
        r = self.client.post("/agent/command_result",
                             json={"command_id": cid, "status": "executed", "result": SUCCESS_RESULT})
        self.assertEqual(r.status_code, 200, r.text)
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "EXECUTED")
        self.assertEqual(cmd["home_result"], "verified")
        self.assertIsNotNone(cmd["completed_at"])
        self.assertEqual(cmd["result"], SUCCESS_RESULT, "raw nested result preserved verbatim")

    def test_lowercase_rejected_preserves_reason(self):
        cid = self._claimed("SET_MODE_AUTO")
        reason = "blocked: SET_MODE_AUTO requires OPERATOR control authority"
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "rejected", "reason": reason})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "REJECTED")
        self.assertEqual(cmd["reason"], reason)
        self.assertIsNotNone(cmd["completed_at"])

    def test_lowercase_failed_maps_to_terminal_failed(self):
        cid = self._claimed("SET_MODE_AUTO")
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "failed", "reason": "mavlink timeout"})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "FAILED")
        self.assertEqual(cmd["reason"], "mavlink timeout")

    def test_set_home_executed_but_unverified_is_annotated_failed(self):
        """A bare/incomplete Set Home result is never an optimistic success."""
        cid = self._claimed()
        self.client.post("/agent/command_result", json={
            "command_id": cid, "status": "executed",
            "result": {"accepted": False, "verified": False, "home_position": None,
                       "verification_distance_m": None,
                       "error": {"code": "ACK_TIMEOUT", "message": "No ack from the Pixhawk."}}})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "EXECUTED")      # transport worked...
        self.assertEqual(cmd["home_result"], "failed")   # ...Set Home did not
        self.assertEqual(cmd["reason"], "No ack from the Pixhawk.")

    def test_set_home_out_of_tolerance_readback_is_failed(self):
        cid = self._claimed()
        far = dict(SUCCESS_RESULT, verification_distance_m=main.HOME_VERIFY_TOLERANCE_M + 1)
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "executed", "result": far})
        self.assertEqual(main.commands_by_id[cid]["home_result"], "failed")

    def test_result_on_an_unclaimed_command_still_applies(self):
        """Scout may report without this backend having seen a claim (e.g. restart)."""
        cid = self.queue("SET_HOME")
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "executed", "result": SUCCESS_RESULT})
        self.assertEqual(main.commands_by_id[cid]["status"], "EXECUTED")

    def test_replayed_result_is_idempotent(self):
        cid = self._claimed()
        self.client.post("/agent/command_result",
                         json={"command_id": cid, "status": "executed", "result": SUCCESS_RESULT})
        r = self.client.post("/agent/command_result",
                             json={"command_id": cid, "status": "executed", "result": SUCCESS_RESULT})
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.json()["applied"], False, "a replayed result must not re-apply")
        self.assertEqual(main.commands_by_id[cid]["home_result"], "verified")


class TestFullRoundTrip(AgentCommandsBase):
    def test_scout_round_trip_queue_claim_redeliver_result_stop(self):
        """The exact lifecycle the deployed Scout performs, end to end."""
        cid = self.queue("SET_HOME")

        # 1. Scout polls and claims.
        cmds = self.poll().json()["commands"]
        self.assertEqual(cmds[0]["command_id"], cid)
        self.assertEqual(main.commands_by_id[cid]["status"], "SENT")

        # 1b. The delivery response is lost on the link — the Scout never saw it. The
        # next poll must hand the command over again rather than lose it.
        self.assertEqual([c["command_id"] for c in self.poll().json()["commands"]], [cid])

        # 2. Scout executes and reports.
        self.client.post("/agent/command_result", json={
            "command_id": cid, "status": "executed", "result": SUCCESS_RESULT})
        cmd = main.commands_by_id[cid]
        self.assertEqual(cmd["status"], "EXECUTED")
        self.assertEqual(cmd["home_result"], "verified")

        # 3. Never handed out again.
        self.assertEqual(self.poll().json()["commands"], [])

        # 4. The operator/UI view agrees (this is what the Map page polls).
        ui = self.client.get(f"/api/commands/{SCOUT_VID}").json()["commands"]
        self.assertEqual(ui[0]["id"], cid)
        self.assertEqual(ui[0]["status"], "EXECUTED")
        self.assertEqual(ui[0]["home_result"], "verified")


if __name__ == "__main__":
    unittest.main()
