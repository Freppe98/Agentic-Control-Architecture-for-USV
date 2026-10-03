"""Scout quick-move-v1 REPORTING-COMPATIBILITY tests — replay Scout-shaped captures through
the REAL operator ingestion endpoint and assert on the resulting FLEET STATE, not on HTTP 200.
Run:  python -m unittest tests.test_scout_quick_move_compat

WHAT THIS PROVES, AND WHAT IT DOES NOT
--------------------------------------
It proves REPORTING compatibility: that this operator build parses, orders, correlates and
publishes what Scout's quick-move publisher emits. It does NOT prove DELIVERY of a real
operator-generated command to a real vehicle, and nothing here is evidence that any vehicle
moved. Those need the bench steps in QUICK_MOVE_HANDOFF.md.

EVIDENCE QUALITY — READ THIS
----------------------------
These replay whatever `*.json` sits in tests/fixtures/scout_quick_move_payloads/. Today that
is a RECONSTRUCTION transcribed from the prose of Scout's own QUICK_MOVE_HANDOFF.md, not a
capture of Scout's output — see that directory's README for why the difference matters and
`test_the_evidence_quality_is_stated_honestly` below, which fails loudly if a reconstruction is
ever allowed to pass itself off as a capture.

HTTP 200 IS NOT SUCCESS. /agent/status returns 200 for almost anything naming a known vehicle;
a refused quick_move block is only ever visible in GET /api/fleet/status's
`quick_move.rejections`. Every assertion below reads the fleet row.

ISOLATION. OPERATOR_RUNTIME_DIR is forced to a temp directory before `main` is imported, so the
production runtime_data/ is unreachable. Scout's fixture names its vehicle `usv-2-fixture`,
which the operator auto-discovers as its OWN vehicle — so none of this can reach, or be
confused with, the real Scout (id 2). That isolation is a property of the identity model, not
something this test arranges, and `test_the_fixture_vehicle_is_isolated_from_the_real_scout`
pins it.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PAYLOAD_DIR = HERE / "fixtures" / "scout_quick_move_payloads"

# Before `main` is imported anywhere in this process (see main._resolve_runtime_dir).
os.environ.setdefault("OPERATOR_RUNTIME_DIR",
                      tempfile.mkdtemp(prefix="scout-quick-move-compat-"))
sys.path.insert(0, str(REPO))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FIXTURE_VID = "usv-2-fixture"
RECONSTRUCTION = PAYLOAD_DIR / "RECONSTRUCTED-from-scout-handoff.json"


def load_scenarios():
    """{name: [envelope, ...]} from whatever is in the payload directory.

    A real Scout capture export is one file per scenario (Scout's own convention); the
    reconstruction is a single file with a `scenarios` map. Both shapes load here so the real
    captures can simply be dropped in and these tests start running against them instead."""
    out = {}
    for path in sorted(PAYLOAD_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("scenarios"), dict):
            out.update(data["scenarios"])
        elif isinstance(data, list):
            out[path.stem] = data
        elif isinstance(data, dict):
            out[path.stem] = [data]
    return out


SCENARIOS = load_scenarios()


class CompatBase(unittest.TestCase):
    """One scenario per test, each from a genuinely clean slate. Scout's scenarios are
    INDEPENDENT of one another — replaying them into shared state would let one scenario's
    accepted session leak into the next and quietly turn a real refusal into a pass."""

    def setUp(self):
        self.assertTrue(SCENARIOS, f"no payloads found in {PAYLOAD_DIR}")
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()
        main.quick_move_state_by_id.clear()
        main._last_quick_move_state.clear()
        main.comms_epoch_by_id.clear()
        for store in (main.current_vehicle_state, main.last_known_telemetry,
                      main.last_known_groups, main.latest_msg_ts_by_id,
                      main.comms_state_by_id, main.last_seen_by_id):
            store.pop(FIXTURE_VID, None)
        self._real_auth = main.read_control_authority
        # The dispatch gate re-reads authority LIVE and there is no Scout here. Stubbing the
        # READ is the narrow thing to stub: capability, armed and link state still have to be
        # genuinely satisfied by the capture itself.
        main.read_control_authority = lambda vid: {
            "ok": True, "vehicle_id": vid, "available": True, "reachable": True,
            "authority": "OPERATOR"}

    def tearDown(self):
        main.read_control_authority = self._real_auth

    # --- replay helpers ---------------------------------------------------------------
    def post(self, env):
        r = self.client.post("/agent/status", json=env)
        self.assertEqual(r.status_code, 200, f"ingestion refused the envelope outright: {r.text}")
        return r

    def row(self, vid=FIXTURE_VID):
        for v in self.client.get("/api/fleet/status").json():
            if v["id"] == vid:
                return v
        self.fail(f"{vid} missing from the fleet after replay")

    def qm(self, vid=FIXTURE_VID):
        return self.row(vid)["quick_move"]

    def captured_command_id(self, envelopes):
        for env in envelopes:
            active = ((env.get("payload") or {}).get("quick_move") or {}).get("active")
            if isinstance(active, dict) and active.get("command_id"):
                return active["command_id"]
        return None

    def replay(self, name, *, correlate=True):
        """Replay one scenario IN ORDER. With `correlate`, the captured command_id is mapped
        onto a real operator-minted one first (Scout's ids come from Scout's run; the operator
        correctly refuses a status for an id it never issued). Returns the minted id or None."""
        envelopes = SCENARIOS[name]
        self.assertTrue(envelopes, f"scenario {name} is empty")
        captured = self.captured_command_id(envelopes)
        minted = None
        if correlate and captured:
            # Capability has to land before a command can be minted, so establish it from the
            # scenario's own first envelope rather than from anything this test invents.
            self.post(envelopes[0])
            main.comms_state_by_id[FIXTURE_VID] = "CONNECTED"
            active = next(((e.get("payload") or {}).get("quick_move") or {}).get("active")
                          for e in envelopes
                          if isinstance(((e.get("payload") or {}).get("quick_move") or {})
                                        .get("active"), dict))
            r = self.client.post("/api/commands", json={
                "vehicle_id": FIXTURE_VID, "type": "QUICK_MOVE",
                "params": {"target": active["target"]}})
            self.assertEqual(r.status_code, 200,
                             f"the operator refused to mint a correlated command: {r.text}")
            minted = r.json()["command"]["id"]
        for env in envelopes:
            env = json.loads(json.dumps(env))      # never mutate the loaded capture
            if minted:
                blk = (env.get("payload") or {}).get("quick_move") or {}
                if isinstance(blk.get("active"), dict):
                    blk["active"]["command_id"] = minted
            self.post(env)
            main.comms_state_by_id[FIXTURE_VID] = "CONNECTED"
        return minted


# ── Evidence quality and isolation ─────────────────────────────────────────────────────
class EvidenceTests(CompatBase):
    def test_the_evidence_quality_is_stated_honestly(self):
        """A reconstruction must never be able to pass itself off as a Scout capture. If the
        reconstruction file is present it MUST carry its warning; when real captures replace
        it, this test simply stops finding it and the suite is running on real evidence."""
        if not RECONSTRUCTION.exists():
            return                      # real captures are in place — nothing to police
        doc = json.loads(RECONSTRUCTION.read_text(encoding="utf-8"))
        self.assertIn("_WARNING", doc)
        self.assertIn("RECONSTRUCTION", doc["_WARNING"])
        self.assertIn("NOT A SCOUT CAPTURE", doc["_WARNING"])
        readme = (PAYLOAD_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("are NOT here yet", readme)

    def test_the_fixture_vehicle_is_isolated_from_the_real_scout(self):
        """Scout suffixes its fixture's usv_id with `-fixture`. The operator resolves that to
        its OWN vehicle, so simulated quick-move state can never land on the real Scout's row
        — and the simulated name travels with it, so a screen showing it cannot be mistaken
        for a live vehicle."""
        self.replay("03_full_success")
        self.assertNotEqual(main.canonical_id(FIXTURE_VID), 2)
        row = self.row()
        self.assertIn("FIXTURE", (row.get("name") or "").upper())
        real = next((v for v in self.client.get("/api/fleet/status").json() if v["id"] == 2), None)
        self.assertIsNotNone(real)
        self.assertIsNone(real["quick_move"]["current"],
                          "fixture traffic must never appear on the real Scout's row")
        self.assertFalse(real["quick_move"]["capability"]["supported"])


# ── 1. Capability absent/disabled keeps the action unavailable ─────────────────────────
class CapabilityTests(CompatBase):
    def test_capability_only_heartbeat_advertises_without_a_movement(self):
        self.replay("01_capability_only_heartbeat", correlate=False)
        blk = self.qm()
        self.assertTrue(blk["capability"]["supported"])
        self.assertIsNone(blk["current"], "a heartbeat must not invent a movement")
        self.assertEqual(blk["rejections"]["count"], 0, blk["rejections"])

    def test_a_disabled_capability_carries_scouts_reason_and_blocks_the_action(self):
        self.replay("02_disabled_carries_reason", correlate=False)
        cap = self.qm()["capability"]
        self.assertFalse(cap["supported"])
        self.assertIn("disabled", cap["reason"].lower())
        main.comms_state_by_id[FIXTURE_VID] = "CONNECTED"
        r = self.client.post("/api/commands", json={
            "vehicle_id": FIXTURE_VID, "type": "QUICK_MOVE",
            "params": {"target": {"lat": 56.7005, "lng": 13.0038}}})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "not_supported")
        self.assertIn("disabled", r.json()["message"].lower())

    def test_a_vehicle_that_reports_but_never_advertises_cannot_be_commanded(self):
        """Reporting telemetry is not advertising a capability. The vehicle below is live,
        connected and in the fleet — and still refused, because it has never said it can do
        this."""
        env = json.loads(json.dumps(SCENARIOS["01_capability_only_heartbeat"][0]))
        env["payload"].pop("quick_move")
        self.post(env)
        main.comms_state_by_id[FIXTURE_VID] = "CONNECTED"
        r = self.client.post("/api/commands", json={
            "vehicle_id": FIXTURE_VID, "type": "QUICK_MOVE",
            "params": {"target": {"lat": 56.7005, "lng": 13.0038}}})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "not_reported")

    def test_a_vehicle_the_station_has_never_seen_is_a_plain_unknown_vehicle(self):
        """Not a quick-move refusal — there is no vehicle to refuse FOR. Pinned so the two
        are never conflated: `not_reported` means "this vehicle has not advertised", and a
        404 means "no such vehicle"."""
        r = self.client.post("/api/commands", json={
            "vehicle_id": "usv-77", "type": "QUICK_MOVE",
            "params": {"target": {"lat": 56.7005, "lng": 13.0038}}})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "unknown vehicle")


# ── 2/3. Starting and moving stay distinct from completion ─────────────────────────────
class ProgressionTests(CompatBase):
    def test_every_running_state_is_distinct_from_completion(self):
        envelopes = SCENARIOS["03_full_success"]
        minted = self.replay_progressively(envelopes)
        self.assertIsNotNone(minted)

    def replay_progressively(self, envelopes):
        """Replay one envelope at a time, asserting the published state after each."""
        captured = self.captured_command_id(envelopes)
        self.post(envelopes[0])
        main.comms_state_by_id[FIXTURE_VID] = "CONNECTED"
        active = next(((e.get("payload") or {}).get("quick_move") or {}).get("active")
                      for e in envelopes
                      if isinstance(((e.get("payload") or {}).get("quick_move") or {})
                                    .get("active"), dict))
        minted = self.client.post("/api/commands", json={
            "vehicle_id": FIXTURE_VID, "type": "QUICK_MOVE",
            "params": {"target": active["target"]}}).json()["command"]["id"]
        seen = []
        for env in envelopes:
            env = json.loads(json.dumps(env))
            blk = (env.get("payload") or {}).get("quick_move") or {}
            if isinstance(blk.get("active"), dict):
                blk["active"]["command_id"] = minted
            self.post(env)
            main.comms_state_by_id[FIXTURE_VID] = "CONNECTED"
            st = self.qm()["current"]["status"]
            if st:
                seen.append(st["state"])
                if st["state"] in ("STARTING", "MOVING"):
                    self.assertIsNone(st["completion"],
                                      f"{st['state']} must never carry a completion")
                    self.assertFalse(st["terminal"])
        self.assertEqual(seen[0], "STARTING")
        self.assertIn("MOVING", seen)
        self.assertEqual(seen[-1], "COMPLETED")
        self.assertTrue(captured)
        return minted

    def test_delivery_and_acceptance_are_never_a_completion(self):
        minted = self.replay("01_capability_only_heartbeat", correlate=False) or None
        main.comms_state_by_id[FIXTURE_VID] = "CONNECTED"
        cmd = self.client.post("/api/commands", json={
            "vehicle_id": FIXTURE_VID, "type": "QUICK_MOVE",
            "params": {"target": {"lat": 56.7005, "lng": 13.0038}}}).json()["command"]
        self.client.get(f"/agent/commands?usv_id={FIXTURE_VID}")
        self.client.post("/agent/command_result", json={
            "command_id": cmd["id"], "status": "EXECUTED",
            "result": {"verified": True, "observed_mode": "GUIDED"}})
        cur = self.qm()["current"]
        self.assertIsNone(cur["status"],
                          "an EXECUTED command result is not a movement, let alone an arrival")
        self.assertIsNone(minted)


# ── 4. Successful completion shows arrival AND verified LOITER consistently ────────────
class CompletionTests(CompatBase):
    def test_full_success_is_confirmed_and_internally_consistent(self):
        self.replay("03_full_success")
        st = self.qm()["current"]["status"]
        self.assertEqual(st["state"], "COMPLETED")
        self.assertEqual(st["completion"], "CONFIRMED")
        self.assertTrue(st["arrival_confirmed"])
        self.assertTrue(st["loiter_verified"])
        # Scout's own mode/completion coherence fix, checked rather than trusted.
        self.assertEqual(st["mode"], "LOITER")
        self.assertFalse(st["mode_incoherent"])

    def test_a_verified_hold_reported_in_another_mode_is_refused_as_confirmed(self):
        """Guards Scout's own fixed bug from regressing silently: `loiter_verified: true`
        alongside `mode: GUIDED` is self-contradictory, and the operator must not render it as
        a finished hold just because Scout said COMPLETED."""
        envelopes = json.loads(json.dumps(SCENARIOS["03_full_success"]))
        last = (envelopes[-1]["payload"]["quick_move"]["active"])
        last["mode"] = "GUIDED"
        SCENARIOS["_mutated_incoherent"] = envelopes
        try:
            self.replay("_mutated_incoherent")
            st = self.qm()["current"]["status"]
            self.assertEqual(st["state"], "COMPLETED")
            self.assertTrue(st["mode_incoherent"])
            self.assertEqual(st["completion"], "CLAIMED_UNVERIFIED",
                             "a self-contradictory report is never a confirmed hold")
        finally:
            SCENARIOS.pop("_mutated_incoherent", None)


# ── 5. Arrival followed by a failed LOITER is not shown as success ─────────────────────
class ArrivalFailureTests(CompatBase):
    def test_arrived_but_loiter_unverified_is_a_failure_that_still_reports_the_arrival(self):
        self.replay("04_arrival_loiter_failure")
        st = self.qm()["current"]["status"]
        self.assertEqual(st["state"], "FAILED")
        self.assertIsNone(st["completion"], "FAILED is never a completion of any kind")
        self.assertTrue(st["terminal"])
        # Both halves matter operationally: the vehicle IS at the target (so it is sitting
        # there, possibly drifting) and it is NOT confirmed holding.
        self.assertTrue(st["arrival_confirmed"])
        self.assertFalse(st["loiter_verified"])
        self.assertEqual(st["mode"], "GUIDED")
        self.assertIn("LOITER", st["reason"])


# ── 6. Rejection and failed dispatch show the actual reason ────────────────────────────
class RejectionTests(CompatBase):
    def test_scouts_not_armed_rejection_is_unreachable_because_the_operator_refuses_first(self):
        """FINDING. Scout's `rejected_not_armed` scenario cannot be produced from this UI: the
        operator gate reads the SAME telemetry and refuses `disarmed` before a command is ever
        created, so Scout's own `prepare()` never sees it.

        That is defence in depth working, not dead code on either side — Scout must keep its
        check (armed state can change between the operator's gate and Scout's prepare, and a
        caller other than this UI can reach the queue). But it does mean the operator-facing
        reason for a disarmed vehicle is the OPERATOR's sentence, not Scout's."""
        self.replay("07_rejected_not_armed", correlate=False)
        blk = self.qm()
        self.assertTrue(blk["capability"]["supported"])
        self.assertIsNone(blk["current"], "no movement was ever registered")
        self.assertEqual(blk["rejections"]["count"], 0,
                         "an absent `active` key is a heartbeat, not a malformed block")
        main.comms_state_by_id[FIXTURE_VID] = "CONNECTED"
        r = self.client.post("/api/commands", json={
            "vehicle_id": FIXTURE_VID, "type": "QUICK_MOVE",
            "params": {"target": {"lat": 56.7005, "lng": 13.0038}}})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "disarmed")

    def test_a_scout_only_rejection_reaches_the_operator_on_the_command_result(self):
        """The rejections the operator CANNOT pre-empt — the ones only Scout can know. The
        mission-supervisor restriction is the live example: Scout refuses a quick move while
        its controller is actively driving a bound mission, and this station has no way to
        know that before asking. The reason must arrive and be preserved verbatim."""
        self.replay("01_capability_only_heartbeat", correlate=False)
        main.comms_state_by_id[FIXTURE_VID] = "CONNECTED"
        cmd = self.client.post("/api/commands", json={
            "vehicle_id": FIXTURE_VID, "type": "QUICK_MOVE",
            "params": {"target": {"lat": 56.7005, "lng": 13.0038}}}).json()["command"]
        self.client.get(f"/agent/commands?usv_id={FIXTURE_VID}")
        reason = ("Mission supervisor is actively executing a bound mission (RUNNING) - "
                  "Quick move refused.")
        self.client.post("/agent/command_result", json={
            "command_id": cmd["id"], "status": "REJECTED", "reason": reason})
        rec = main.commands_by_id[cmd["id"]]
        self.assertEqual(rec["status"], "REJECTED")
        self.assertEqual(rec["reason"], reason, "Scout's sentence is preserved verbatim")
        self.assertEqual(rec["verification"]["outcome"], "REJECTED")
        # And no movement is invented from a refusal.
        self.assertIsNone(self.qm()["current"]["status"])

    def test_a_failed_dispatch_reports_its_own_reason_and_the_safety_hold_it_reached(self):
        self.replay("08_failed_dispatch_target_not_accepted")
        st = self.qm()["current"]["status"]
        self.assertEqual(st["state"], "FAILED")
        self.assertTrue(st["terminal"])
        self.assertIn("MAV_RESULT_DENIED", st["reason"])
        self.assertEqual(st["mode"], "LOITER", "the safety LOITER that did take is reported")
        self.assertIsNone(st["completion"])


# ── 7. Cancellation / authority transfer cannot be overwritten by delayed older status ──
class CancellationTests(CompatBase):
    def test_a_loiter_cancellation_survives_a_delayed_older_moving_report(self):
        self.replay("05_cancelled_by_loiter_command")
        blk = self.qm()
        st = blk["current"]["status"]
        self.assertEqual(st["state"], "CANCELLED", "a buffered MOVING must not resurrect it")
        self.assertIn("LOITER", st["reason"])
        self.assertEqual(blk["rejections"]["last_reason"], "terminal_state_regression")

    def test_an_authority_transfer_cancellation_survives_the_same_replay(self):
        self.replay("06_authority_transfer_mid_move")
        blk = self.qm()
        st = blk["current"]["status"]
        self.assertEqual(st["state"], "CANCELLED")
        self.assertIn("authority", st["reason"].lower())
        self.assertEqual(blk["rejections"]["last_reason"], "terminal_state_regression")

    def test_a_status_for_an_uncorrelated_command_is_refused_outright(self):
        """The bare-replay case: Scout's own captured ids were never issued here, so every
        status is refused. This is the guard working — and it is why --correlate-commands
        exists rather than the guard being relaxed."""
        self.replay("03_full_success", correlate=False)
        blk = self.qm()
        self.assertIsNone(blk["current"])
        self.assertEqual(blk["rejections"]["last_reason"], "unknown_command_id")
        self.assertGreater(blk["rejections"]["count"], 0)


# ── 8. Vehicle selection does not mix targets or results ───────────────────────────────
class MultiVehicleTests(CompatBase):
    def test_two_vehicles_never_share_a_target_or_an_outcome(self):
        minted = self.replay("03_full_success")
        # A second vehicle reports a movement for the FIRST vehicle's command id.
        other = "usv-3-fixture"
        env = json.loads(json.dumps(SCENARIOS["03_full_success"][1]))
        env["source"] = other
        env["payload"]["usv_id"] = other
        env["payload"]["name"] = "SAR (SIMULATED QUICK MOVE FIXTURE)"
        env["payload"]["quick_move"]["active"]["command_id"] = minted
        env["payload"]["quick_move"]["active"]["target"] = {"lat": 56.7100, "lng": 13.0200}
        self.post(env)
        first, second = self.qm(), self.qm(other)
        self.assertEqual(first["current"]["status"]["state"], "COMPLETED",
                         "the other vehicle's report must not touch this one")
        self.assertIsNone(second["current"], "it is not that vehicle's move")
        self.assertEqual(second["rejections"]["last_reason"], "unknown_command_id")
        for store in (main.current_vehicle_state, main.comms_state_by_id, main.last_seen_by_id):
            store.pop(other, None)


# ── 9. Stale telemetry does not appear current ─────────────────────────────────────────
class StalenessTests(CompatBase):
    def test_every_published_movement_carries_its_own_age(self):
        self.replay("03_full_success")
        st = self.qm()["current"]["status"]
        self.assertIsNotNone(st["status_age_s"], "the UI cannot age what is not published")
        fresh = st["updated"]
        for key in ("observed_at", "min_age_s", "reporting_delay_s", "estimated_age_s",
                    "freshness_quality", "clock_anomaly"):
            self.assertIn(key, fresh)
        self.assertIn(fresh["freshness_quality"], ("LOWER_BOUND", "UNKNOWN"))

    def test_a_replayed_capture_is_honestly_aged_not_presented_as_live(self):
        """Scout's captures carry its ORIGINAL clock. Replayed later, `min_age_s` must reflect
        that honestly rather than being reset to look fresh — the whole freshness model exists
        so a replayed observation cannot masquerade as a current one."""
        self.replay("03_full_success")
        fresh = self.qm()["current"]["status"]["updated"]
        self.assertEqual(fresh["freshness_quality"], "LOWER_BOUND")
        self.assertIsNotNone(fresh["min_age_s"])
        self.assertEqual(fresh["observed_at"], 1790000008.0,
                         "Scout's own source timestamp is preserved verbatim")


if __name__ == "__main__":
    unittest.main()
