"""companion-v2 — Scout's optional UAV-companion block (companion_telemetry.py).

Pins the contract in COMPANION_CONTRACT.md: backward compatibility, per-parent isolation,
the three-number freshness model (source age / reporting delay / estimated age, all
monotonic-clock-based locally), snapshot session/seq ordering on top of per-hazard revisions,
duplicate/out-of-order handling, validation bounds, explicit clear vs omission (and reliable
REPEATED clearing), and that companion telemetry has NO command, mission or planning side
effects.
"""
import json
import math
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import companion_telemetry as ct
import main
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import companion_fixture as fx  # noqa: E402

SCOUT, SAR = fx.SCOUT, fx.SAR


def _shift_mark(mark, seconds):
    if mark is not None:
        mark["wall"] -= seconds
        mark["mono"] -= seconds


def _shift_evidence(ev, seconds):
    if ev is not None:
        _shift_mark((ev.get("receipt") or {}).get("mark"), seconds)


class Base(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        for store in (main.current_vehicle_state, main.last_known_telemetry,
                      main.last_known_agent, main.latest_msg_ts_by_id, main.last_seen_by_id,
                      main.comms_state_by_id, main.comms_history_by_id,
                      main.last_agent_decision_by_id, main.last_mission_state_by_id,
                      main.last_known_groups, main.packet_loss_by_id,
                      main.companion_state_by_id):
            store.clear()
        # The command queue is module-level too: a command another test file left behind (e.g.
        # an EXECUTED SET_HOME for Scout) must not read as one created by a companion report.
        main.commands.clear()
        main.commands_by_id.clear()
        main.vehicle_names = {c: main.REGISTRY.default_display_name(c)
                              for c in main.REGISTRY.configured_ids()}

    def post(self, env):
        r = self.client.post("/agent/status", json=env)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def row(self, cid):
        r = self.client.get("/api/fleet/status")
        self.assertEqual(r.status_code, 200, r.text)
        rows = [v for v in r.json() if v["id"] == cid]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def items(self, cid):
        return self.row(cid)["companions"]["items"]

    def only(self, cid=SCOUT):
        items = self.items(cid)
        self.assertEqual(len(items), 1, items)
        return items[0]

    def backdate(self, seconds, cid=SCOUT):
        """Scout went quiet `seconds` ago: every receipt-clock mark this module stores (wall
        AND monotonic) moves back together, exactly as if nothing had been received since —
        including every nested piece of evidence, not just the top-level bookkeeping."""
        main.last_seen_by_id[cid] = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        rec = main.current_vehicle_state[cid]
        rec["received_at"] = main.last_seen_by_id[cid]
        st = main.companion_state_by_id.get(cid)
        if not st:
            return
        _shift_mark(st.get("block_received"), seconds)
        if st.get("session"):
            _shift_mark(st["session"].get("started"), seconds)
        for c in st["companions"].values():
            _shift_mark(c.get("status_received"), seconds)
            _shift_mark(c.get("hazards_received"), seconds)
            for key in ("position", "battery", "inspection"):
                _shift_evidence(c.get(key), seconds)
            _shift_evidence((c.get("link") or {}).get("last_peer_contact"), seconds)
            for h in c["hazards"].values():
                _shift_evidence(h.get("observed"), seconds)
                _shift_evidence((h.get("disposition") or {}).get("decided"), seconds)
                replan = h.get("replan")
                if replan:
                    _shift_evidence(replan.get("updated"), seconds)


def ingest(store, payload, *, ts, rx=None, rx_mono=None, vid=SCOUT):
    """Test-only shortcut around ct.ingest(): `rx`/`rx_mono` default to `ts` (or the explicit
    `rx`), which is a safe simplification here since wall and monotonic clocks are never
    compared against EACH OTHER inside companion_telemetry.py — only within their own domain
    (reporting_delay_s is wall-vs-wall/Scout-clock; min_age_s's elapsed term is mono-vs-mono)."""
    rx = ts if rx is None else rx
    ct.ingest(store, vid, payload, packet_ts=ts, received_at=rx,
              received_mono=rx if rx_mono is None else rx_mono, resolve_parent=main.canonical_id)


def blk(*items, seq=1, session_id="test-session", generation=1):
    """A companion-v2 block with FIXED defaults, deliberately decoupled from fx's own
    module-level `_generation_state` (which mutates across the whole test process whenever a
    test calls fx.bump_generation()) — so an ordinary test's calls stay self-consistent and
    test-order-independent regardless of what other tests in this file have done."""
    return fx.block(*items, seq=seq, session_id=session_id, generation=generation)


# --- 1. backward compatibility ---------------------------------------------------------------

class BackwardCompatibility(Base):
    def test_legacy_payload_has_empty_unreported_block_and_untouched_row(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t))
        row = self.row(SCOUT)
        self.assertEqual(row["companions"]["reported"], False)
        self.assertEqual(row["companions"]["items"], [])
        self.assertEqual(row["companions"]["schema"], "companion-v2")
        self.assertIsNone(row["companions"]["session"])
        self.assertEqual(row["battery"], 82)
        self.assertEqual(row["comm_state"], "CONNECTED")
        self.assertAlmostEqual(row["lat"], fx.SCOUT_POS[0])

    def test_never_contacted_row_has_the_same_shape(self):
        row = self.row(1)
        self.assertEqual(row["contacted"], False)
        self.assertEqual(row["companions"], ct.empty_block())

    def test_companion_v1_is_explicitly_rejected_not_silently_reinterpreted(self):
        """companion-v1 was proposed but never adopted by any real Scout build — this is a
        deliberate, breaking schema revision (see the module docstring), so a v1-shaped block
        is rejected the same way any other unrecognised schema is, never quietly upgraded."""
        t = time.time()
        v1 = {"schema": "companion-v1", "items": [fx.uav(t, hazards=[])]}
        self.post(fx.envelope(SCOUT, t, companions=v1))
        row = self.row(SCOUT)
        self.assertEqual(row["companions"]["items"], [])
        self.assertFalse(row["companions"]["reported"])
        self.assertEqual(row["companions"]["rejections"]["by_reason"]["unsupported_schema"], 1)

    def test_every_fixture_scenario_posts_and_the_fleet_stays_serializable(self):
        t0 = time.time()
        for i, (name, (_, build)) in enumerate(fx.SCENARIOS.items()):
            seq = fx.next_seq()
            for k in range(2):
                for env in build(t0, t0 + i * 4 + k * 2, k, seq):
                    json.dumps(env, allow_nan=False)
                    self.post(env)
        self.row(SCOUT)
        self.row(SAR)

    def test_fixture_refuses_non_loopback_targets(self):
        with self.assertRaises(SystemExit):
            fx.check_loopback("http://10.0.2.10:8000")
        fx.check_loopback("http://127.0.0.1:8199")


# --- 2. contract content -----------------------------------------------------------------------

class Contract(Base):
    def test_assigned_with_no_contact(self):
        t = time.time()
        _, build = fx.SCENARIOS["assigned_no_contact"]
        self.post(build(t, t, 0, fx.next_seq())[0])
        c = self.only()
        self.assertEqual(c["assignment_state"], "ASSIGNED")
        self.assertEqual(c["link"]["state"], "NEVER_CONNECTED")
        self.assertIsNone(c["link"]["last_peer_contact"])
        self.assertIsNone(c["position"])
        self.assertIsNone(c["battery"])
        self.assertIsNone(c["inspection"])
        self.assertEqual(c["parent_id"], SCOUT)
        self.assertEqual(c["vehicle_type"], "UAV")

    def test_active_inspection_with_fresh_position(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, hazards=[]))))
        c = self.only()
        self.assertEqual(c["activity"], {"state": "INSPECTING", "task_id": "insp-0001",
                                         "route_revision": 3})
        pos = c["position"]
        self.assertEqual(pos["freshness_quality"], "LOWER_BOUND")
        self.assertLess(pos["min_age_s"], 3)
        self.assertLess(pos["estimated_age_s"], 3)
        self.assertFalse(pos["clock_anomaly"])
        self.assertEqual(pos["altitude_ref"], "AGL")
        self.assertIsNotNone(pos["observed_at"])
        self.assertEqual(c["battery"]["remaining_pct"], 71.0)
        self.assertEqual(c["inspection"]["progress_pct"], 40.0)
        self.assertTrue(c["hazards_reported"])
        self.assertEqual(c["hazards"], [])

    def test_hazard_lifecycle_is_carried_verbatim(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, hazards=[fx.buoy(t - 1)]))))
        h = self.only()["hazards"][0]
        self.assertEqual((h["hazard_id"], h["revision"], h["kind"]), ("hz-buoy-1", 1, "BUOY"))
        self.assertEqual(h["disposition"]["state"], "REPORTED")
        self.assertIsNone(h["replan"], "a missing outcome stays unreported")
        self.assertEqual(h["geometry"]["type"], "point")
        self.assertEqual(h["exclusion"]["type"], "polygon")
        self.assertEqual(len(h["exclusion"]["ring_latlng"]), 4)
        self.assertEqual(h["uncertainty_m"], 8.0)
        self.assertEqual(h["source"], "uav-1:eo_camera")
        self.assertEqual(h["observed"]["freshness_quality"], "LOWER_BOUND")

        t2 = t + 2
        self.post(fx.envelope(SCOUT, t2, companions=blk(fx.uav(t2, hazards=[fx._verified(t)]),
                                                        seq=2)))
        h = self.only()["hazards"][0]
        self.assertEqual(h["disposition"]["state"], "ACCEPTED")
        self.assertEqual(h["replan"]["state"], "VERIFIED")
        self.assertEqual(h["replan"]["mission_revision"], 4)
        self.assertIsNotNone(h["replan"]["updated"])
        self.assertEqual(h["replan"]["updated"]["freshness_quality"], "LOWER_BOUND")

    def test_unrecognised_tokens_never_become_accepted_or_verified(self):
        t = time.time()
        hz = fx.buoy(t, disposition="ACCEPTED_MAYBE",
                     replan={"state": "DONE", "updated_at": t})
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, link="GREAT", hazards=[hz]))))
        c = self.only()
        self.assertEqual(c["link"]["state"], "UNKNOWN")
        self.assertEqual(c["hazards"][0]["disposition"]["state"], "UNKNOWN")
        self.assertEqual(c["hazards"][0]["replan"]["state"], "UNKNOWN")

    def test_altitude_without_reference_is_dropped_not_guessed(self):
        t = time.time()
        item = fx.uav(t, hazards=[])
        del item["position"]["altitude_ref"]
        self.post(fx.envelope(SCOUT, t, companions=blk(item)))
        pos = self.only()["position"]
        self.assertIsNone(pos["altitude_m"])
        self.assertIsNone(pos["altitude_ref"])
        self.assertAlmostEqual(pos["lat"], fx.UAV_POS[0])

    def test_block_publishes_session_id_and_seq(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, hazards=[]), seq=7,
                                                        session_id="abc123")))
        session = self.row(SCOUT)["companions"]["session"]
        self.assertEqual(session["id"], "abc123")
        self.assertEqual(session["seq"], 7)
        self.assertLess(session["age_s"], 3)


# --- 3. freshness: three distinct numbers, never collapsed into one --------------------------

class Freshness(Base):
    def test_fresh_packet_with_old_uav_position_reads_old(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, obs_at=t - 60, peer_at=t - 60))))
        c = self.only()
        self.assertEqual(self.row(SCOUT)["comm_state"], "CONNECTED")
        self.assertGreaterEqual(c["position"]["min_age_s"], 60)
        self.assertGreaterEqual(c["position"]["estimated_age_s"], 60)
        self.assertGreaterEqual(c["link"]["last_peer_contact"]["min_age_s"], 60)
        self.assertLess(c["status_age_s"], 3, "Scout's claim itself is fresh")

    def test_delayed_envelope_reads_as_at_least_the_transit_delay_old(self):
        """A newly received envelope delayed ~30s in transit, with no newer packet already
        received: the naive 'age = envelope.timestamp - observed_at, plus elapsed since receipt'
        formula would read this as ~0s old the instant it arrives. min_age_s alone (which
        assumes zero delivery delay) still understates it — but reporting_delay_s/estimated_age_s
        must surface the ~30s gap rather than silently treating it as zero."""
        store = {}
        send_ts = 1_000_000.0
        arrival_wall = send_ts + 30.0             # the packet took ~30s to actually arrive
        ingest(store, {"companions": blk(fx.uav(send_ts, obs_at=send_ts - 0.2), seq=1)},
              ts=send_ts, rx=arrival_wall, rx_mono=arrival_wall)
        c = ct.fleet_block(store, SCOUT, arrival_wall + 0.1, arrival_wall + 0.1)["items"][0]
        pos = c["position"]
        self.assertEqual(pos["freshness_quality"], "LOWER_BOUND")
        self.assertLess(pos["min_age_s"], 1.0, "min_age_s assumes zero transit delay by design")
        self.assertGreaterEqual(pos["reporting_delay_s"], 29.5)
        self.assertGreaterEqual(pos["estimated_age_s"], 29.5,
                                "the ESTIMATE must reflect the delay — never read as fresh")
        self.assertFalse(pos["clock_anomaly"])

    def test_duplicate_delivery_does_not_manufacture_freshness(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, obs_at=99.5), seq=1)}, ts=100.0, rx=100.0)
        first_mark = dict(store[SCOUT]["companions"]["uav-1"]["position"]["receipt"]["mark"])
        # The EXACT same envelope, re-delivered (e.g. a retry) 10s later in wall/receipt time.
        ingest(store, {"companions": blk(fx.uav(100.0, obs_at=99.5), seq=1)}, ts=100.0, rx=110.0,
              rx_mono=110.0)
        second_mark = store[SCOUT]["companions"]["uav-1"]["position"]["receipt"]["mark"]
        self.assertEqual(second_mark, first_mark, "a duplicate keeps the ORIGINAL receipt clock")
        c = ct.fleet_block(store, SCOUT, 110.0, 110.0)["items"][0]
        self.assertGreaterEqual(c["position"]["min_age_s"], 10.0,
                                "age grows with real elapsed time, not reset by the replay")

    def test_missing_packet_timestamp_makes_freshness_unknown_not_fresh(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(1000.0), seq=1)}, ts=None, rx=5000.0)
        c = ct.fleet_block(store, SCOUT, 5001.0, 5001.0)["items"][0]
        self.assertEqual(c["position"]["freshness_quality"], "UNKNOWN")
        self.assertIsNone(c["position"]["min_age_s"])
        self.assertIsNone(c["position"]["reporting_delay_s"])
        self.assertIsNone(c["position"]["estimated_age_s"])
        self.assertIsNotNone(c["position"]["observed_at"], "the source timestamp is still preserved")
        # Link contact evidence exists (Scout reported a last_peer_contact_at) but gets the SAME
        # UNKNOWN treatment as position — evidence with no envelope timestamp to anchor its age.
        self.assertIsNotNone(c["link"]["last_peer_contact"])
        self.assertEqual(c["link"]["last_peer_contact"]["freshness_quality"], "UNKNOWN")
        self.assertIsNone(c["link"]["last_peer_contact"]["min_age_s"])

    def test_clock_offset_does_not_corrupt_the_guaranteed_lower_bound(self):
        """Scout's clock an hour behind ours: min_age_s (a same-clock-on-each-side delta) must
        stay correct regardless, because it never compares the two clocks against each other."""
        store = {}
        skew = -3600.0
        rx = 1_800_000_000.0
        ts = rx + skew
        ingest(store, {"companions": blk(fx.uav(ts, obs_at=ts - 5), seq=1)}, ts=ts, rx=rx)
        c = ct.fleet_block(store, SCOUT, rx + 2, rx + 2)["items"][0]
        self.assertAlmostEqual(c["position"]["min_age_s"], 7.0, places=1)
        # reporting_delay_s DOES mix in the offset (received_at - envelope.timestamp), so it is
        # documented as an estimate, not proof — here it reads ~3600s, which is the offset, not
        # a real delivery delay, exactly the ambiguity the contract says never to treat as fact.
        self.assertGreater(c["position"]["reporting_delay_s"], 3500.0)

    def test_clock_discontinuity_is_flagged_not_silently_absorbed(self):
        """Envelope timestamp is notably AHEAD of the operator's own wall clock at receipt —
        signals the two clocks disagree or one jumped. min_age_s stays valid regardless (it
        never compares the two clocks), but clock_anomaly must be surfaced."""
        store = {}
        rx = 2_000_000_000.0
        ts = rx + 30.0                              # envelope claims to be from 30s in the future
        ingest(store, {"companions": blk(fx.uav(ts, obs_at=ts - 0.5), seq=1)}, ts=ts, rx=rx)
        c = ct.fleet_block(store, SCOUT, rx + 1, rx + 1)["items"][0]
        self.assertTrue(c["position"]["clock_anomaly"])
        self.assertEqual(c["position"]["reporting_delay_s"], 0.0, "clamped, never negative")
        self.assertLess(c["position"]["min_age_s"], 2.0, "still a valid, unaffected lower bound")

    def test_clock_offset_plus_matching_delivery_delay_must_not_read_as_fresh(self):
        """THE exact counterexample this rewrite exists for: Scout's clock is 30s AHEAD of the
        operator's, and the envelope genuinely took 30s to be delivered. The two effects cancel
        in reporting_delay_s's raw calculation (receipt_wall - envelope.timestamp), so it reads
        ~0 -- an observation that is REALLY ~60s old (5s send-time freshness + 30s clock offset +
        ~25s more delivery) must not be presented as having zero delivery delay. min_age_s alone
        (the only number this module lets anything call STALE) still only sees the small,
        legitimate 5s same-clock delta and cannot detect the true age either -- this is exactly
        why the classification is UNCERTAIN here, never FRESH, regardless of how good every
        individual number looks (see operator/lib/companion.js's evidenceView for where that
        classification is actually made)."""
        store = {}
        real_observe = 1_000_000.0                 # true wall-clock time of the UAV fix
        real_send = real_observe + 5.0              # Scout composes the envelope 5s later
        real_deliver = real_send + 30.0              # genuine 30s delivery delay
        clock_offset = 30.0                          # Scout's clock reads 30s ahead of real time
        scout_ts_observe = real_observe + clock_offset
        scout_ts_send = real_send + clock_offset
        operator_wall_receive = real_deliver         # operator's own clock is accurate

        ingest(store, {"companions": blk(fx.uav(scout_ts_send, obs_at=scout_ts_observe), seq=1)},
              ts=scout_ts_send, rx=operator_wall_receive, rx_mono=operator_wall_receive)
        c = ct.fleet_block(store, SCOUT, operator_wall_receive, operator_wall_receive)["items"][0]
        pos = c["position"]
        self.assertAlmostEqual(pos["min_age_s"], 5.0, delta=0.2,
                               msg="only the genuine 5s same-clock delta is provable")
        self.assertAlmostEqual(pos["reporting_delay_s"], 0.0, delta=0.2,
                               msg="the offset and the real delay cancel out in this estimate")
        self.assertLess(pos["estimated_age_s"], 6.0,
                        "the flawed estimate looks fresh even though the true age is ~35s")
        # The true age at receipt was real_deliver - real_observe = 35s. Neither number above
        # comes anywhere close to admitting that -- which is exactly why NEITHER may be used to
        # assert freshness; only min_age_s crossing a threshold may ever assert STALE.
        self.assertLess(pos["min_age_s"], real_deliver - real_observe)
        self.assertLess(pos["estimated_age_s"], real_deliver - real_observe)

    def test_future_observation_is_rejected(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, obs_at=t + 30))))
        row = self.row(SCOUT)
        self.assertIsNone(row["companions"]["items"][0]["position"])
        self.assertIn("future_observed_at", row["companions"]["rejections"]["by_reason"])

    def test_scout_contact_loss_keeps_last_claim_but_ages_it(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, hazards=[fx.buoy(t)]))))
        self.backdate(40)
        row = self.row(SCOUT)
        self.assertEqual(row["comm_state"], "DISCONNECTED")
        c = row["companions"]["items"][0]
        self.assertEqual(c["link"]["state"], "CONNECTED", "Scout's last claim, verbatim")
        self.assertGreaterEqual(c["status_age_s"], 40, "…but visibly 40 s old")
        self.assertGreaterEqual(c["link"]["last_peer_contact"]["min_age_s"], 40)
        self.assertGreaterEqual(c["position"]["min_age_s"], 40)
        self.assertGreaterEqual(c["hazards_age_s"], 40)

    def test_clock_discontinuity_via_the_real_endpoint_poisons_the_outer_guard_not_this_module(self):
        """Documents a REAL interaction, found via the browser fixture: an envelope whose own
        timestamp is far in the future is ACCEPTED by main.py's pre-existing per-vehicle
        monotonic guard (it only rejects timestamps that go BACKWARD), which then raises that
        guard's high-water mark into the future. Every subsequent NORMAL-timestamped packet for
        that vehicle is rejected as stale (by the OUTER guard, before companion_telemetry.py
        ever runs) until real time catches up. This is a property of the pre-existing guard this
        module deliberately does not touch or duplicate (see its module docstring and
        COMPANION_CONTRACT.md's Limitations) — companion_telemetry.py's OWN session/seq
        ordering is unaffected and would apply correctly if it were ever reached; it simply
        never is reached while the outer guard is rejecting the envelope outright."""
        t = time.time()
        self.post(fx.envelope(SCOUT, t + 30.0, companions=blk(fx.uav(t + 30.0), seq=1)))
        r = self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, link="LOST"), seq=2)))
        self.assertTrue(r["stale"], "the OUTER envelope guard rejects it, not companion_telemetry")
        self.assertEqual(self.only()["link"]["state"], "CONNECTED", "the future packet's claim stands")

    def test_local_elapsed_time_uses_the_monotonic_clock_not_wall_clock(self):
        """The elapsed-since-receipt component of min_age_s/status_age_s must come from the
        MONOTONIC reading, immune to a wall-clock jump — passing a wildly different `now_wall_s`
        (simulating an NTP step / DST change / manual clock correction) must not move it at all,
        only advancing `now_mono_s` may."""
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, obs_at=99.5), seq=1)}, ts=100.0, rx=100.0)
        normal = ct.fleet_block(store, SCOUT, 105.0, 105.0)["items"][0]
        # Wall clock stepped backward by an hour; monotonic clock is unaffected by such jumps.
        jumped = ct.fleet_block(store, SCOUT, 105.0 - 3600.0, 105.0)["items"][0]
        self.assertEqual(normal["position"]["min_age_s"], jumped["position"]["min_age_s"])
        self.assertEqual(normal["status_age_s"], jumped["status_age_s"])


# --- 4. snapshot session/seq ordering (block-level, on top of per-hazard revisions) ----------

class SnapshotOrdering(Base):
    def test_old_snapshot_inside_newer_envelope_does_not_overwrite(self):
        """The OUTER envelope guard only proves the envelope itself is not a replay — it says
        nothing about whether the COMPANION PAYLOAD inside it is current. A later envelope
        (newer send time) carrying an OLDER companion seq must leave the newer snapshot alone."""
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, hazards=[fx._verified(100.0)]), seq=5)},
              ts=100.0, rx=100.0)
        ingest(store, {"companions": blk(fx.uav(101.0, hazards=[fx.buoy(100.0)]), seq=4)},
              ts=101.0, rx=101.0)   # envelope is NEWER, but seq regressed within the same session
        c = ct.fleet_block(store, SCOUT, 101.0, 101.0)["items"][0]
        self.assertEqual(c["hazards"][0]["disposition"]["state"], "ACCEPTED",
                         "the newer (seq 5) snapshot must survive untouched")
        self.assertEqual(store[SCOUT]["rejections"]["by_reason"]["stale_snapshot_seq"], 1)

    def test_equal_seq_is_idempotent_but_still_confirms_liveness(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, obs_at=99.5), seq=3)}, ts=100.0, rx=100.0)
        before = dict(store[SCOUT]["companions"]["uav-1"]["position"])
        ingest(store, {"companions": blk(fx.uav(100.0, obs_at=99.5), seq=3)}, ts=100.0, rx=105.0,
              rx_mono=105.0)
        after = store[SCOUT]["companions"]["uav-1"]["position"]
        # The OBSERVATION itself (and its receipt clock) is untouched by the resend...
        self.assertEqual(after["observed_at"], before["observed_at"])
        self.assertEqual(after["receipt"]["mark"], before["receipt"]["mark"])
        # ...but Scout confirming it is STILL alive and reporting DOES refresh status liveness.
        c = ct.fleet_block(store, SCOUT, 105.0, 105.0)["items"][0]
        self.assertLess(c["status_age_s"], 1.0)

    def test_hazard_omitted_by_newer_complete_snapshot_is_removed(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, hazards=[fx.buoy(99.0)]), seq=1)}, ts=100.0)
        ingest(store, {"companions": blk(fx.uav(101.0, hazards=[]), seq=2)}, ts=101.0)
        self.assertEqual(store[SCOUT]["companions"]["uav-1"]["hazards"], {})

    def test_old_snapshot_cannot_restore_a_removed_hazard(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, hazards=[fx.buoy(99.0)]), seq=1)}, ts=100.0)
        ingest(store, {"companions": blk(fx.uav(101.0, hazards=[]), seq=2)}, ts=101.0)   # removed
        # A later envelope replays the OLD (seq 1) snapshot, which still lists the hazard.
        ingest(store, {"companions": blk(fx.uav(102.0, hazards=[fx.buoy(99.0)]), seq=1)}, ts=102.0)
        self.assertEqual(store[SCOUT]["companions"]["uav-1"]["hazards"], {},
                         "the stale seq must not resurrect a hazard the newer snapshot removed")

    def test_empty_snapshot_then_stale_nonempty_data_is_rejected(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1)}, ts=100.0)
        ingest(store, {"companions": blk(seq=2)}, ts=101.0)                     # explicit clear
        ingest(store, {"companions": blk(fx.uav(102.0), seq=1)}, ts=102.0)      # stale replay
        self.assertEqual(store[SCOUT]["companions"], {}, "the clear must survive a stale replay")

    def test_A_then_B_then_delayed_A_inside_a_newer_envelope_rejects_A_and_preserves_B(self):
        """Part 1's exact required scenario. Generation A is accepted; Scout's companion
        subsystem restarts to generation B (a genuinely HIGHER generation number); a STILL
        LATER envelope (its own timestamp newer again, so main.py's outer guard would happily
        accept it) replays generation A's old, now-RETIRED data. B's state must be completely
        untouched by the replay — not membership, not hazards, not activity, not measurements."""
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, link="LOST", hazards=[fx.buoy(99.0)]),
                                         seq=9, session_id="session-a", generation=1)}, ts=100.0)
        ingest(store, {"companions": blk(fx.uav(101.0, link="CONNECTED", hazards=[]),
                                         seq=1, session_id="session-b", generation=2)}, ts=101.0)
        self.assertEqual(store[SCOUT]["session"]["id"], "session-b")
        self.assertEqual(store[SCOUT]["session"]["generation"], 2)
        self.assertEqual(store[SCOUT]["companions"]["uav-1"]["link"]["state"], "CONNECTED")
        # A still-later envelope (timestamp 102 > 101, main.py's own guard would accept it)
        # replays generation A's old data verbatim: same id, same generation, higher seq than A
        # ever reached, different (LOST) link and a hazard A never lost. None of it may apply.
        ingest(store, {"companions": blk(fx.uav(102.0, link="LOST", hazards=[fx.buoy(99.0)]),
                                         seq=99, session_id="session-a", generation=1)}, ts=102.0)
        self.assertEqual(store[SCOUT]["session"]["id"], "session-b", "B must remain current")
        self.assertEqual(store[SCOUT]["session"]["generation"], 2)
        self.assertEqual(store[SCOUT]["companions"]["uav-1"]["link"]["state"], "CONNECTED",
                         "the retired generation must not modify link/activity")
        self.assertEqual(store[SCOUT]["companions"]["uav-1"]["hazards"], {},
                         "nor resurrect a hazard B never reported")
        self.assertIn("retired_session_generation", store[SCOUT]["rejections"]["by_reason"])

    def test_unfamiliar_older_session_is_still_rejected_by_generation_not_by_recognition(self):
        """Retired-session TRACKING alone only recognises ids this process has actually seen —
        an "unfamiliar" session (never observed before, e.g. because it predates an operator
        restart, or arrives wildly out of order) must still be judged correctly. The policy here
        never depends on recognising the id at all: ANY session, familiar or not, whose
        `generation` is behind the tracked high-water mark is rejected, purely by the numbers."""
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1, session_id="session-current",
                                         generation=5)}, ts=100.0)
        # A THIRD session id this process has never seen even once, at a lower generation.
        ingest(store, {"companions": blk(fx.uav(101.0, link="LOST"), seq=1,
                                         session_id="session-never-seen-before", generation=3)},
              ts=101.0)
        self.assertEqual(store[SCOUT]["session"]["id"], "session-current")
        self.assertEqual(store[SCOUT]["session"]["generation"], 5)
        self.assertIn("retired_session_generation", store[SCOUT]["rejections"]["by_reason"])

    def test_missing_packet_timestamp_cannot_bypass_session_protection(self):
        """The generation check is pure integer comparison of Scout-supplied values — unlike the
        superseded session-id-trust policy, it does NOT rely on the envelope having a timestamp
        at all. A retired generation is rejected even when packet_ts is None throughout."""
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, link="LOST"), seq=1,
                                         session_id="a", generation=1)}, ts=None, rx=100.0)
        ingest(store, {"companions": blk(fx.uav(101.0, link="CONNECTED"), seq=1,
                                         session_id="b", generation=2)}, ts=None, rx=101.0)
        # A retired-generation replay, still with no envelope timestamp.
        ingest(store, {"companions": blk(fx.uav(102.0, link="LOST"), seq=99,
                                         session_id="a", generation=1)}, ts=None, rx=102.0)
        self.assertEqual(store[SCOUT]["session"]["generation"], 2)
        self.assertEqual(store[SCOUT]["companions"]["uav-1"]["link"]["state"], "CONNECTED")
        self.assertIn("retired_session_generation", store[SCOUT]["rejections"]["by_reason"])

    def test_inconsistent_generation_with_a_different_id_is_distrusted_wholesale(self):
        """The SAME generation reported under a DIFFERENT id is self-contradictory (a generation
        only advances on a restart, and a restart is what gives it a fresh id too) — rejected
        entirely, including no liveness update, rather than trusted as new content."""
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, hazards=[]), seq=1,
                                         session_id="a", generation=1)}, ts=100.0)
        before = dict(store[SCOUT]["companions"]["uav-1"])
        block_received_before = store[SCOUT]["block_received"]
        ingest(store, {"companions": blk(fx.uav(101.0, link="LOST"), seq=2,
                                         session_id="a-imposter", generation=1)}, ts=101.0)
        self.assertEqual(store[SCOUT]["companions"]["uav-1"]["link"], before["link"],
                         "the imposter block must not modify companion state")
        self.assertEqual(store[SCOUT]["block_received"], block_received_before,
                         "not even liveness — this report violates the ordering contract")
        self.assertIn("inconsistent_session_generation", store[SCOUT]["rejections"]["by_reason"])

    def test_missing_or_malformed_session_rejects_the_whole_block(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1)}, ts=100.0)
        for bad in ({"schema": "companion-v2", "items": []},                         # no session
                    {"schema": "companion-v2", "session": {"seq": 1, "generation": 1},
                     "items": []},                                                    # no id
                    {"schema": "companion-v2", "session": {"id": "x", "generation": 1},
                     "items": []},                                                    # no seq
                    {"schema": "companion-v2", "session": {"id": "x", "seq": 1},
                     "items": []},                                                    # no generation
                    {"schema": "companion-v2", "session": "x", "items": []},          # not an object
                    {"schema": "companion-v2",
                     "session": {"id": "x", "seq": -1, "generation": 1}, "items": []},
                    {"schema": "companion-v2",
                     "session": {"id": "x", "seq": 1, "generation": -1}, "items": []}):
            ingest(store, {"companions": bad}, ts=101.0)
        self.assertEqual(list(store[SCOUT]["companions"]), ["uav-1"], "last-known kept")
        self.assertFalse(store[SCOUT]["companions"] == {})
        by = store[SCOUT]["rejections"]["by_reason"]
        self.assertGreaterEqual(by.get("missing_snapshot_session", 0)
                                + by.get("bad_snapshot_seq", 0)
                                + by.get("bad_session_generation", 0), 7)

    def test_clearing_survives_replay_of_an_obsolete_nonempty_snapshot(self):
        """A retired-generation snapshot that is NONEMPTY must not un-clear a companion that a
        higher generation already cleared."""
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1, session_id="a", generation=1)},
              ts=100.0)
        ingest(store, {"companions": blk(seq=1, session_id="b", generation=2)}, ts=101.0)  # clear
        self.assertEqual(store[SCOUT]["companions"], {})
        ingest(store, {"companions": blk(fx.uav(102.0), seq=2, session_id="a", generation=1)},
              ts=102.0)   # retired generation, nonempty — must not restore the companion
        self.assertEqual(store[SCOUT]["companions"], {},
                         "the clear must survive a retired-generation replay")

    def test_two_parent_vehicles_have_independent_sessions_and_generations(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=99, session_id="scout-session",
                                         generation=7)}, ts=100.0, vid=SCOUT)
        ingest(store, {"companions": blk(fx.uav(100.0, parent="usv-3"), seq=1,
                                         session_id="sar-session", generation=1)},
              ts=100.0, vid=SAR)
        self.assertEqual(store[SCOUT]["session"]["id"], "scout-session")
        self.assertEqual(store[SAR]["session"]["id"], "sar-session")
        # A retired-generation replay against Scout's own history must not touch SAR's, and
        # SAR's low generation number (1) must not be judged "retired" just because SCOUT's
        # happens to be higher — the two vehicles' generation counters are independent.
        ingest(store, {"companions": blk(fx.uav(101.0, link="LOST"), seq=1,
                                         session_id="scout-session", generation=6)},
              ts=101.0, vid=SCOUT)
        self.assertEqual(store[SAR]["session"]["generation"], 1, "SAR is untouched and unrejected")
        self.assertNotIn("retired_session_generation", store[SAR]["rejections"]["by_reason"])
        self.assertIn("retired_session_generation", store[SCOUT]["rejections"]["by_reason"])

    def test_generations_are_never_ordered_lexicographically_by_id(self):
        """A lexically SMALLER session id at a HIGHER generation is trusted; a lexically LARGER
        id at the SAME (not higher) generation is not what decides anything — only the
        generation NUMBER does."""
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, link="LOST"), seq=5,
                                         session_id="zzz-old", generation=1)}, ts=100.0)
        ingest(store, {"companions": blk(fx.uav(101.0, link="CONNECTED"), seq=1,
                                         session_id="aaa-new", generation=2)}, ts=101.0)
        self.assertEqual(store[SCOUT]["session"]["id"], "aaa-new")
        self.assertEqual(store[SCOUT]["companions"]["uav-1"]["link"]["state"], "CONNECTED")


# --- 4b. companion-session ordering persistence (survives an OPERATOR restart) ----------------

class SessionPersistence(unittest.TestCase):
    """companion_telemetry.session_snapshot / seed_session / validate_session_snapshot — the
    pure boundary main.py's _save_companion_sessions / _load_companion_sessions sit on. No
    FastAPI here: this module owns no file I/O (see its own docstring), so these tests exercise
    exactly what a restart needs without touching disk."""

    def test_snapshot_export_then_seed_reproduces_ordering_protection_with_no_companion_data(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, hazards=[fx.buoy(99.0)]), seq=7,
                                         session_id="s1", generation=3)}, ts=100.0)
        snapshot = ct.session_snapshot(store)
        self.assertEqual(snapshot, {SCOUT: {"id": "s1", "seq": 7, "generation": 3}})

        # A FRESH store — as if the operator process had just restarted — seeded from exactly
        # that snapshot, and NOTHING else: no companion, no hazard, no measurement survives.
        fresh = {}
        ct.seed_session(fresh, SCOUT, session_id="s1", seq=7, generation=3)
        self.assertEqual(fresh[SCOUT]["companions"], {}, "no companion data is restored")
        self.assertFalse(fresh[SCOUT]["reported"], "this process has not itself heard from Scout")
        view = ct.fleet_block(fresh, SCOUT, 100.0, 100.0)
        self.assertEqual(view["items"], [])
        self.assertIsNone(view["session"]["age_s"], "an honest 'unknown how long ago' — never a fabricated 0")

        # The retired-generation replay from BEFORE the (simulated) restart is still rejected.
        ingest(fresh, {"companions": blk(fx.uav(102.0), seq=99, session_id="s1", generation=2)},
              ts=102.0)
        self.assertEqual(fresh[SCOUT]["companions"], {}, "the retired generation stays rejected")
        self.assertIn("retired_session_generation", fresh[SCOUT]["rejections"]["by_reason"])
        # And the CURRENT generation continuing normally still applies.
        ingest(fresh, {"companions": blk(fx.uav(103.0), seq=8, session_id="s1", generation=3)},
              ts=103.0)
        self.assertEqual(list(fresh[SCOUT]["companions"]), ["uav-1"])

    def test_session_snapshot_excludes_vehicles_with_no_established_session(self):
        store = {SCOUT: ct.new_state(), SAR: ct.new_state()}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1, session_id="s", generation=1)},
              ts=100.0, vid=SCOUT)
        self.assertEqual(ct.session_snapshot(store), {SCOUT: {"id": "s", "seq": 1, "generation": 1}})

    def test_validate_session_snapshot_fails_closed_on_a_corrupt_or_unrecognised_file(self):
        for bad in (
            "not a dict",
            {"version": 999, "vehicles": {}},                                  # wrong version
            {"version": 1, "vehicles": "not a dict"},
            {"version": 1, "vehicles": {"usv-2": "not a dict"}},
            {"version": 1, "vehicles": {"usv-2": {"id": "s", "seq": 1}}},       # no generation
            {"version": 1, "vehicles": {"usv-2": {"id": "s", "seq": -1, "generation": 1}}},
        ):
            with self.assertRaises(ValueError):
                ct.validate_session_snapshot(bad)

    def test_validate_session_snapshot_accepts_a_well_formed_file(self):
        data = {"version": 1, "vehicles": {"usv-2": {"id": "s1", "seq": 7, "generation": 3}}}
        self.assertEqual(ct.validate_session_snapshot(data),
                         {"usv-2": {"id": "s1", "seq": 7, "generation": 3}})


# --- 5. duplicates and out-of-order (per-hazard revision / per-measurement level) -------------

class Ordering(Base):
    def test_older_position_never_replaces_newer_and_duplicates_are_noops(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, obs_at=99.0), seq=1)}, ts=100.0)
        ingest(store, {"companions": blk(fx.uav(102.0, obs_at=70.0), seq=2)}, ts=102.0)
        c = store[SCOUT]["companions"]["uav-1"]
        self.assertEqual(c["position"]["observed_at"], 99.0)
        self.assertEqual(store[SCOUT]["rejections"]["by_reason"].get("stale_observation"), 3)
        before = store[SCOUT]["rejections"]["count"]
        ingest(store, {"companions": blk(fx.uav(104.0, obs_at=99.0), seq=3)}, ts=104.0)
        self.assertEqual(store[SCOUT]["rejections"]["count"], before, "equal time = duplicate")
        self.assertEqual(c["position"]["observed_at"], 99.0)

    def test_repeated_hazard_ids_collapse_to_the_highest_revision(self):
        t = time.time()
        hz = [fx._verified(t), fx._accepted(t), fx._verified(t), fx.buoy(t)]
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, hazards=hz))))
        hazards = self.only()["hazards"]
        self.assertEqual(len(hazards), 1)
        self.assertEqual(hazards[0]["revision"], 3)

    def test_lower_revision_later_never_regresses(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, hazards=[fx._verified(t)]))))
        self.post(fx.envelope(SCOUT, t + 1, companions=blk(fx.uav(t + 1, hazards=[fx.buoy(t)]),
                                                           seq=2)))
        h = self.only()["hazards"][0]
        self.assertEqual(h["revision"], 3)
        self.assertEqual(h["replan"]["state"], "VERIFIED")

    def test_same_revision_content_is_immutable(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, hazards=[fx.buoy(t)]))))
        changed = fx.buoy(t, disposition="ACCEPTED")          # same id + revision 1
        self.post(fx.envelope(SCOUT, t + 1, companions=blk(fx.uav(t + 1, hazards=[changed]),
                                                           seq=2)))
        self.assertEqual(self.only()["hazards"][0]["disposition"]["state"], "REPORTED")

    def test_replayed_older_packet_does_not_rewrite_companion_state(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, link="LOST"))))
        r = self.post(fx.envelope(SCOUT, t - 5, companions=blk(fx.uav(t - 5, link="CONNECTED"),
                                                              seq=2)))
        self.assertTrue(r["stale"])
        self.assertEqual(self.only()["link"]["state"], "LOST")

    def test_peer_contact_time_is_monotonic(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, peer_at=99.0), seq=1)}, ts=100.0)
        ingest(store, {"companions": blk(fx.uav(101.0, peer_at=50.0), seq=2)}, ts=101.0)
        lpc = store[SCOUT]["companions"]["uav-1"]["link"]["last_peer_contact"]
        self.assertEqual(lpc["observed_at"], 99.0)


# --- 6. explicit clear vs omission, and reliable REPEATED removal propagation ------------------

class ClearVsOmission(Base):
    def test_omitted_block_keeps_assignment_without_refreshing_it(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1)}, ts=100.0)
        ingest(store, {"telemetry": {"lat": 1}}, ts=130.0, rx=130.0)          # later packet, no block
        blk_out = ct.fleet_block(store, SCOUT, 130.0, 130.0)
        c = blk_out["items"][0]
        self.assertEqual(c["assignment_state"], "ASSIGNED")
        self.assertEqual(c["status_age_s"], 30.0, "omission does not refresh the claim")
        self.assertAlmostEqual(c["position"]["min_age_s"], 30.5, places=1)

    def test_null_block_is_treated_as_omission(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1)}, ts=100.0)
        ingest(store, {"companions": None}, ts=101.0)
        self.assertEqual(len(ct.fleet_block(store, SCOUT, 101.0, 101.0)["items"]), 1)

    def test_explicit_empty_items_clears_the_assignment(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t))))
        self.post(fx.envelope(SCOUT, t + 1, companions=blk(seq=2)))
        blk_out = self.row(SCOUT)["companions"]
        self.assertEqual(blk_out["items"], [])
        self.assertTrue(blk_out["cleared"])
        self.assertTrue(blk_out["reported"])

    def test_lost_first_clear_packet_then_a_later_repeated_clear_still_succeeds(self):
        """Part 3: 'send items: [] only once' was the WRONG instruction. Scout must keep
        resending its current (empty) truth; losing the first clear packet must not leave a
        stale assignment on screen forever, because the SECOND (repeated) clear still arrives
        and applies normally — nothing special has to happen for this to work."""
        t0 = time.time()
        _, build = fx.SCENARIOS["repeated_clear_after_drop"]
        seq = fx.next_seq()
        for k in range(3):
            envs = build(t0, t0 + k, k, seq)
            for env in envs:
                self.post(env)
        blk_out = self.row(SCOUT)["companions"]
        self.assertEqual(blk_out["items"], [], "cleared by the SECOND clear, despite the lost first one")
        self.assertTrue(blk_out["cleared"])

    def test_companion_missing_from_snapshot_is_removed(self):
        store = {}
        two = blk(fx.uav(100.0), fx.uav(100.0, companion_id="uav-2"), seq=1)
        ingest(store, {"companions": two}, ts=100.0)
        ingest(store, {"companions": blk(fx.uav(101.0), seq=2)}, ts=101.0)
        self.assertEqual(list(store[SCOUT]["companions"]), ["uav-1"])

    def test_hazard_omission_keeps_snapshot_removal_and_empty_list_clear(self):
        store = {}
        a = fx.buoy(99.0)
        b = {**fx.buoy(99.0), "hazard_id": "hz-debris-2"}
        ingest(store, {"companions": blk(fx.uav(100.0, hazards=[a, b]), seq=1)}, ts=100.0)
        item = fx.uav(110.0)                                          # no `hazards` key
        ingest(store, {"companions": blk(item, seq=2)}, ts=110.0)
        c = ct.fleet_block(store, SCOUT, 110.0, 110.0)["items"][0]
        self.assertEqual(len(c["hazards"]), 2, "omitted hazards are kept…")
        self.assertEqual(c["hazards_age_s"], 10.0, "…and visibly not refreshed")
        ingest(store, {"companions": blk(fx.uav(111.0, hazards=[a]), seq=3)}, ts=111.0)
        self.assertEqual([h["hazard_id"] for h in
                          ct.fleet_block(store, SCOUT, 111.0, 111.0)["items"][0]["hazards"]],
                         ["hz-buoy-1"], "absent from a snapshot = removed")
        ingest(store, {"companions": blk(fx.uav(112.0, hazards=[]), seq=4)}, ts=112.0)
        self.assertEqual(ct.fleet_block(store, SCOUT, 112.0, 112.0)["items"][0]["hazards"], [])

    def test_explicit_null_measurement_clears_only_that_measurement(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1)}, ts=100.0)
        item = fx.uav(101.0)
        item["position"] = None
        ingest(store, {"companions": blk(item, seq=2)}, ts=101.0)
        c = ct.fleet_block(store, SCOUT, 101.0, 101.0)["items"][0]
        self.assertIsNone(c["position"])
        self.assertIsNotNone(c["battery"])

    def test_malformed_transfer_is_not_interpreted_as_a_valid_empty_snapshot(self):
        """A structurally broken block (e.g. `items` truncated into something that is not a
        list) must never be read as '0 items = clear' — it is rejected wholesale instead."""
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1)}, ts=100.0)
        ingest(store, {"companions": {"schema": "companion-v2",
                                      "session": {"id": "t", "seq": 2}, "items": "oops"}},
              ts=101.0)
        self.assertEqual(list(store[SCOUT]["companions"]), ["uav-1"], "kept, not wiped")
        self.assertIn("items_not_list", store[SCOUT]["rejections"]["by_reason"])


# --- 7. validation and bounds ---------------------------------------------------------------

class Validation(Base):
    def _pos_rejected(self, **pos):
        store = {}
        item = fx.uav(100.0)
        item["position"].update(pos)
        ingest(store, {"companions": blk(item, seq=1)}, ts=100.0)
        self.assertIsNone(store[SCOUT]["companions"]["uav-1"]["position"], pos)

    def test_bad_coordinates_are_rejected(self):
        self._pos_rejected(lat=91.0)
        self._pos_rejected(lng=-181.0)
        self._pos_rejected(lat=0.0, lng=0.0)
        self._pos_rejected(lat=float("nan"))
        self._pos_rejected(lng=float("inf"))
        self._pos_rejected(lat="56.7")
        self._pos_rejected(lat=True)

    def _hazard_rejected(self, **overrides):
        store = {}
        hz = {**fx.buoy(99.0), **overrides}
        ingest(store, {"companions": blk(fx.uav(100.0, hazards=[hz]), seq=1)}, ts=100.0)
        self.assertEqual(store[SCOUT]["companions"]["uav-1"]["hazards"], {}, overrides)

    def test_bad_hazard_geometry_is_rejected(self):
        c = fx.BUOY
        many = [{"lat": c[0] + 0.0001 * math.sin(i / 70 * 2 * math.pi),
                 "lng": c[1] + 0.0001 * math.cos(i / 70 * 2 * math.pi)} for i in range(70)]
        bowtie = [{"lat": c[0], "lng": c[1]}, {"lat": c[0] + 0.001, "lng": c[1] + 0.001},
                  {"lat": c[0], "lng": c[1] + 0.001}, {"lat": c[0] + 0.001, "lng": c[1]}]
        line = [{"lat": c[0], "lng": c[1]}, {"lat": c[0] + 0.001, "lng": c[1]},
                {"lat": c[0] + 0.002, "lng": c[1]}]
        huge = fx.square(c, 4000.0)
        for ring in (many, bowtie, line, huge, [{"lat": c[0], "lng": c[1]}]):
            self._hazard_rejected(geometry={"type": "polygon", "ring": ring})
        self._hazard_rejected(geometry={"type": "circle", "lat": c[0], "lng": c[1]})
        self._hazard_rejected(geometry={"type": "point", "lat": 95.0, "lng": c[1]})
        self._hazard_rejected(exclusion={"type": "polygon", "ring": bowtie})
        self._hazard_rejected(exclusion={"type": "point", "lat": c[0], "lng": c[1]})
        self._hazard_rejected(uncertainty_m=5000.0)
        self._hazard_rejected(uncertainty_m=-1.0)
        self._hazard_rejected(revision=-1)
        self._hazard_rejected(hazard_id="Not An Id!")
        self._hazard_rejected(source=None)

    def test_valid_polygon_hazard_is_accepted(self):
        store = {}
        hz = {**fx.buoy(99.0), "geometry": {"type": "polygon", "ring": fx.square(fx.BUOY, 10)}}
        ingest(store, {"companions": blk(fx.uav(100.0, hazards=[hz]), seq=1)}, ts=100.0)
        h = ct.fleet_block(store, SCOUT, 100.0, 100.0)["items"][0]["hazards"][0]
        self.assertEqual(len(h["geometry"]["ring_latlng"]), 4)
        self.assertEqual(len(h["geometry"]["ring_latlng"][0]), 2)

    def test_oversized_hazard_list_keeps_last_known(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0, hazards=[fx.buoy(99.0)]), seq=1)}, ts=100.0)
        many = [{**fx.buoy(99.0), "hazard_id": f"hz-{i}"} for i in range(ct.MAX_HAZARDS + 1)]
        ingest(store, {"companions": blk(fx.uav(101.0, hazards=many), seq=2)}, ts=101.0)
        self.assertEqual(list(store[SCOUT]["companions"]["uav-1"]["hazards"]), ["hz-buoy-1"])
        self.assertEqual(store[SCOUT]["rejections"]["by_reason"]["too_many_hazards"], 1)

    def test_oversized_or_malformed_block_is_rejected_wholesale(self):
        store = {}
        ingest(store, {"companions": blk(fx.uav(100.0), seq=1)}, ts=100.0)
        five = blk(*[fx.uav(101.0, companion_id=f"uav-{i}") for i in range(5)], seq=2)
        ingest(store, {"companions": five}, ts=101.0)
        ingest(store, {"companions": {"schema": "companion-v9", "session": {"id": "t", "seq": 3},
                                      "items": []}}, ts=102.0)
        # Correct v2 schema, but `items` is not a list — a DIFFERENT failure than a bad schema
        # string, and must be counted separately.
        ingest(store, {"companions": {"schema": "companion-v2", "session": {"id": "t", "seq": 4},
                                      "items": {"a": 1}}}, ts=103.0)
        ingest(store, {"companions": "yes"}, ts=104.0)               # not even a dict
        self.assertEqual(list(store[SCOUT]["companions"]), ["uav-1"], "last-known kept")
        by = store[SCOUT]["rejections"]["by_reason"]
        self.assertEqual(by["too_many_companions"], 1)
        self.assertEqual(by["unsupported_schema"], 2)
        self.assertEqual(by["items_not_list"], 1)

    def test_nonfinite_values_cannot_break_the_fleet_endpoint(self):
        t = time.time()
        item = fx.uav(t)
        item["position"]["lat"] = float("nan")
        item["battery"]["remaining_pct"] = float("inf")
        body = json.dumps(fx.envelope(SCOUT, t, companions=blk(item)))  # NaN/Infinity tokens
        r = self.client.post("/agent/status", content=body,
                             headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)
        c = self.only()
        self.assertIsNone(c["position"])
        self.assertIsNone(c["battery"])


# --- 8. isolation ---------------------------------------------------------------------------

class Isolation(Base):
    def test_second_usv_never_shows_scouts_companion(self):
        t = time.time()
        _, build = fx.SCENARIOS["second_usv"]
        for env in build(t, t, 0, fx.next_seq()):
            self.post(env)
        self.assertEqual(len(self.items(SCOUT)), 1)
        sar_blk = self.row(SAR)["companions"]
        self.assertEqual(sar_blk["items"], [])
        self.assertFalse(sar_blk["reported"])

    def test_parent_mismatch_is_refused_and_never_rehomed(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, parent="usv-3"))))
        self.post(fx.envelope(SAR, t, companions=blk(fx.uav(t, parent="usv-2"))))
        self.assertEqual(self.items(SCOUT), [])
        self.assertEqual(self.items(SAR), [])
        self.assertEqual(self.row(SCOUT)["companions"]["rejections"]["by_reason"]["parent_mismatch"], 1)
        self.assertEqual(self.row(SAR)["companions"]["rejections"]["by_reason"]["parent_mismatch"], 1)

    def test_parent_accepts_any_spelling_of_its_own_identity(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, parent="Scout"))))
        self.assertEqual(self.only()["parent_id"], SCOUT)

    def test_other_vehicles_packets_do_not_refresh_scouts_companion(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t))))
        self.backdate(20)
        self.post(fx.sar(time.time()))
        self.assertGreaterEqual(self.only()["status_age_s"], 20)

    def test_same_companion_id_under_two_parents_stays_separate(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t, companions=blk(fx.uav(t, link="CONNECTED"))))
        self.post(fx.envelope(SAR, t, companions=blk(fx.uav(t, parent="usv-3", link="LOST"))))
        self.assertEqual(self.only(SCOUT)["link"]["state"], "CONNECTED")
        self.assertEqual(self.only(SAR)["link"]["state"], "LOST")
        self.assertEqual(self.only(SAR)["parent_id"], SAR)


# --- 9. no command / mission / planning side effects ----------------------------------------

class NoSideEffects(Base):
    def test_companion_reports_never_touch_commands_missions_or_drafts(self):
        t = time.time()
        self.post(fx.envelope(SCOUT, t))                       # first contact (events expected)
        drafts = Path(main.__file__).resolve().parent / "planning_drafts"
        snap = {
            "commands": json.dumps(main.commands, default=str, sort_keys=True),
            "original_missions": json.dumps(main.original_missions, default=str, sort_keys=True),
            "active_original": dict(main.active_original_by_vehicle),
            "events": len(main.event_log),
            "drafts": sorted(p.name for p in drafts.iterdir()) if drafts.is_dir() else None,
        }
        mission_before = self.row(SCOUT)["mission_status"]
        for i, hz in enumerate([fx.buoy(t), fx._accepted(t), fx._verified(t)]):
            ts = t + 1 + i
            self.post(fx.envelope(SCOUT, ts, companions=blk(fx.uav(ts, hazards=[hz]), seq=i + 1)))
        self.assertEqual(json.dumps(main.commands, default=str, sort_keys=True), snap["commands"])
        self.assertEqual(json.dumps(main.original_missions, default=str, sort_keys=True),
                         snap["original_missions"])
        self.assertEqual(dict(main.active_original_by_vehicle), snap["active_original"])
        self.assertEqual(len(main.event_log), snap["events"])
        self.assertEqual(sorted(p.name for p in drafts.iterdir()) if drafts.is_dir() else None,
                         snap["drafts"])
        self.assertEqual(self.row(SCOUT)["mission_status"], mission_before)
        self.assertEqual(self.client.get(f"/api/commands/{SCOUT}").json()["commands"], [])

    def test_module_imports_nothing_that_can_command(self):
        src = Path(ct.__file__).read_text(encoding="utf-8")
        for forbidden in ("import main", "requests", "commands", "mission_publish", "planning"):
            self.assertNotIn(forbidden, src.split('"""', 2)[2])


if __name__ == "__main__":
    unittest.main()
