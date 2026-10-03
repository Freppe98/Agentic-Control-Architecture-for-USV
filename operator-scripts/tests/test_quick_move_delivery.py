"""Backend tests for QUICK_MOVE *delivery* — the half the creation-time gate cannot cover.
Run:  python -m unittest tests.test_quick_move_delivery

THE DEFECT THESE PIN. `create_command` refuses a quick move unless the link is CONNECTED,
which proves the link was current at t=0 and says nothing at all about t=20. Before this
suite existed, a quick move created on a healthy link survived a complete
CONNECTED -> PARTITIONED -> DISCONNECTED -> CONNECTED cycle and was then handed to Scout and
executed, up to QUICK_MOVE_TTL_S after the operator pressed — pointing a vehicle at a spot
chosen from a picture that had since gone away and come back. The creation check plus a TTL
is NOT sufficient; delivery has to be re-decided at the moment of delivery.

Three independent bounds, each tested separately here because each one alone has a hole:
  * LINK CONTINUITY (`link_epoch`) — any transition since creation refuses delivery. This is
    the only one that catches the sub-second race where Scout's poll lands between the
    outage and the 1 Hz comms monitor noticing it.
  * INVALIDATION ON LINK LOSS — the move is terminated the moment the link leaves CONNECTED,
    rather than left for the TTL (which would still allow delivery once the link returned).
  * CLAIM WINDOW — an unclaimed move older than QUICK_MOVE_CLAIM_WINDOW_S is stale intent
    even on a link that never wobbled.

Also covered: at-least-once redelivery keeps a stable command id and is NOT mistaken for a
late first delivery; supersession; and the Stop (LOITER) races in both directions.
"""
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import main  # noqa: E402
import quick_move_fixture as fx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VID = 2
TARGET = {"lat": 56.70050, "lng": 13.00380}


class DeliveryBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()
        main.quick_move_state_by_id.clear()
        main._last_quick_move_state.clear()
        main.comms_state_by_id[VID] = "CONNECTED"
        main.comms_epoch_by_id.clear()
        main.last_known_telemetry.pop(VID, None)
        main.last_known_groups.pop(VID, None)
        main.current_vehicle_state.pop(VID, None)
        main.latest_msg_ts_by_id.pop(VID, None)
        self._real_auth = main.read_control_authority
        main.read_control_authority = lambda vid: {
            "ok": True, "vehicle_id": vid, "available": True, "reachable": True,
            "authority": "OPERATOR"}
        # Capability + armed telemetry, so the creation gate is satisfied.
        self.client.post("/agent/status", json=fx.envelope(
            fx.block(cap=fx.capability(), active_status=None), vid=VID))
        main.comms_state_by_id[VID] = "CONNECTED"

    def tearDown(self):
        main.read_control_authority = self._real_auth

    def issue(self):
        r = self.client.post("/api/commands", json={
            "vehicle_id": VID, "type": "QUICK_MOVE", "params": {"target": TARGET}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["command"]

    def poll(self):
        """One Scout command poll. Returns the QUICK_MOVE command ids it was handed."""
        body = self.client.get("/agent/commands?usv_id=usv-2").json()
        return [c["command_id"] for c in body["commands"] if c["command_type"] == "QUICK_MOVE"]

    def transition(self, state, age=20.0):
        main.record_comms_state(VID, state, datetime.now(timezone.utc), age)


# ── The reported defect ────────────────────────────────────────────────────────────────
class LinkInterruptionTests(DeliveryBase):
    def test_a_brief_disconnect_and_reconnect_never_delivers_the_stale_move(self):
        """THE regression. Created on a healthy link, a full outage cycle in between, Scout
        polls well inside the 45 s TTL — and must get nothing."""
        cmd = self.issue()
        self.transition("PARTITIONED")
        self.transition("DISCONNECTED", 35.0)
        self.transition("CONNECTED", 0.5)
        self.assertEqual(self.poll(), [], "a stale quick move must never reach Scout")
        self.assertEqual(main.commands_by_id[cmd["id"]]["status"], "EXPIRED")

    def test_it_is_invalidated_the_moment_the_link_degrades_not_at_the_TTL(self):
        """Waiting for the TTL would leave a 45 s window in which a returning link delivers
        it. The move is dead as soon as the link stops being current."""
        cmd = self.issue()
        self.transition("PARTITIONED")
        rec = main.commands_by_id[cmd["id"]]
        self.assertEqual(rec["status"], "EXPIRED")
        self.assertIn("PARTITIONED", rec["reason"])
        self.assertIn("invalidated", rec["reason"].lower())

    def test_even_a_PARTITIONED_blip_counts_as_an_interruption(self):
        """PARTITIONED is >15 s of silence. For "go to that spot now" that is an outage,
        not a hiccup."""
        self.issue()
        self.transition("PARTITIONED")
        self.transition("CONNECTED", 0.5)
        self.assertEqual(self.poll(), [])

    def test_the_epoch_catches_a_poll_the_1Hz_monitor_has_not_seen_yet(self):
        """The sub-second race: the link dropped and returned, but the comms monitor has not
        run, so comm_state still reads CONNECTED and nothing has been invalidated. Only the
        epoch stamped at creation can refuse this one."""
        cmd = self.issue()
        # Simulate transitions the monitor recorded WITHOUT the invalidation hook having
        # fired for this command (bump the epoch directly, leave the record non-terminal).
        main.comms_epoch_by_id[VID] = main.comms_epoch(VID) + 2
        main.comms_state_by_id[VID] = "CONNECTED"
        self.assertEqual(main.commands_by_id[cmd["id"]]["status"], "QUEUED")
        self.assertEqual(self.poll(), [], "the epoch mismatch alone must refuse delivery")
        self.assertIn("interrupted", main.commands_by_id[cmd["id"]]["reason"])

    def test_an_uninterrupted_link_delivers_normally(self):
        """The guard must not break the ordinary case."""
        cmd = self.issue()
        self.assertEqual(self.poll(), [cmd["id"]])
        self.assertEqual(main.commands_by_id[cmd["id"]]["status"], "SENT")

    def test_the_operator_card_stops_presenting_an_invalidated_move_as_live(self):
        self.issue()
        self.transition("PARTITIONED")
        row = next(v for v in self.client.get("/api/fleet/status").json() if v["id"] == VID)
        cur = row["quick_move"]["current"]
        self.assertIsNotNone(cur["cancel_requested"])
        self.assertIn("invalidated", cur["cancel_requested"]["reason"].lower())
        # Never CANCELLED: if it HAD reached Scout, only Scout can say the vehicle stood down.
        self.assertIsNone(cur["status"])


# ── Claim window ───────────────────────────────────────────────────────────────────────
class ClaimWindowTests(DeliveryBase):
    def _age_command(self, cmd_id, seconds):
        rec = main.commands_by_id[cmd_id]
        created = datetime.fromisoformat(rec["created_at"]) - timedelta(seconds=seconds)
        rec["created_at"] = created.isoformat()

    def test_an_unclaimed_move_goes_stale_even_on_a_perfect_link(self):
        cmd = self.issue()
        self._age_command(cmd["id"], main.QUICK_MOVE_CLAIM_WINDOW_S + 2)
        self.assertEqual(self.poll(), [])
        self.assertIn("stale", main.commands_by_id[cmd["id"]]["reason"])

    def test_the_claim_window_is_shorter_than_the_ttl_and_both_are_published(self):
        self.assertLess(main.QUICK_MOVE_CLAIM_WINDOW_S, main.QUICK_MOVE_TTL_S)
        caps = self.client.get("/api/commands/capabilities").json()["commands"]["QUICK_MOVE"]
        self.assertEqual(caps["claim_window_s"], main.QUICK_MOVE_CLAIM_WINDOW_S)
        self.assertEqual(caps["ttl_s"], main.QUICK_MOVE_TTL_S)
        self.assertTrue(caps["requires_continuous_link"])
        self.assertFalse(caps["offline_queueable"])

    def test_the_claim_window_does_not_cut_off_an_ALREADY_CLAIMED_move(self):
        """Redelivery of a claimed command is the at-least-once mechanism working, not a
        late first delivery — the TTL governs from the claim onward."""
        cmd = self.issue()
        self.assertEqual(self.poll(), [cmd["id"]])          # claimed
        self._age_command(cmd["id"], main.QUICK_MOVE_CLAIM_WINDOW_S + 10)
        self.assertEqual(self.poll(), [cmd["id"]], "a claimed move keeps being redelivered")


# ── Retry identity ─────────────────────────────────────────────────────────────────────
class RetryTests(DeliveryBase):
    def test_redelivery_reuses_the_command_id_and_the_claim_time(self):
        cmd = self.issue()
        self.assertEqual(self.poll(), [cmd["id"]])
        claimed_at = main.commands_by_id[cmd["id"]]["claimed_at"]
        for _ in range(3):
            self.assertEqual(self.poll(), [cmd["id"]])
        self.assertEqual(main.commands_by_id[cmd["id"]]["claimed_at"], claimed_at,
                         "claimed_at is the FIRST claim, never rewritten by a redelivery")

    def test_redelivery_stops_once_a_result_arrives(self):
        cmd = self.issue()
        self.poll()
        self.client.post("/agent/command_result", json={
            "command_id": cmd["id"], "status": "EXECUTED",
            "result": {"verified": True, "observed_mode": "GUIDED"}})
        self.assertEqual(self.poll(), [])

    def test_a_link_blip_stops_redelivery_of_an_already_claimed_move(self):
        """Scout has it but never acked. Without this, the at-least-once retry would keep
        re-offering it across the outage — the very thing the fix exists to prevent."""
        cmd = self.issue()
        self.assertEqual(self.poll(), [cmd["id"]])
        self.transition("PARTITIONED")
        self.transition("CONNECTED", 0.5)
        self.assertEqual(self.poll(), [])
        self.assertEqual(main.commands_by_id[cmd["id"]]["status"], "EXPIRED")


# ── Supersession ───────────────────────────────────────────────────────────────────────
class SupersessionTests(DeliveryBase):
    def test_only_the_newest_move_is_ever_deliverable(self):
        first = self.issue()
        self.poll()
        second = self.issue()
        self.assertEqual(main.commands_by_id[first["id"]]["status"], "EXPIRED")
        self.assertEqual(self.poll(), [second["id"]])

    def test_supersession_survives_a_poll_in_between(self):
        first = self.issue()
        self.assertEqual(self.poll(), [first["id"]])
        second = self.issue()
        for _ in range(2):
            self.assertEqual(self.poll(), [second["id"]],
                             "the retired move must never reappear on a later poll")

    def test_a_superseded_move_is_not_reported_as_a_link_failure(self):
        """Distinct reasons matter: 'replaced' and 'link interrupted' send the operator to
        completely different conclusions about what happened."""
        first = self.issue()
        self.issue()
        self.assertIn("replaced", main.commands_by_id[first["id"]]["reason"])


# ── Stop (LOITER) races ────────────────────────────────────────────────────────────────
class StopRaceTests(DeliveryBase):
    def loiter(self):
        return self.client.post("/api/commands",
                                json={"vehicle_id": VID, "type": "SET_MODE_LOITER"})

    def test_stop_before_delivery_means_the_move_is_never_delivered(self):
        """The safety-critical direction: an undelivered move must not fire AFTER the hold."""
        cmd = self.issue()
        self.loiter()
        self.assertEqual(main.commands_by_id[cmd["id"]]["status"], "EXPIRED")
        self.assertEqual(self.poll(), [])

    def test_stop_after_delivery_stops_redelivery_but_claims_nothing_about_the_vehicle(self):
        cmd = self.issue()
        self.assertEqual(self.poll(), [cmd["id"]])
        self.loiter()
        self.assertEqual(self.poll(), [], "no further redelivery once Stop was pressed")
        row = next(v for v in self.client.get("/api/fleet/status").json() if v["id"] == VID)
        cur = row["quick_move"]["current"]
        self.assertIsNotNone(cur["cancel_requested"])
        self.assertIsNone(cur["status"],
                          "a stop REQUEST never becomes a movement outcome by itself")

    def test_the_loiter_itself_is_always_delivered(self):
        """Whatever happens to the quick move, the safety hold must reach Scout. LOITER is
        deliberately NOT subject to the quick-move delivery guard."""
        self.issue()
        self.loiter()
        body = self.client.get("/agent/commands?usv_id=usv-2").json()
        self.assertIn("SET_MODE_LOITER", [c["command_type"] for c in body["commands"]])

    def test_a_loiter_is_still_delivered_after_a_link_blip(self):
        """The quick-move guard must not leak onto other command types: queue-until-contact
        is exactly what a safety hold needs."""
        self.loiter()
        self.transition("PARTITIONED")
        self.transition("CONNECTED", 0.5)
        body = self.client.get("/agent/commands?usv_id=usv-2").json()
        self.assertIn("SET_MODE_LOITER", [c["command_type"] for c in body["commands"]])

    def test_scout_reporting_cancelled_is_what_actually_ends_the_movement(self):
        cmd = self.issue()
        self.poll()
        self.client.post("/agent/status", json=fx.envelope(fx.block(
            cap=fx.capability(), active_status=fx.active(
                cmd["id"], "MOVING", updated_at=time.time(), distance_remaining_m=60.0))))
        self.loiter()
        row = next(v for v in self.client.get("/api/fleet/status").json() if v["id"] == VID)
        self.assertEqual(row["quick_move"]["current"]["status"]["state"], "MOVING",
                         "still moving until Scout says otherwise")
        self.client.post("/agent/status", json=fx.envelope(fx.block(
            cap=fx.capability(), active_status=fx.active(
                cmd["id"], "CANCELLED", updated_at=time.time() + 1,
                reason="Cancelled by an operator LOITER command."))))
        row = next(v for v in self.client.get("/api/fleet/status").json() if v["id"] == VID)
        self.assertEqual(row["quick_move"]["current"]["status"]["state"], "CANCELLED")


# ── The pending/debug view must not be a second way in ─────────────────────────────────
class PendingViewTests(DeliveryBase):
    def test_the_operator_pending_view_applies_the_same_delivery_rule(self):
        cmd = self.issue()
        self.transition("PARTITIONED")
        self.transition("CONNECTED", 0.5)
        pending = self.client.get(f"/api/commands/pending/{VID}").json()["pending"]
        self.assertEqual([c for c in pending if c["type"] == "QUICK_MOVE"], [],
                         "the debug view must not hand out what /agent/commands refuses")
        self.assertEqual(main.commands_by_id[cmd["id"]]["status"], "EXPIRED")


if __name__ == "__main__":
    unittest.main()
