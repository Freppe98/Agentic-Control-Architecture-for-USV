"""Backend tests for QUICK_MOVE — the Map's operator-authority "Move <vehicle> here".
Run:  python -m unittest tests.test_quick_move

These pin the properties the operator's safety story actually rests on, not the happy path:

  * Independence from missions. A quick move works with NO mission and NO geometry, and a
    stored mission is bit-for-bit unchanged by issuing, running and completing one.
  * Capability. Absence of a Scout advertisement means unsupported — being present in the
    fleet is never evidence a vehicle can perform a guided move.
  * Authority. The dispatch gate re-reads authority LIVE and fails closed on anything that
    is not a confirmed OPERATOR, including "could not read it".
  * Short-lived. A quick move is refused outright while DISCONNECTED (confirm:true cannot
    buy its way into the offline queue) and carries its own short TTL.
  * Correlation. A status for a superseded or unknown command id never reaches the display;
    a stale or terminal-regressing status for the CURRENT id is refused too.
  * Completion. COMPLETED is published as CONFIRMED only with BOTH arrival_confirmed and
    loiter_verified; a bare COMPLETED is a CLAIM, and delivery is never completion.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import main  # noqa: E402
import quick_move  # noqa: E402
import quick_move_fixture as fx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VID = 2
TARGET = {"lat": 56.70050, "lng": 13.00380}


class QuickMoveBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()
        main.quick_move_state_by_id.clear()
        main._last_quick_move_state.clear()
        main.comms_state_by_id[VID] = "CONNECTED"
        main.last_known_telemetry.pop(VID, None)
        main.last_known_groups.pop(VID, None)
        main.current_vehicle_state.pop(VID, None)
        main.latest_msg_ts_by_id.pop(VID, None)
        # The dispatch gate reads authority LIVE from Scout. These tests are about the
        # OPERATOR's logic, so the read is stubbed with an explicit, per-test value rather
        # than left to whatever a nonexistent Scout at the configured address answers.
        self._real_auth = main.read_control_authority
        self.authority = "OPERATOR"
        main.read_control_authority = lambda vid: (
            {"ok": True, "vehicle_id": vid, "available": True,
             "reachable": self.authority is not None, "authority": self.authority})

    def tearDown(self):
        main.read_control_authority = self._real_auth

    # --- helpers ---------------------------------------------------------------------
    def report(self, block, *, timestamp=None, armed=True, pos=(56.69950, 13.00215)):
        """Deliver one Scout status envelope through the REAL endpoint, so every test goes
        through the same ingest path production does (including the monotonic guard)."""
        env = fx.envelope(block, vid=VID, timestamp=timestamp, pos=pos, armed=armed)
        return self.client.post("/agent/status", json=env)

    def advertise(self, **kw):
        return self.report(fx.block(cap=fx.capability(**kw), active_status=None))

    _DEFAULT = object()

    def issue(self, target=_DEFAULT):
        """`target` is sentinel-defaulted, not `or`-defaulted: an empty dict is falsy, and
        an `or` here would quietly swap the very case a bad-target test is trying to send."""
        t = TARGET if target is self._DEFAULT else target
        return self.client.post("/api/commands", json={
            "vehicle_id": VID, "type": "QUICK_MOVE", "params": {"target": t}})

    def row(self):
        for v in self.client.get("/api/fleet/status").json():
            if v["id"] == VID:
                return v
        self.fail("vehicle %s missing from the fleet" % VID)

    def qm(self):
        return self.row()["quick_move"]


# ── Capability: absence means unsupported ──────────────────────────────────────────────
class CapabilityTests(QuickMoveBase):
    def test_a_vehicle_that_never_advertised_is_unsupported_and_refused(self):
        self.report(fx.block(cap=None, include_active=False))   # a packet, but no capability
        cap = self.qm()["capability"]
        self.assertFalse(cap["supported"])
        self.assertEqual(cap["source"], "absent")
        r = self.issue()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "not_reported")

    def test_being_in_the_fleet_is_not_a_capability(self):
        """The whole point of the advertisement: a vehicle that reports perfectly normal
        telemetry, is connected, armed and under OPERATOR authority is STILL refused until
        it says it can do this."""
        self.report({"schema": quick_move.SCHEMA})   # well-formed, advertises nothing
        self.assertEqual(self.issue().json()["code"], "not_reported")

    def test_explicit_unsupported_is_refused_with_scouts_own_reason(self):
        self.advertise(supported=False, reason="GUIDED is not available on this build.")
        r = self.issue()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "not_supported")
        self.assertIn("GUIDED is not available", r.json()["message"])

    def test_a_never_contacted_vehicle_carries_the_same_unsupported_shape(self):
        """A consumer must never have to branch on "has this key appeared yet"."""
        block = quick_move.empty_block()
        self.assertEqual(block["schema"], quick_move.SCHEMA)
        self.assertFalse(block["capability"]["supported"])
        self.assertIsNone(block["current"])

    def test_nav_not_ready_blocks_only_when_EXPLICITLY_false(self):
        self.advertise(nav_ready=False)
        self.assertEqual(self.issue().json()["code"], "nav_not_ready")
        # Absent (Scout said nothing) is NOT a readiness failure and must not be invented.
        main.quick_move_state_by_id.clear()
        self.advertise(nav_ready=None)
        self.assertEqual(self.issue().status_code, 200)


# ── Prerequisites re-checked at dispatch ───────────────────────────────────────────────
class DispatchGateTests(QuickMoveBase):
    def setUp(self):
        super().setUp()
        self.advertise()

    def test_the_happy_path_needs_no_mission_and_no_geometry(self):
        """No mission is loaded, no planning draft exists, no approved package — and the
        move is accepted anyway. This is the defining property of the feature."""
        r = self.issue()
        self.assertEqual(r.status_code, 200, r.text)
        cmd = r.json()["command"]
        self.assertEqual(cmd["type"], "QUICK_MOVE")
        self.assertEqual(cmd["params"]["target"], TARGET)
        self.assertEqual(cmd["params"]["schema"], quick_move.SCHEMA)

    def test_authority_must_be_a_confirmed_OPERATOR(self):
        self.authority = "LOCAL_AGENT"
        self.assertEqual(self.issue().json()["code"], "not_operator")

    def test_an_unreadable_authority_fails_closed_and_says_so(self):
        """Distinct from LOCAL_AGENT on purpose: "take control" is useless advice when the
        read itself failed."""
        self.authority = None
        body = self.issue().json()
        self.assertEqual(body["code"], "authority_unreadable")
        self.assertNotIn("Take Control", body["message"])

    def test_a_disarmed_vehicle_is_refused(self):
        self.report(fx.block(cap=fx.capability(), active_status=None), armed=False)
        self.assertEqual(self.issue().json()["code"], "disarmed")

    def test_an_unknown_armed_state_is_refused_separately(self):
        env = fx.envelope(fx.block(cap=fx.capability(), active_status=None), vid=VID)
        env["payload"]["telemetry"].pop("armed")
        main.last_known_groups.pop(VID, None)
        self.client.post("/agent/status", json=env)
        self.assertEqual(self.issue().json()["code"], "armed_unknown")

    def test_invalid_coordinates_are_refused(self):
        for bad in ({"lat": 91, "lng": 13}, {"lat": 56.7, "lng": 181},
                    {"lat": 0, "lng": 0}, {"lat": None, "lng": 13}, {}):
            with self.subTest(bad=bad):
                r = self.issue(bad)
                self.assertIn(r.status_code, (400, 409))
                self.assertEqual(r.json()["code"], "bad_target")

    def test_beyond_the_advertised_range_is_refused_with_both_numbers(self):
        self.advertise(max_distance_m=50.0)
        body = self.issue().json()
        self.assertEqual(body["code"], "too_far")
        self.assertIn("50 m Quick move range", body["message"])

    def test_the_ui_cannot_authorize_what_the_backend_refuses(self):
        """The dispatch gate is not a mirror of a UI check — it is the decision. A caller
        that skips the browser entirely hits exactly the same refusal."""
        self.authority = "RC"
        self.assertEqual(self.issue().json()["code"], "not_operator")


# ── Short-lived: never an offline queue ────────────────────────────────────────────────
class ShortLivedTests(QuickMoveBase):
    def setUp(self):
        super().setUp()
        self.advertise()

    def test_disconnected_is_refused_outright_and_confirm_cannot_override_it(self):
        main.comms_state_by_id[VID] = "DISCONNECTED"
        r = self.client.post("/api/commands", json={
            "vehicle_id": VID, "type": "QUICK_MOVE", "confirm": True,
            "params": {"target": TARGET}})
        self.assertEqual(r.status_code, 409)
        # NOT the generic needs_confirmation path that every other command type can take.
        self.assertEqual(r.json()["code"], "link_not_current")
        self.assertNotIn("needs_confirmation", r.json())
        self.assertEqual([c for c in main.commands if c["type"] == "QUICK_MOVE"], [])

    def test_partitioned_is_refused_too(self):
        main.comms_state_by_id[VID] = "PARTITIONED"
        self.assertEqual(self.issue().json()["code"], "link_not_current")

    def test_the_ttl_is_its_own_and_much_shorter_than_the_queue_default(self):
        self.assertLess(main.QUICK_MOVE_TTL_S, main.COMMAND_TTL_S)
        cmd = self.issue().json()["command"]
        from datetime import datetime
        span = (datetime.fromisoformat(cmd["expires_at"])
                - datetime.fromisoformat(cmd["created_at"])).total_seconds()
        self.assertAlmostEqual(span, main.QUICK_MOVE_TTL_S, delta=1)

    def test_an_expired_quick_move_is_never_delivered(self):
        cmd = self.issue().json()["command"]
        from datetime import datetime, timedelta, timezone
        main.expire_commands(datetime.now(timezone.utc) + timedelta(seconds=main.QUICK_MOVE_TTL_S + 1))
        self.assertEqual(main.commands_by_id[cmd["id"]]["status"], "EXPIRED")
        delivered = self.client.get("/agent/commands?usv_id=usv-2").json()["commands"]
        self.assertEqual([c for c in delivered if c["command_type"] == "QUICK_MOVE"], [])


# ── Delivery, retries, supersession ────────────────────────────────────────────────────
class DeliveryTests(QuickMoveBase):
    def setUp(self):
        super().setUp()
        self.advertise()

    def test_a_delivery_retry_reuses_the_SAME_command_id(self):
        cmd = self.issue().json()["command"]
        first = self.client.get("/agent/commands?usv_id=usv-2").json()["commands"]
        second = self.client.get("/agent/commands?usv_id=usv-2").json()["commands"]
        ids = [c["command_id"] for c in first if c["command_type"] == "QUICK_MOVE"]
        self.assertEqual(ids, [cmd["id"]])
        self.assertEqual([c["command_id"] for c in second
                          if c["command_type"] == "QUICK_MOVE"], ids)

    def test_a_second_move_supersedes_the_first_and_stops_it_being_delivered(self):
        first = self.issue().json()["command"]
        self.client.get("/agent/commands?usv_id=usv-2")          # claim it (QUEUED -> SENT)
        second = self.issue({"lat": 56.70200, "lng": 13.00900}).json()["command"]
        self.assertEqual(main.commands_by_id[first["id"]]["status"], "EXPIRED")
        delivered = [c["command_id"] for c in
                     self.client.get("/agent/commands?usv_id=usv-2").json()["commands"]
                     if c["command_type"] == "QUICK_MOVE"]
        self.assertEqual(delivered, [second["id"]])
        self.assertEqual(self.qm()["current"]["command_id"], second["id"])

    def test_a_delayed_status_for_the_superseded_move_never_restores_the_old_target(self):
        first = self.issue().json()["command"]
        second = self.issue({"lat": 56.70200, "lng": 13.00900}).json()["command"]
        # A buffered report about the FIRST move arrives inside a perfectly fresh envelope.
        self.report(fx.block(cap=fx.capability(), active_status=fx.active(
            first["id"], "MOVING", target=(TARGET["lat"], TARGET["lng"]),
            updated_at=time.time(), distance_remaining_m=88.0)))
        cur = self.qm()["current"]
        self.assertEqual(cur["command_id"], second["id"])
        self.assertIsNone(cur["status"], "the retired move must contribute no status")
        self.assertEqual(cur["requested_target"]["lng"], 13.00900)
        self.assertEqual(self.qm()["rejections"]["last_reason"], "superseded_status")

    def test_a_status_for_a_command_this_station_never_issued_is_refused(self):
        self.issue()
        self.report(fx.block(cap=fx.capability(), active_status=fx.active(
            "00000000-0000-0000-0000-000000000000", "MOVING", updated_at=time.time())))
        self.assertEqual(self.qm()["rejections"]["last_reason"], "unknown_command_id")
        self.assertIsNone(self.qm()["current"]["status"])

    def test_delivery_is_unconfirmed_until_scout_reports_the_id_back(self):
        cmd = self.issue().json()["command"]
        self.assertFalse(self.qm()["current"]["delivery_confirmed"])
        self.report(fx.block(cap=fx.capability(), active_status=fx.active(
            cmd["id"], "STARTING", updated_at=time.time())))
        self.assertTrue(self.qm()["current"]["delivery_confirmed"])


# ── Status correlation, ordering and completion ────────────────────────────────────────
class StatusTests(QuickMoveBase):
    def setUp(self):
        super().setUp()
        self.advertise()
        self.cmd = self.issue().json()["command"]
        # Anchored to the advertisement envelope so the first status is strictly newer.
        self._packet_ts = time.time()

    def next_packet_ts(self):
        """A strictly-increasing envelope timestamp for this test's vehicle."""
        self._packet_ts = max(getattr(self, "_packet_ts", 0.0) + 0.5, time.time())
        return self._packet_ts

    def status(self, state, *, packet_ts=None, **kw):
        """Deliver one movement status. The ENVELOPE timestamp always moves forward (that
        is main.py's guard, not this module's), while `updated_at` -- the OBSERVATION time
        this module orders on -- is the test's to choose, including backwards."""
        ts = self.next_packet_ts() if packet_ts is None else packet_ts
        kw.setdefault("updated_at", ts)
        self.report(fx.block(cap=fx.capability(),
                             active_status=fx.active(self.cmd["id"], state, **kw)),
                    timestamp=ts)
        return self.qm()["current"]["status"]

    def test_accepted_is_not_moving_and_moving_is_not_arrival(self):
        st = self.status("STARTING")
        self.assertEqual(st["state"], "STARTING")
        self.assertIsNone(st["completion"])
        st = self.status("MOVING", distance_remaining_m=40.0)
        self.assertEqual(st["distance_remaining_m"], 40.0)
        self.assertIsNone(st["completion"], "movement is never a completion")

    def test_completion_requires_arrival_AND_verified_loiter(self):
        st = self.status("COMPLETED", arrival_confirmed=True, loiter_verified=True)
        self.assertEqual(st["completion"], quick_move.COMPLETION_CONFIRMED)

    def test_a_bare_completed_is_a_claim_not_a_confirmation(self):
        st = self.status("COMPLETED")
        self.assertEqual(st["completion"], quick_move.COMPLETION_CLAIMED)

    def test_completed_with_arrival_but_no_verified_loiter_is_still_only_a_claim(self):
        st = self.status("COMPLETED", arrival_confirmed=True, loiter_verified=False)
        self.assertEqual(st["completion"], quick_move.COMPLETION_CLAIMED)

    def test_an_executed_command_result_alone_never_produces_a_completion(self):
        """The queue reaching EXECUTED means the move was delivered and commanded. It is not
        arrival, and nothing in the published block may turn into one because of it."""
        self.client.post("/agent/command_result", json={
            "command_id": self.cmd["id"], "status": "EXECUTED",
            "result": {"verified": True, "observed_mode": "GUIDED"}})
        self.assertEqual(main.commands_by_id[self.cmd["id"]]["status"], "EXECUTED")
        cur = self.qm()["current"]
        self.assertIsNone(cur["status"], "a command result is not a movement status")

    def test_an_out_of_order_status_is_refused(self):
        """A buffered OBSERVATION arriving inside a perfectly fresh envelope -- the case the
        outer envelope guard cannot catch, and the reason this module orders on its own
        `updated_at` rather than on delivery order."""
        ts1 = self.next_packet_ts()
        self.status("MOVING", packet_ts=ts1, updated_at=ts1 - 1.0, distance_remaining_m=40.0)
        ts2 = self.next_packet_ts()
        st = self.status("MOVING", packet_ts=ts2, updated_at=ts1 - 6.0,
                         distance_remaining_m=115.0)
        self.assertEqual(st["distance_remaining_m"], 40.0, "the older report must not apply")
        self.assertEqual(self.qm()["rejections"]["last_reason"], "stale_status")

    def test_a_terminal_state_is_never_walked_back_to_a_running_one(self):
        ts1 = self.next_packet_ts()
        self.status("CANCELLED", packet_ts=ts1, updated_at=ts1 - 1.0)
        # A STRICTLY NEWER observation, and still refused: the outcome already happened, so
        # a buffered running status must not resurrect the movement.
        ts2 = self.next_packet_ts()
        st = self.status("MOVING", packet_ts=ts2, updated_at=ts2, distance_remaining_m=90.0)
        self.assertEqual(st["state"], "CANCELLED")
        self.assertEqual(self.qm()["rejections"]["last_reason"], "terminal_state_regression")

    def test_a_target_scout_echoes_back_differently_is_flagged(self):
        st = self.status("MOVING", target=(56.70120, 13.00600), distance_remaining_m=60.0)
        self.assertTrue(st["target_mismatch"])
        self.assertGreater(st["target_offset_m"], quick_move.TARGET_MATCH_TOLERANCE_M)

    def test_scout_reporting_nothing_active_is_not_a_completion(self):
        self.status("MOVING", distance_remaining_m=40.0)
        self.report(fx.block(cap=fx.capability(), active_status=None))
        st = self.qm()["current"]["status"]
        self.assertEqual(st["state"], "MOVING", "silence never completes a move")
        self.assertIsNone(st["completion"])

    def test_the_freshness_model_is_the_stations_one_model(self):
        ts = self.next_packet_ts()
        st = self.status("MOVING", packet_ts=ts, updated_at=ts - 1.0)
        for key in ("observed_at", "min_age_s", "reporting_delay_s", "estimated_age_s",
                    "freshness_quality", "clock_anomaly"):
            self.assertIn(key, st["updated"])
        self.assertEqual(st["updated"]["freshness_quality"], "LOWER_BOUND")

    def test_a_replayed_older_envelope_changes_no_movement_state(self):
        """The outer per-vehicle monotonic guard applies here exactly as it does to
        telemetry — a buffered older packet must not rewrite the movement."""
        ts = self.next_packet_ts()
        self.status("MOVING", packet_ts=ts, updated_at=ts, distance_remaining_m=30.0)
        self.report(fx.block(cap=fx.capability(), active_status=fx.active(
            self.cmd["id"], "FAILED", updated_at=ts - 100.5, reason="old")),
            timestamp=ts - 100)
        self.assertEqual(self.qm()["current"]["status"]["state"], "MOVING")


# ── Cancellation via the ordinary LOITER command ───────────────────────────────────────
class CancellationTests(QuickMoveBase):
    def setUp(self):
        super().setUp()
        self.advertise()
        self.cmd = self.issue().json()["command"]

    def test_loiter_records_a_cancel_REQUEST_not_a_cancellation(self):
        self.client.post("/api/commands", json={"vehicle_id": VID, "type": "SET_MODE_LOITER"})
        cur = self.qm()["current"]
        self.assertIsNotNone(cur["cancel_requested"])
        self.assertIn("LOITER", cur["cancel_requested"]["reason"])
        self.assertIsNone(cur["status"], "the movement is not cancelled by the request alone")

    def test_loiter_stops_an_undelivered_quick_move_from_firing_afterwards(self):
        self.client.post("/api/commands", json={"vehicle_id": VID, "type": "SET_MODE_LOITER"})
        self.assertEqual(main.commands_by_id[self.cmd["id"]]["status"], "EXPIRED")
        delivered = [c["command_type"] for c in
                     self.client.get("/agent/commands?usv_id=usv-2").json()["commands"]]
        self.assertNotIn("QUICK_MOVE", delivered)

    def test_only_scout_ends_the_movement(self):
        self.client.post("/api/commands", json={"vehicle_id": VID, "type": "SET_MODE_LOITER"})
        self.report(fx.block(cap=fx.capability(), active_status=fx.active(
            self.cmd["id"], "CANCELLED", updated_at=time.time(),
            reason="Cancelled by an operator LOITER command.")))
        self.assertEqual(self.qm()["current"]["status"]["state"], "CANCELLED")

    def test_loiter_after_the_move_has_ended_is_not_a_stop_request(self):
        """Pressing LOITER after a move finished is a perfectly ordinary thing to do -- but
        it is not a stop request for THAT move, and recording it as one would leave a
        finished card reading "stop requested" indefinitely."""
        self.report(fx.block(cap=fx.capability(), active_status=fx.active(
            self.cmd["id"], "COMPLETED", updated_at=time.time(),
            arrival_confirmed=True, loiter_verified=True)))
        self.client.post("/api/commands", json={"vehicle_id": VID, "type": "SET_MODE_LOITER"})
        self.assertIsNone(self.qm()["current"]["cancel_requested"])

    def test_releasing_authority_cancels_the_queue_and_the_card(self):
        from datetime import datetime, timezone
        main.cancel_pending_commands(VID, datetime.now(timezone.utc), "authority released")
        self.assertEqual(main.commands_by_id[self.cmd["id"]]["status"], "EXPIRED")
        self.assertIsNotNone(self.qm()["current"]["cancel_requested"])


# ── Isolation: missions and other vehicles are untouched ───────────────────────────────
class IsolationTests(QuickMoveBase):
    def test_a_stored_mission_is_unchanged_by_a_whole_quick_move(self):
        stored = {k: dict(v) if isinstance(v, dict) else v
                  for k, v in getattr(main, "mission_store", {}).items()}
        self.advertise()
        cmd = self.issue().json()["command"]
        self.report(fx.block(cap=fx.capability(), active_status=fx.active(
            cmd["id"], "COMPLETED", updated_at=time.time(),
            arrival_confirmed=True, loiter_verified=True)))
        after = {k: dict(v) if isinstance(v, dict) else v
                 for k, v in getattr(main, "mission_store", {}).items()}
        self.assertEqual(stored, after, "a quick move must not touch the mission store")

    def test_quick_move_is_not_a_mission_write_command(self):
        """Pinned because the Map refetches the Pixhawk mission after a mission-write
        command: QUICK_MOVE must never be in that set, or every move would trigger a
        spurious mission readback."""
        path = os.path.join(os.path.dirname(__file__), "..", "operator", "lib",
                            "mission-refresh.js")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("QUICK_MOVE", src, "quick move is not a mission write")

    def test_two_vehicles_never_share_quick_move_state(self):
        other = 3
        main.comms_state_by_id[other] = "CONNECTED"
        self.advertise()
        cmd = self.issue().json()["command"]
        env = fx.envelope(fx.block(cap=fx.capability(), active_status=fx.active(
            cmd["id"], "MOVING", updated_at=time.time())), vid=other)
        self.client.post("/agent/status", json=env)
        rows = {v["id"]: v for v in self.client.get("/api/fleet/status").json()}
        # The OTHER vehicle reported a status for a command issued against VID. It is not
        # that vehicle's move, so it is refused there and it is untouched here.
        self.assertIsNone(rows[other]["quick_move"]["current"])
        self.assertEqual(rows[other]["quick_move"]["rejections"]["last_reason"],
                         "unknown_command_id")
        self.assertIsNone(rows[VID]["quick_move"]["current"]["status"])


# ── The station-level capability endpoint ──────────────────────────────────────────────
class CapabilityEndpointTests(QuickMoveBase):
    def test_the_station_endpoint_points_at_the_per_vehicle_one(self):
        caps = self.client.get("/api/commands/capabilities").json()["commands"]
        qm = caps["QUICK_MOVE"]
        self.assertTrue(qm["supported"])
        self.assertEqual(qm["per_vehicle_capability"], "fleet.quick_move.capability")
        self.assertFalse(qm["offline_queueable"])
        self.assertEqual(qm["ttl_s"], main.QUICK_MOVE_TTL_S)


# ── Pure-module properties ─────────────────────────────────────────────────────────────
class ModuleTests(unittest.TestCase):
    def test_latlng_refuses_arrays_and_null_island(self):
        self.assertIsNone(quick_move.latlng([56.7, 13.0]))
        self.assertIsNone(quick_move.latlng({"lat": 0, "lng": 0}))
        self.assertIsNone(quick_move.latlng({"lat": float("nan"), "lng": 13.0}))
        self.assertEqual(quick_move.latlng({"lat": 56.7, "lng": 13.0}),
                         {"lat": 56.7, "lng": 13.0})

    def test_a_terminal_move_clears_itself_from_the_live_display(self):
        store, vid = {}, 7
        quick_move.register_command(store, vid, command_id="c1", target={"lat": 56.7, "lng": 13.0},
                                    now_wall_s=1000.0, now_mono_s=1000.0)
        quick_move.ingest(store, vid, {"quick_move": {
            "schema": quick_move.SCHEMA,
            "active": {"command_id": "c1", "state": "COMPLETED", "updated_at": 999.0,
                       "target": {"lat": 56.7, "lng": 13.0},
                       "arrival_confirmed": True, "loiter_verified": True}}},
            packet_ts=1000.0, received_at=1000.0, received_mono=1000.0)
        soon = quick_move.fleet_block(store, vid, 1001.0, 1001.0)
        self.assertEqual(soon["current"]["status"]["state"], "COMPLETED")
        later = quick_move.fleet_block(store, vid, 2000.0,
                                       1000.0 + quick_move.TERMINAL_RETENTION_S + 1)
        self.assertIsNone(later["current"], "a finished move does not linger on the map")

    def test_an_unknown_schema_is_refused_wholesale(self):
        store, vid = {}, 7
        applied = quick_move.ingest(store, vid, {"quick_move": {"schema": "quick-move-v0"}},
                                    packet_ts=1.0, received_at=1.0, received_mono=1.0)
        self.assertFalse(applied)
        self.assertEqual(store[vid]["rejections"]["last_reason"], "unsupported_schema")
        self.assertFalse(store[vid]["reported"])

    def test_an_omitted_block_is_no_information_at_all(self):
        store, vid = {}, 7
        self.assertFalse(quick_move.ingest(store, vid, {}, packet_ts=1.0, received_at=1.0,
                                           received_mono=1.0))
        self.assertEqual(store, {}, "an absent block must not even create state")


if __name__ == "__main__":
    unittest.main()
