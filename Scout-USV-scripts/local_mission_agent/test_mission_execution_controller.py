"""
Standalone tests for mission_execution_controller.py -- the ORIGINAL mission
lifecycle FSM (Start / Pause / Resume / return completion).

    python3 test_mission_execution_controller.py

Uses a fake gateway (no HTTP / no MAVLink / no Pixhawk) and a scratch planning
package. Covers the full Start sequence and every failure mode, Pause/Resume and
sequence retention/continuation, the replanning handoff and write arbitration,
the return-to-Home arrival monitor and final LOITER, restart safety, and the
canonical status payload.
"""
import os
import tempfile
import unittest

import decision_snapshot as dsm
import mission_execution_config as me_cfg
import mission_execution_controller as mec
import planning_package as pp
import write_arbiter


# ── Fake gateway ──────────────────────────────────────────────────────────────
_HOME = {"latitude": 56.6490, "longitude": 12.8700}
_ROUTE = [
    {"latitude": 56.6500, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_OUTBOUND_TRANSIT},
    {"latitude": 56.6510, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
    {"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
]
_BOUNDARY = [[56.648, 12.868], [56.653, 12.868], [56.653, 12.872], [56.648, 12.872]]


class _Timeout(Exception):
    """A stand-in transient read timeout the fake gateway can raise (its class
    name contains 'Timeout', so the controller's _is_timeout recognises it)."""


def _store_verified_package(mission_id="m1", route=None, home=None,
                            navigable_boundary=None, home_corridor=None):
    """Persist a v1-structural, Pixhawk-verified planning package (immutable
    original + active), the way the real acceptance path (replan_api) does, and
    return its route content hash so the fake Pixhawk readback can match it."""
    route = _ROUTE if route is None else route
    home = _HOME if home is None else home
    navigable_boundary = _BOUNDARY if navigable_boundary is None else navigable_boundary
    pkg = pp.build_package(mission_id, route, home, usv_id="usv-2",
                           no_go_zones=[], navigable_boundary=navigable_boundary,
                           home_corridor=home_corridor)
    pkg["route_hash"] = pkg["original_route_hash"]
    pkg["mission_revision"] = 0
    pkg["immutable"] = True
    pp.store_accepted(pkg, pkg["route_hash"], {"source": "test"})
    return pkg["route_hash"]


class FakeGateway:
    def __init__(self):
        self.authority = "LOCAL_AGENT"
        self.authority_values = None       # optional list, popped per call
        self.authority_raises = False
        self.mode_name = "LOITER"
        self.armed = True
        self.lat = 56.6490
        self.lon = 12.8700
        self.position_age_s = 0.5
        # Battery percent reported by read_vehicle_state's telemetry -- a
        # healthy default so every existing test's mission-energy-feasibility
        # Start gate evaluation passes without needing to know about it.
        # Tests exercising that gate directly override this (or use
        # experiment_injection) -- see test_mission_execution_controller_
        # feasibility_gate.py.
        self.battery = 55
        self.mission_id = "m1"             # vehicle_state.mission.current_mission_id
        self.current_seq = 2
        self.mission_count = 4
        self.mission_active = True
        # Three-valued MAVLink-derived running evidence (independent of the
        # operator-lifecycle mission_active flag). Default ACTIVE_TRUE so the
        # happy path proves progression via signal A; tests override to UNKNOWN
        # to exercise sequence-advance / movement proof.
        self.mission_active_evidence = "ACTIVE_TRUE"
        # Freshness/age (seconds) of the mission_active_evidence observation
        # (mission_progression.py's freshness-semantics correction: proof A
        # now requires a KNOWN, in-bound age -- an unreported age is
        # unprovable, not "assume fresh"). Fresh by default, mirroring a real
        # just-observed MISSION_CURRENT sample, so the default ACTIVE_TRUE
        # happy path keeps proving via signal A exactly as before.
        self.mission_active_evidence_age_s = 0.3
        self.heartbeat_age_s = 0.3         # fresh telemetry by default
        self.home_verified = True
        self.loiter_verified = True
        self.auto_verified = True
        # ARM knobs. command_arm returns arm_result if set; else a verified
        # default that also flips self.armed to arm_sets_armed (default True).
        self.arm_result = None
        self.arm_sets_armed = True
        self.set_home_result = None        # override; else a verified default
        self.auto_sets_seq = None          # if set, command_auto forces current_seq
        self.auto_sets_evidence = None     # if set, command_auto forces evidence
        # Optional per-read hook: on_state_read(gateway, call_index) may mutate
        # the gateway before the response is built -- used to script progression
        # (delayed arming / sequence advance / movement / mode blips).
        self.on_state_read = None
        self._state_reads = 0
        self.read_raises = False
        self.state_read_error = None       # optional exception (or list, popped) for read_vehicle_state
        # Fresh Pixhawk readback knobs. route_hash is injected by the fixture so
        # it matches the stored package by default.
        self.pixhawk_reachable = True
        self.pixhawk_partial = False
        self.pixhawk_mission_valid = True
        self.pixhawk_route_hash = None
        self.pixhawk_route_count = len(_ROUTE)
        self.pixhawk_mission_id = None     # Pixhawk carries no Operator msn- id
        self.pixhawk_error = None
        self.readback_error = None         # optional exception (or list, popped)
        # Coordinator proof/freshness envelope (GET /agent/pixhawk_mission is now
        # cache-first). Default is a VALID, fresh COORDINATED_CACHE proof so the
        # normal Start/READY path is exercised; tests override these knobs to
        # simulate a stale/refreshing/busy/aged/unattributed readback. Setting
        # pixhawk_proof_source=None drops the source entirely (the "no explicit
        # proof_source" rejection case). A field set to None is omitted.
        self.pixhawk_proof_source = pp.PROOF_SOURCE_CACHE
        self.pixhawk_proof_completed_at = None   # DIRECT_TRANSACTION completion time
        self.pixhawk_refresh_generation = 1
        self.pixhawk_cached = True
        self.pixhawk_stale = False
        self.pixhawk_refreshing = False
        self.pixhawk_busy = False
        self.pixhawk_observed_at = 1000.0
        self.pixhawk_age_s = 0.5
        self.calls = []

    # reads
    def current_authority(self):
        self.calls.append("auth")
        if self.authority_raises:
            raise RuntimeError("control_authority unreachable")
        if self.authority_values:
            return self.authority_values.pop(0)
        return self.authority

    def pixhawk_mission_readback(self):
        self.calls.append("pixhawk")
        err = self.readback_error
        if isinstance(err, list):
            err = err.pop(0) if err else None
        if err is not None:
            raise err
        rb = {
            "reachable": self.pixhawk_reachable,
            "partial": self.pixhawk_partial,
            "mission_valid": self.pixhawk_mission_valid,
            "route_content_hash": self.pixhawk_route_hash,
            "route_waypoint_count": self.pixhawk_route_count,
            "mission_id": self.pixhawk_mission_id,
            "error": self.pixhawk_error,
        }
        for key, val in (("proof_source", self.pixhawk_proof_source),
                         ("proof_completed_at", self.pixhawk_proof_completed_at),
                         ("refresh_generation", self.pixhawk_refresh_generation),
                         ("cached", self.pixhawk_cached), ("stale", self.pixhawk_stale),
                         ("refreshing", self.pixhawk_refreshing), ("busy", self.pixhawk_busy),
                         ("observed_at", self.pixhawk_observed_at), ("age_s", self.pixhawk_age_s)):
            if val is not None:
                rb[key] = val
        return rb

    def read_vehicle_state(self):
        self.calls.append("state")
        if self.on_state_read is not None:
            self.on_state_read(self, self._state_reads)
        self._state_reads += 1
        err = self.state_read_error
        if isinstance(err, list):
            err = err.pop(0) if err else None
        if err is not None:
            raise err
        if self.read_raises:
            raise RuntimeError("state unreachable")
        return {
            "usv_id": "usv-2",
            "telemetry": {"lat": self.lat, "lng": self.lon, "battery": self.battery,
                          "mode_name": self.mode_name, "armed": self.armed},
            "mavlink": {"heartbeat_age_s": self.heartbeat_age_s, "last_message_age_s": self.position_age_s},
            "mission": {"current_mission_id": self.mission_id, "mission_active": self.mission_active,
                        "mission_active_evidence": self.mission_active_evidence,
                        "mission_active_evidence_age_s": self.mission_active_evidence_age_s,
                        "current_waypoint": self.current_seq, "mission_count": self.mission_count},
            "agent": {"control_authority": self.authority,
                      "home_status": {"verified": self.home_verified, "ready_for_auto": self.home_verified,
                                      "home_position": dict(_HOME)}},
        }

    def home_status(self):
        self.calls.append("home_status")
        # ready_for_auto/ready_for_rtl mirror services/set_home_service.py's
        # get_home_status(), which always latches them identically (both gate
        # on the same `verified` latch -- see that module's docstring).
        return {"reachable": True, "verified": self.home_verified,
                "ready_for_auto": self.home_verified, "ready_for_rtl": self.home_verified,
                "home_position": dict(_HOME)}

    # writes
    def command_loiter(self):
        self.calls.append("loiter")
        if self.loiter_verified:
            self.mode_name = "LOITER"
        return {"verified": self.loiter_verified, "observed_mode": 5, "requested_mode": "LOITER"}

    def command_auto(self):
        self.calls.append("auto")
        if self.auto_verified:
            self.mode_name = "AUTO"
            self.mission_active = True
            if self.auto_sets_seq is not None:
                self.current_seq = self.auto_sets_seq
            if self.auto_sets_evidence is not None:
                self.mission_active_evidence = self.auto_sets_evidence
        return {"verified": self.auto_verified, "observed_mode": 10, "requested_mode": "AUTO"}

    def command_arm(self):
        self.calls.append("arm")
        if self.arm_result is not None:
            return dict(self.arm_result)
        if self.arm_sets_armed is not None:
            self.armed = self.arm_sets_armed
        return {"accepted": True, "verified": True, "armed": self.armed,
                "ack_result": "MAV_RESULT_ACCEPTED", "reason": None, "error": None}

    def set_home(self, command_id, tolerance_m=None, freshness_s=None):
        self.calls.append("set_home")
        if self.set_home_result is not None:
            return dict(self.set_home_result)
        self.home_verified = True
        return {"accepted": True, "verified": True,
                "home_position": {"latitude": self.lat, "longitude": self.lon},
                "requested_position": {"latitude": self.lat, "longitude": self.lon},
                "verification_distance_m": 1.2, "ack_result": "ACCEPTED", "error": None}

    # ── Stop-related writes ──────────────────────────────────────────────────
    def set_mission_current(self, seq):
        self.calls.append("set_current")
        self.rewind_target = seq
        # rewind_applies=True (default) actually resets the observed sequence;
        # False simulates an accepted ACK that never took effect (the mission was
        # NOT actually rewound) so the controller's fresh-sequence verification
        # fails closed.
        if getattr(self, "rewind_applies", True):
            self.current_seq = seq
        return {"status": f"Jumped to waypoint {seq}"}

    def upload_mission(self, route, command_id, upload_context="AGENT_STOP_RESTORE"):
        self.calls.append("upload")
        self.upload_route = route
        verified = getattr(self, "upload_verified", True)
        if verified and getattr(self, "upload_sets_hash", None) is not None:
            # A successful restore makes the installed mission read back as the
            # uploaded route (the original) on the next readback.
            self.pixhawk_route_hash = self.upload_sets_hash
            self.pixhawk_route_count = getattr(self, "upload_sets_count", len(route))
        return {"accepted": True, "verified": verified,
                "observed_route_content_hash": self.pixhawk_route_hash if verified else None,
                "observed_route_waypoint_count": len(route) if verified else None,
                "error": None if verified else {"code": "UPLOAD_FAILED", "message": "not verified"}}

    def set_control_authority(self, authority):
        self.calls.append("set_authority")
        self.authority_set_to = authority
        self.authority = authority
        return {"authority": authority}

    @property
    def write_calls(self):
        return [c for c in self.calls
                if c in ("loiter", "auto", "set_home", "arm", "set_current", "upload")]


def _cfg(**overrides):
    base = me_cfg.MissionExecutionConfig().to_dict()
    # Force tests to synchronous, unthrottled readiness so an observe()/
    # refresh_readiness() proves readiness in-line and deterministically (no
    # background thread, no polling delay). Callers can still override.
    base["readiness_poll_interval_s"] = 0.0
    base.update(overrides)
    return me_cfg.MissionExecutionConfig(**base)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class AdvancingClock:
    """A monotonic clock the progression-watch tests drive deterministically:
    __call__ returns the current time; advance(dt) is wired in as the watch's
    _sleep so each poll interval moves virtual time forward with no real sleep.
    Lets the full configured deadline be exercised instantly."""
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _Base(unittest.TestCase):
    def setUp(self):
        write_arbiter._reset_for_tests()
        self.dir = tempfile.mkdtemp()
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        self.route_hash = _store_verified_package("m1")
        self.gw = FakeGateway()
        self.gw.pixhawk_route_hash = self.route_hash

    def tearDown(self):
        write_arbiter._reset_for_tests()
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))

    def _ctrl(self, cfg=None, clock=None, **kw):
        return mec.MissionExecutionController(
            cfg=cfg or _cfg(), gateway=self.gw, clock=clock, **kw)

    def _snapshot(self, lat=56.6490, lon=12.8700, seq=3, count=4, mode="AUTO",
                  age=0.5, mission_id="m1", home=_HOME):
        vs = {
            "usv_id": "usv-2",
            "telemetry": {"lat": lat, "lng": lon, "battery": 55, "mode_name": mode, "armed": True},
            "mavlink": {"heartbeat_age_s": 0.3, "last_message_age_s": age},
            "mission": {"current_mission_id": mission_id, "mission_active": True,
                        "current_waypoint": seq, "mission_count": count},
            "agent": {"control_authority": "LOCAL_AGENT",
                      "home_status": {"verified": True, "ready_for_auto": True,
                                      "home_position": dict(home)}},
        }
        return dsm.build_snapshot(vs, "CONNECTED", "LOCAL_AGENT", planning_package=pp.load())


# ── Start ─────────────────────────────────────────────────────────────────────
class TestStart(_Base):
    def _ready_ctrl(self, **kw):
        ctrl = self._ctrl(**kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)  # promote NOT_READY -> READY
        return ctrl

    def test_full_start_sequence(self):
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["verified_mode"], "AUTO")
        states = [h["to"] for h in ctrl.status()["history"]]
        # New USV ordering: armed is verified FIRST (already armed -> VERIFYING_ARMED
        # with no ARM command), then LOITER-while-armed, Home, package, AUTO.
        self.assertEqual(states[-10:], [
            mec.START_REQUESTED, mec.VERIFYING_ARMED,
            mec.START_HOLD_REQUESTED, mec.START_HOLD_CONFIRMED,
            mec.SETTING_HOME, mec.VERIFYING_HOME, mec.SYNCHRONIZING_PACKAGE,
            mec.STARTING_AUTO, mec.CONFIRMING_PROGRESSION, mec.RUNNING,
        ])
        # Already armed -> no ARM; write order is LOITER -> SET_HOME -> AUTO.
        self.assertNotIn("arm", self.gw.calls)  # already armed -> idempotent
        self.assertEqual(self.gw.write_calls, ["loiter", "set_home", "auto"])
        # Progression proven via signal A (explicit ACTIVE_TRUE evidence).
        self.assertEqual(res["progression"]["proof"], "A")

    def test_home_synchronized_into_package(self):
        # Launch elsewhere -- close enough (~150 m) to stay within the default
        # mission-energy-feasibility budget (usable_range_m=3000, reserve=10%)
        # for this short fixture route, so this test still exercises Home
        # sync specifically rather than incidentally tripping the feasibility
        # gate (see test_mission_execution_controller_feasibility_gate.py for
        # the dedicated energy-feasibility Start tests).
        self.gw.lat, self.gw.lon = 56.6480, 12.8710  # launch elsewhere
        ctrl = self._ready_ctrl()
        ctrl.start("m1")
        pkg = pp.load()
        self.assertAlmostEqual(pkg["home"]["latitude"], 56.6480, places=4)
        self.assertAlmostEqual(pkg["home"]["longitude"], 12.8710, places=4)
        st = ctrl.status()
        self.assertAlmostEqual(st["verified_home"]["latitude"], 56.6480, places=4)
        self.assertIsNotNone(st["home_verification_distance_m"])

    def test_start_succeeds_with_null_vehicle_mission_id(self):
        # Task test 1: explicit requested id + matching package id + matching
        # Pixhawk hash succeeds EVEN WHEN vehicle current_mission_id is null (the
        # MAVLink mission carries no Operator msn- id). This is the bench bug.
        self.gw.mission_id = None
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["mission_id"], "m1")
        self.assertEqual(res["route_hash"], self.route_hash)

    def test_start_resolves_from_package_when_id_omitted(self):
        # Task test 2: a missing requested id resolves from the stored usable
        # package (the API contract makes the body id optional).
        self.gw.mission_id = None
        ctrl = self._ready_ctrl()
        res = ctrl.start()
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["mission_id"], "m1")

    def test_no_planning_package(self):
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        res = ctrl.start()
        self.assertEqual(res["error"]["code"], "NO_PLANNING_PACKAGE")
        self.assertEqual(self.gw.write_calls, [])

    def test_mission_id_mismatch_requested(self):
        ctrl = self._ready_ctrl()
        res = ctrl.start("a-different-id")
        self.assertEqual(res["error"]["code"], "MISSION_ID_MISMATCH")
        self.assertEqual(self.gw.write_calls, [])

    def test_vehicle_mission_id_conflict(self):
        # Task test 4: a NON-NULL vehicle current_mission_id that disagrees with
        # the resolved/package identity fails closed with MISSION_ID_CONFLICT,
        # before any write.
        self.gw.mission_id = "m-other"
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "MISSION_ID_CONFLICT")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(self.gw.write_calls, [])

    def test_start_succeeds_with_legacy_operator_mission_label(self):
        # Mission binding/reproof identity bug regression: Flask's legacy
        # /start_mission operator-typed sensor-logging label
        # ("<YYYY-MM-DD_HH-MM>_<name>") lives in a DIFFERENT identifier
        # namespace from the canonical msn-* planning-package mission id (see
        # _is_legacy_operator_mission_label) and must never be compared
        # byte-for-byte against it. An exact route-hash match must still
        # succeed even though the vehicle's reported current_mission_id is a
        # human-readable label, not the canonical id -- this is the exact
        # observed-state regression (package msn-183d11e892ff, vehicle label
        # "2026-08-20_11-54_biltema 1", identical route hash).
        self.route_hash = _store_verified_package("msn-183d11e892ff")
        self.gw.pixhawk_route_hash = self.route_hash
        self.gw.mission_id = "2026-08-20_11-54_biltema 1"
        ctrl = self._ready_ctrl()
        res = ctrl.start("msn-183d11e892ff")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["mission_id"], "msn-183d11e892ff")
        self.assertEqual(res["route_hash"], self.route_hash)

    def test_start_still_blocks_conflicting_canonical_vehicle_mission_id(self):
        # The namespace fix above must not become "ignore all mission-id
        # mismatches" (fail-closed preserved): a vehicle-reported id that IS
        # in the comparable (non-legacy-label) namespace and genuinely
        # disagrees with the resolved canonical identity still fails closed.
        self.route_hash = _store_verified_package("msn-183d11e892ff")
        self.gw.pixhawk_route_hash = self.route_hash
        self.gw.mission_id = "msn-000000000000"  # a DIFFERENT canonical id
        ctrl = self._ready_ctrl()
        res = ctrl.start("msn-183d11e892ff")
        self.assertEqual(res["error"]["code"], "MISSION_ID_CONFLICT")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(self.gw.write_calls, [])

    def test_pixhawk_hash_mismatch_fails_before_write(self):
        # Task test 5: matching package id but a mismatching fresh Pixhawk route
        # hash fails before any write.
        self.gw.pixhawk_route_hash = "sha256:" + "0" * 64
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertTrue(res["error"]["code"].startswith("ROUTE_HASH"))
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(self.gw.write_calls, [])

    def test_pixhawk_unreachable_fails_before_write(self):
        # Task test 6: an unreachable Pixhawk readback fails before any write.
        self.gw.pixhawk_reachable = False
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "PIXHAWK_UNAVAILABLE")
        self.assertEqual(self.gw.write_calls, [])

    def test_pixhawk_partial_fails_before_write(self):
        # Task test 6: a partial Pixhawk readback fails before any write.
        self.gw.pixhawk_partial = True
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "PIXHAWK_READBACK_PARTIAL")
        self.assertEqual(self.gw.write_calls, [])

    def test_stale_position(self):
        self.gw.position_age_s = 99.0
        ctrl = self._ready_ctrl()
        res = ctrl.start()
        self.assertEqual(res["error"]["code"], "POSITION_STALE_OR_INVALID")
        self.assertEqual(self.gw.write_calls, [])

    def test_null_island_position(self):
        self.gw.lat, self.gw.lon = 0.0, 0.0
        ctrl = self._ready_ctrl()
        res = ctrl.start()
        self.assertEqual(res["error"]["code"], "POSITION_STALE_OR_INVALID")

    def test_authority_blocked(self):
        # Task test 8: authority not LOCAL_AGENT prevents Start (before any write).
        self.gw.authority = "OPERATOR"
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.SUSPENDED)
        self.assertEqual(res["error"]["code"], "AUTHORITY_NOT_LOCAL_AGENT")
        self.assertNotIn("loiter", self.gw.calls)  # never wrote

    def test_loiter_failure_no_home_no_auto(self):
        self.gw.loiter_verified = False
        ctrl = self._ready_ctrl()
        res = ctrl.start()
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "LOITER_NOT_VERIFIED")
        self.assertNotIn("set_home", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)

    def test_set_home_failure_stays_loiter(self):
        self.gw.set_home_result = {"accepted": True, "verified": False,
                                   "home_position": None, "verification_distance_m": None,
                                   "error": {"code": "POSITION_STALE", "message": "stale"}}
        ctrl = self._ready_ctrl()
        res = ctrl.start()
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "SET_HOME_FAILED")
        self.assertNotIn("auto", self.gw.calls)
        # LOITER re-asserted -> at least two loiter commands (initial + restore).
        self.assertGreaterEqual(self.gw.calls.count("loiter"), 2)

    def test_home_readback_mismatch_distance(self):
        self.gw.set_home_result = {"accepted": True, "verified": True,
                                   "home_position": {"latitude": 56.7, "longitude": 12.9},
                                   "verification_distance_m": 500.0, "error": None}
        ctrl = self._ready_ctrl(cfg=_cfg(home_verification_tolerance_m=5.0))
        res = ctrl.start()
        self.assertEqual(res["error"]["code"], "SET_HOME_FAILED")
        self.assertNotIn("auto", self.gw.calls)

    def test_package_sync_failure_when_home_invalid(self):
        self.gw.set_home_result = {"accepted": True, "verified": True,
                                   "home_position": {"latitude": 0.0, "longitude": 0.0},
                                   "verification_distance_m": 1.0, "error": None}
        ctrl = self._ready_ctrl()
        res = ctrl.start()
        self.assertEqual(res["error"]["code"], "PACKAGE_SYNC_FAILED")
        self.assertNotIn("auto", self.gw.calls)

    def test_home_outside_navigable_boundary_rejects_start(self):
        # Launch just outside _BOUNDARY (west edge is lon 12.868), close enough
        # to stay within the mission-energy-feasibility budget so this test
        # exercises the geometry gate specifically, not the energy gate. A
        # verified launch Home that safe_return_planner could never prove a
        # retrace back to must refuse Start before AUTO -- this is exactly the
        # gap that let Scout launch into a mission whose only recovery was an
        # unconstrained native-RTL fallback.
        self.gw.lat, self.gw.lon = 56.6480, 12.8675
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "HOME_OUTSIDE_APPROVED_GEOMETRY")
        self.assertNotIn("auto", self.gw.calls)
        # LOITER re-asserted as the safe fallback hold (ensure_loiter=True).
        self.assertGreaterEqual(self.gw.calls.count("loiter"), 2)

    def test_home_outside_boundary_but_in_corridor_starts_normally(self):
        # Same out-of-boundary Home as above, but an approved home_corridor
        # containing it proves the connector -- Start must proceed, mirroring
        # safe_return_planner's own home_in_boundary-or-home_in_corridor gate.
        corridor = [[56.646, 12.865], [56.646, 12.869], [56.650, 12.869], [56.650, 12.865]]
        self.route_hash = _store_verified_package("m1", home_corridor=corridor)
        self.gw.pixhawk_route_hash = self.route_hash
        self.gw.lat, self.gw.lon = 56.6480, 12.8675
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)

    def test_auto_failure_stays_loiter(self):
        self.gw.auto_verified = False
        ctrl = self._ready_ctrl()
        res = ctrl.start()
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "AUTO_NOT_VERIFIED")
        self.assertGreaterEqual(self.gw.calls.count("loiter"), 2)  # LOITER restored

    def test_progression_unconfirmed(self):
        # AUTO verifies and mode stays AUTO/armed, but the mission never actually
        # progresses: no explicit active evidence, no sequence advance, no
        # movement. The watch must poll the FULL deadline, then restore LOITER.
        # (This is the real bench bug -- NOT a single early-exit sample.)
        clock = AdvancingClock(1000.0)
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"  # no explicit proof
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1                            # seq 1 already selected
        ctrl = self._ready_ctrl(cfg=_cfg(start_progression_timeout_s=10.0,
                                         progression_poll_interval_s=0.5),
                                clock=clock)
        ctrl._sleep = clock.advance
        # baseline seq is 1 (the launch-selected item); it never advances.
        self.gw.current_seq = 1
        res = ctrl.start()
        self.assertEqual(res["error"]["code"], "PROGRESSION_UNCONFIRMED")
        self.assertGreaterEqual(self.gw.calls.count("loiter"), 2)   # LOITER restored
        # Honoured the full deadline rather than failing at ~2-3 s.
        ev = res["error"]["detail"]
        self.assertGreaterEqual(ev["actual_elapsed_s"], 10.0)
        self.assertGreater(ev["sample_count"], 10)
        self.assertEqual(res["error"]["fallback_loiter_verified"], True)

    def test_duplicate_start_while_running_idempotent(self):
        ctrl = self._ready_ctrl()
        ctrl.start("m1")
        res2 = ctrl.start("m1")
        self.assertTrue(res2.get("idempotent"))
        self.assertEqual(res2["current_state"], mec.RUNNING)

    def test_disabled_by_config(self):
        ctrl = self._ready_ctrl(cfg=_cfg(mission_execution_enabled=False))
        res = ctrl.start()
        self.assertFalse(res["accepted"])
        self.assertEqual(res["error"]["code"], "MISSION_EXECUTION_DISABLED")

    def test_authority_lost_mid_start_suspends_in_loiter(self):
        # LOCAL_AGENT for the identity-proof read and the pre-LOITER gate, then
        # OPERATOR at the pre-Set-Home gate -> writes stop, vehicle left in
        # verified LOITER, no Set Home/AUTO.
        ctrl = self._ready_ctrl()   # promotes to READY with default authority
        self.gw.authority_values = ["LOCAL_AGENT", "LOCAL_AGENT", "OPERATOR"]
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.SUSPENDED)
        self.assertEqual(res["error"]["code"], "AUTHORITY_LOST")
        self.assertIn("loiter", self.gw.calls)
        self.assertNotIn("set_home", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        self.assertGreaterEqual(self.gw.calls.count("loiter"), 2)  # LOITER re-asserted


# ── Automatic ARM phase (task: Required tests / Automatic ARM) ─────────────────
class TestAutomaticArm(_Base):
    def _ready_ctrl(self, **kw):
        ctrl = self._ctrl(**kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        return ctrl

    def test_already_armed_does_not_resend_arm(self):
        self.gw.armed = True
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertNotIn("arm", self.gw.calls)

    def test_disarmed_sends_arm_once_and_verifies(self):
        self.gw.armed = False           # starts disarmed
        self.gw.arm_sets_armed = True   # ARM makes it armed, fresh telemetry proves it
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(self.gw.calls.count("arm"), 1)   # exactly one ARM intent
        # New USV ordering: ARM is the FIRST vehicle-changing write, and the full
        # write order is ARM -> LOITER -> SET_HOME -> AUTO.
        self.assertEqual(self.gw.write_calls, ["arm", "loiter", "set_home", "auto"])
        self.assertTrue(ctrl.status()["start_snapshot"]["auto_armed"])

    def test_disarmed_start_write_order_arm_first(self):
        # Explicit write-order invariant for a Start beginning DISARMED.
        self.gw.armed = False
        self.gw.arm_sets_armed = True
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        wc = self.gw.write_calls
        self.assertEqual(wc[0], "arm")                        # ARM is the FIRST write
        self.assertLess(wc.index("arm"), wc.index("loiter"))  # ARM before LOITER
        self.assertLess(wc.index("loiter"), wc.index("set_home"))
        self.assertLess(wc.index("set_home"), wc.index("auto"))
        # No AUTO before armed verified + LOITER verified + Home verified.
        states = [h["to"] for h in ctrl.status()["history"]]
        self.assertLess(states.index(mec.VERIFYING_ARMED), states.index(mec.STARTING_AUTO))
        self.assertLess(states.index(mec.START_HOLD_CONFIRMED), states.index(mec.STARTING_AUTO))
        self.assertLess(states.index(mec.VERIFYING_HOME), states.index(mec.STARTING_AUTO))

    def test_already_armed_start_write_order_loiter_first(self):
        # Explicit write-order invariant for a Start beginning ARMED: no ARM, and
        # the first write is LOITER.
        self.gw.armed = True
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertNotIn("arm", self.gw.calls)
        self.assertEqual(self.gw.write_calls, ["loiter", "set_home", "auto"])

    def test_arm_ack_without_armed_telemetry_fails(self):
        # Command 'verified' ack, but fresh telemetry never shows armed=true.
        self.gw.armed = False
        self.gw.arm_result = {"accepted": True, "verified": True, "armed": True,
                              "ack_result": "MAV_RESULT_ACCEPTED", "error": None}
        self.gw.arm_sets_armed = None   # do NOT flip self.armed -> telemetry stays false
        clock = AdvancingClock(1000.0)
        ctrl = self._ready_ctrl(cfg=_cfg(arm_verify_timeout_s=2.0,
                                         progression_poll_interval_s=0.5), clock=clock)
        ctrl._sleep = clock.advance
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "ARM_NOT_VERIFIED")
        # ARM is the FIRST write, so an unverified ARM blocks LOITER/Home/AUTO.
        self.assertNotIn("loiter", self.gw.calls)
        self.assertNotIn("set_home", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)

    def test_arm_rejection_fails_before_auto(self):
        self.gw.armed = False
        self.gw.arm_result = {"accepted": False, "verified": False, "armed": False,
                              "ack_result": "MAV_RESULT_FAILED", "reason": "prearm",
                              "error": {"code": "ACK_REJECTED", "message": "prearm failed"}}
        self.gw.arm_sets_armed = None
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "ARM_FAILED")
        # ARM rejected -> NEVER send LOITER (as a fake hold), Home, or AUTO.
        self.assertNotIn("loiter", self.gw.calls)
        self.assertNotIn("set_home", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)

    def test_arm_timeout_fails_before_auto(self):
        self.gw.armed = False
        self.gw.arm_result = {"accepted": False, "verified": False, "armed": False,
                              "ack_result": None, "error": None}
        self.gw.arm_sets_armed = None   # never becomes armed
        clock = AdvancingClock(1000.0)
        ctrl = self._ready_ctrl(cfg=_cfg(arm_verify_timeout_s=3.0,
                                         progression_poll_interval_s=0.5), clock=clock)
        ctrl._sleep = clock.advance
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "ARM_NOT_VERIFIED")
        # ARM timeout blocks all later Start writes.
        self.assertNotIn("loiter", self.gw.calls)
        self.assertNotIn("set_home", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)

    def test_unknown_armed_state_fails_closed(self):
        self.gw.armed = None            # telemetry.armed unavailable
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "ARM_STATE_UNAVAILABLE")
        # Fail closed BEFORE any vehicle write -- no ARM, no LOITER, no AUTO.
        self.assertEqual(self.gw.write_calls, [])

    def test_stale_armed_state_fails_closed(self):
        self.gw.armed = True
        self.gw.heartbeat_age_s = 99.0  # armed value is stale -> not fresh
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "ARM_STATE_UNAVAILABLE")
        self.assertEqual(self.gw.write_calls, [])

    def test_disarm_between_arm_and_loiter_fails_start(self):
        # Vehicle disarms AFTER armed verification but BEFORE the LOITER safety-
        # hold gate: a LOITER mode alone cannot physically hold a disarmed USV, so
        # Start fails closed and does NOT continue to Home/AUTO (and never
        # auto-disarms). State reads within start(): resolve(0), arm-check(1),
        # LOITER-hold verify(2) -- disarm at read >= 2.
        self.gw.armed = True
        ctrl = self._ready_ctrl()
        self.gw._state_reads = 0
        def disarm(gw, i):
            if i >= 2:
                gw.armed = False
        self.gw.on_state_read = disarm
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "DISARMED_BEFORE_LOITER")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertIn("loiter", self.gw.calls)      # LOITER was commanded...
        self.assertNotIn("set_home", self.gw.calls)  # ...but Start stops here
        self.assertNotIn("auto", self.gw.calls)
        self.assertNotIn("disarm", self.gw.calls)    # never auto-disarmed

    def test_failure_after_auto_arm_leaves_armed_and_loiter(self):
        # Auto-arm succeeds, but progression never confirms -> ARMED + verified
        # LOITER, never auto-disarmed.
        self.gw.armed = False
        self.gw.arm_sets_armed = True
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        clock = AdvancingClock(1000.0)
        ctrl = self._ready_ctrl(cfg=_cfg(start_progression_timeout_s=4.0,
                                         progression_poll_interval_s=0.5), clock=clock)
        ctrl._sleep = clock.advance
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "PROGRESSION_UNCONFIRMED")
        self.assertTrue(self.gw.armed)             # still ARMED (no auto-disarm)
        self.assertNotIn("disarm", self.gw.calls)  # never disarmed
        self.assertGreaterEqual(self.gw.calls.count("loiter"), 2)

    def test_duplicate_start_does_not_duplicate_arm(self):
        self.gw.armed = False
        self.gw.arm_sets_armed = True
        ctrl = self._ready_ctrl()
        ctrl.start("m1")                 # arms once, runs
        res2 = ctrl.start("m1")          # already running
        self.assertTrue(res2.get("idempotent"))
        self.assertEqual(self.gw.calls.count("arm"), 1)

    def test_restart_during_arm_does_not_retry(self):
        path = os.path.join(self.dir, "me_status.json")
        import json
        with open(path, "w") as f:
            json.dump({"state": mec.ARMING, "mission_id": "m1"}, f)
        store = mec.StatusStore(path=path)
        ctrl = mec.MissionExecutionController(cfg=_cfg(), gateway=self.gw, status_store=store)
        ctrl.recover_after_restart()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.FAILED)
        self.assertEqual(st["last_error"]["code"], "UNKNOWN_AFTER_RESTART")
        self.assertNotIn("arm", self.gw.calls)      # no blind ARM re-send
        self.assertNotIn("auto", self.gw.calls)


# ── Progression watch (task: Required tests / Progression) ────────────────────
class TestProgressionWatch(_Base):
    def _ready_ctrl(self, **kw):
        ctrl = self._ctrl(**kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        return ctrl

    def _watch_ctrl(self, timeout=10.0, poll=0.5):
        clock = AdvancingClock(1000.0)
        ctrl = self._ready_ctrl(cfg=_cfg(start_progression_timeout_s=timeout,
                                         progression_poll_interval_s=poll,
                                         progression_min_displacement_m=1.5),
                                clock=clock)
        ctrl._sleep = clock.advance
        # Reset the gateway read counter so an on_state_read hook set AFTER this
        # counts reads relative to start(): the pre-watch reads are exactly
        # resolve(0), arm-check(1), post-Home feasibility recheck(2), baseline(3)
        # -- the post-Home recheck (task: RTL Home / Start-readiness semantics
        # correction) added the one extra read between arm-check and baseline;
        # the watch's first sample is read index 4. So a hook firing at
        # `i >= 4 + k` fires at watch sample k.
        self.gw._state_reads = 0
        return ctrl, clock

    def test_transient_inactive_sample_is_retried_then_succeeds(self):
        # First few samples: UNKNOWN evidence, no advance. Later: sequence
        # advances -> proven via B. Must NOT fail at the first inactive sample.
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl()
        def advance_seq(gw, i):
            if i >= 6:                 # watch sample 2 -> sequence advances
                gw.current_seq = 2
        self.gw.on_state_read = advance_seq
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["progression"]["proof"], "B")
        self.assertGreater(res["progression"]["sample_count"], 1)

    def test_does_not_fail_early_before_deadline(self):
        # 10 s timeout: a never-progressing run must not fail at ~2-3 s.
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=10.0, poll=0.5)
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "PROGRESSION_UNCONFIRMED")
        self.assertGreaterEqual(res["error"]["detail"]["actual_elapsed_s"], 10.0)

    def test_full_deadline_honoured(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=6.0, poll=0.4)
        res = ctrl.start("m1")
        ev = res["error"]["detail"]
        self.assertGreaterEqual(ev["actual_elapsed_s"], 6.0)
        self.assertLess(ev["actual_elapsed_s"], 6.0 + 0.4 + 0.01)  # within one poll
        self.assertGreaterEqual(ev["sample_count"], 13)             # ~ 6.0 / 0.4

    def test_delayed_mission_active_true_succeeds(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl()
        def activate(gw, i):
            if i >= 6:                 # watch sample 2
                gw.mission_active_evidence = "ACTIVE_TRUE"
        self.gw.on_state_read = activate
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["progression"]["proof"], "A")

    def test_delayed_sequence_advance_succeeds(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl()
        def advance(gw, i):
            if i >= 7:                  # watch sample 3
                gw.current_seq = 2
        self.gw.on_state_read = advance
        res = ctrl.start("m1")
        self.assertEqual(res["progression"]["proof"], "B")

    def test_movement_toward_target_succeeds_when_active_unavailable(self):
        # mission_active unavailable, sequence static, but the vehicle moves a
        # meaningful distance TOWARD the current target -> proven via C.
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        # Launch well south of target route[0] (56.6500), then move north toward it.
        self.gw.lat, self.gw.lon = 56.6490, 12.8700
        ctrl, _ = self._watch_ctrl()
        def move_north(gw, i):
            if i >= 5:                 # watch sample 1 -> moved north toward target
                gw.lat = 56.6494        # ~44 m closer to 56.6500 target
        self.gw.on_state_read = move_north
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["progression"]["proof"], "C")

    def test_gps_jitter_does_not_prove_movement(self):
        # Sub-threshold jitter (<1.5 m) never proves progression.
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        def jitter(gw, i):
            gw.lat = 56.6490 + (0.000002 if i % 2 else -0.000002)  # ~0.2 m
        self.gw.on_state_read = jitter
        ctrl, _ = self._watch_ctrl(timeout=4.0)
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "PROGRESSION_UNCONFIRMED")

    def test_sequence_already_selected_does_not_prove(self):
        # Sequence 1 selected before AUTO and staying at 1 is NOT progression.
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=4.0)
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "PROGRESSION_UNCONFIRMED")

    def test_disarm_during_progression_fails_immediately(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, clock = self._watch_ctrl(timeout=10.0, poll=0.5)
        def disarm(gw, i):
            if i >= 4:                 # watch sample 1 -> disarms
                gw.armed = False
        self.gw.on_state_read = disarm
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "VEHICLE_DISARMED")
        # Failed WELL before the 10 s deadline.
        self.assertLess(res["error"]["detail"]["actual_elapsed_s"], 5.0)

    def test_authority_loss_during_progression_fails_immediately(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=10.0)
        def take_control(gw, i):
            if i >= 4:
                gw.authority = "OPERATOR"
        self.gw.on_state_read = take_control
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "AUTHORITY_LOST")

    def test_mode_leaving_auto_fails_immediately(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=10.0)
        def flip_mode(gw, i):
            if i >= 4:
                gw.mode_name = "MANUAL"
        self.gw.on_state_read = flip_mode
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "MODE_LEFT_AUTO")

    def test_explicit_auto_rejection_fails_immediately(self):
        self.gw.auto_verified = False
        ctrl, _ = self._watch_ctrl(timeout=10.0)
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "AUTO_NOT_VERIFIED")
        # Never entered the progression watch.
        self.assertNotEqual(ctrl.status()["state"], mec.CONFIRMING_PROGRESSION)

    def test_timeout_restores_verified_loiter(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=4.0)
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["fallback_loiter_verified"], True)
        self.assertEqual(self.gw.mode_name, "LOITER")

    def test_diagnostic_evidence_records_all_samples(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_seq = 1
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=4.0, poll=0.5)
        res = ctrl.start("m1")
        ev = res["error"]["detail"]
        self.assertIn("samples", ev)
        self.assertGreater(len(ev["samples"]), 1)
        s0 = ev["samples"][0]
        for key in ("elapsed_s", "armed", "mode_name", "mission_active_raw",
                    "mission_active_evidence", "current_sequence", "groundspeed",
                    "latitude", "longitude", "position_age_s", "distance_moved_m",
                    "authority", "mission_id"):
            self.assertIn(key, s0)
        # The controller also exposes the evidence on status().
        self.assertIsNotNone(ctrl.status()["progression_evidence"])

    def test_uses_shared_mission_progression_verifier(self):
        # Proves Start uses the ONE shared verifier (mission_progression.watch),
        # the same module the replan path delegates to -- not a private copy.
        import mission_progression as mp
        calls = {"n": 0}
        real = mp.watch
        def spy(ctx, baseline, timeout_s):
            calls["n"] += 1
            return real(ctx, baseline, timeout_s)
        mp.watch = spy
        try:
            ctrl = self._ready_ctrl()
            ctrl.start("m1")
        finally:
            mp.watch = real
        self.assertEqual(calls["n"], 1)


# ── Bound original mission (for the replan pre-replan proof, CRITICAL ISSUE 2) ──
class TestBoundOriginalMission(_Base):
    def _ready_ctrl(self, **kw):
        ctrl = self._ctrl(**kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        return ctrl

    def test_none_before_running(self):
        ctrl = self._ready_ctrl()
        self.assertIsNone(ctrl.bound_original_mission())

    def test_exposes_proven_identity_while_running(self):
        ctrl = self._ready_ctrl()
        ctrl.start("m1")
        bound = ctrl.bound_original_mission()
        self.assertIsNotNone(bound)
        self.assertEqual(bound["mission_id"], "m1")
        self.assertEqual(bound["original_route_hash"], self.route_hash)
        self.assertEqual(bound["original_route_count"], len(_ROUTE))


# ── Restart safety ────────────────────────────────────────────────────────────
class TestRestartSafety(_Base):
    def test_interrupted_start_recovered_failed(self):
        path = os.path.join(self.dir, "me_status.json")
        import json
        with open(path, "w") as f:
            json.dump({"state": mec.SETTING_HOME, "mission_id": "m1"}, f)
        store = mec.StatusStore(path=path)
        ctrl = mec.MissionExecutionController(cfg=_cfg(), gateway=self.gw, status_store=store)
        ctrl.recover_after_restart()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.FAILED)
        self.assertEqual(st["last_error"]["code"], "UNKNOWN_AFTER_RESTART")
        self.assertEqual(st["last_error"]["interrupted_state"], mec.SETTING_HOME)

    def test_stable_running_reconciles_end_to_end(self):
        # A real Start then a process restart: the persisted RUNNING is NOT trusted
        # verbatim -- it is reconciled against fresh evidence (still AUTO/armed/
        # matching/Home) and restored to authoritative RUNNING without any new
        # ARM/AUTO write.
        path = os.path.join(self.dir, "me_status.json")
        store = mec.StatusStore(path=path)
        ctrl = self._ctrl_with_store(store)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        ctrl.start("m1")
        store2 = mec.StatusStore(path=path)
        ctrl2 = mec.MissionExecutionController(cfg=_cfg(), gateway=self.gw, status_store=store2)
        self.gw.calls = []  # only count vehicle traffic DURING recovery
        ctrl2.recover_after_restart()
        st = ctrl2.status()
        self.assertEqual(st["state"], mec.RUNNING)
        self.assertEqual(st["effective_state"], mec.RUNNING)
        self.assertEqual(self.gw.write_calls, [])  # no ARM/AUTO re-issued
        self.assertTrue(st["recovery"]["reconciled"])

    def _ctrl_with_store(self, store):
        return mec.MissionExecutionController(cfg=_cfg(), gateway=self.gw, status_store=store)


# ── Restart reconciliation (task: persisted RUNNING must be re-proved) ─────────
class TestRestartReconciliation(_Base):
    """Persisted STABLE autonomous state (RUNNING/PAUSED/...) is evidence of the
    prior run, never proof of the current vehicle state. Each case seeds a
    persisted status file and asserts the fresh-evidence reconciliation outcome --
    and that recovery NEVER issues a vehicle write (no ARM/AUTO)."""

    def _seed(self, **fields):
        path = os.path.join(self.dir, "me_status.json")
        import json
        data = {"state": mec.RUNNING, "mission_id": "m1",
                "original_route_hash": self.route_hash,
                "active_route_hash": self.route_hash,
                "active_operation_id": "OLD-OP-708ead37",
                "verified_home": dict(_HOME), "home_verification_distance_m": 0.05}
        data.update(fields)
        with open(path, "w") as f:
            json.dump(data, f)
        return mec.StatusStore(path=path)

    def _recover(self, store, **gw):
        for k, v in gw.items():
            setattr(self.gw, k, v)
        ctrl = mec.MissionExecutionController(cfg=_cfg(), gateway=self.gw, status_store=store)
        self.gw.calls = []  # only count vehicle traffic DURING recovery
        ctrl.recover_after_restart()
        return ctrl

    # 1. Fresh matching AUTO/armed/mission/Home/package -> reconciles to RUNNING.
    def test_running_matching_evidence_reconciles_without_arm_auto(self):
        ctrl = self._recover(self._seed(), mode_name="AUTO", armed=True, home_verified=True)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RUNNING)
        self.assertEqual(st["effective_state"], mec.RUNNING)
        self.assertEqual(self.gw.write_calls, [])
        self.assertNotIn("arm", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        self.assertTrue(st["recovery"]["reconciled"])
        self.assertEqual(st["recovery"]["prior_state"], mec.RUNNING)
        self.assertIsNone(st["active_operation_id"])  # old id not resurrected

    # 2. Package mismatch while ARMED/AUTO -> definitive contradiction -> physical
    #    verified LOITER safe-hold -> SUSPENDED; never ARM/AUTO/disarm.
    def test_running_package_mismatch_armed_auto_safe_holds(self):
        self.route_hash = _store_verified_package("m2")  # stored package now for m2
        self.gw.pixhawk_route_hash = self.route_hash
        ctrl = self._recover(self._seed(mission_id="m1", original_route_hash="sha256:old",
                                        active_route_hash="sha256:old"),
                             mode_name="AUTO", armed=True)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)      # operator rearm required
        self.assertEqual(st["last_error"]["code"], "MISSION_ID_MISMATCH")
        self.assertEqual(self.gw.write_calls, ["loiter"])  # EXACTLY LOITER
        self.assertNotIn("arm", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        self.assertEqual(st["recovery"]["safe_hold"], "VERIFIED_LOITER")

    # 3. Hash mismatch while ARMED/AUTO -> physical verified LOITER -> SUSPENDED.
    def test_running_hash_mismatch_armed_auto_safe_holds(self):
        ctrl = self._recover(self._seed(), mode_name="AUTO", armed=True,
                             pixhawk_route_hash="sha256:0000000000000000")
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(self.gw.write_calls, ["loiter"])
        self.assertNotIn("arm", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)

    # 4. Home unverified while ARMED/AUTO -> physical verified LOITER -> SUSPENDED.
    def test_running_home_unverified_armed_auto_safe_holds(self):
        ctrl = self._recover(self._seed(), mode_name="AUTO", armed=True, home_verified=False)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "RECOVERY_HOME_UNVERIFIED")
        self.assertEqual(self.gw.write_calls, ["loiter"])

    # 5. Pixhawk unavailable -> TEMPORARY read gap -> zero writes, never a hold.
    def test_running_pixhawk_unavailable_zero_writes(self):
        ctrl = self._recover(self._seed(), mode_name="AUTO", armed=True,
                             pixhawk_reachable=False)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RECOVERY_PENDING)
        self.assertNotEqual(st["state"], mec.RUNNING)
        self.assertEqual(self.gw.write_calls, [])   # do not write when Pixhawk is down

    # 6. Vehicle now MANUAL (not autonomous) -> DEFINITIVE contradiction, but
    #    nothing under autonomous propulsion to hold: exits pending into the
    #    rearmable SUSPENDED state and does NOT LOITER.
    def test_running_now_manual_does_not_restore_or_hold(self):
        ctrl = self._recover(self._seed(), mode_name="MANUAL", armed=True, home_verified=True)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "RECOVERY_MODE_MISMATCH")
        self.assertEqual(self.gw.write_calls, [])

    # Vehicle FRESH DISARMED -> DEFINITIVE contradiction, nothing under propulsion:
    # exits pending into the rearmable SUSPENDED state, no LOITER (the reproduced
    # restart case: persisted RUNNING, fresh MANUAL/disarmed must not stay pending
    # and must not LOITER).
    def test_running_disarmed_does_not_restore_or_hold(self):
        ctrl = self._recover(self._seed(), mode_name="MANUAL", armed=False, home_verified=True)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "RECOVERY_DISARMED")
        self.assertEqual(self.gw.write_calls, [])

    # LOITER must be VERIFIED before the safe-hold (SUSPENDED) state is declared.
    def test_safe_hold_requires_verified_loiter(self):
        ctrl = self._recover(self._seed(), mode_name="AUTO", armed=True, home_verified=False,
                             loiter_verified=True)
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertIn("loiter", self.gw.calls)   # a verified LOITER was issued first

    # LOITER verification FAILURE -> stays an explicit recovery state, never SUSPENDED.
    def test_safe_hold_loiter_unverified_stays_recovery(self):
        ctrl = self._recover(self._seed(), mode_name="AUTO", armed=True, home_verified=False,
                             loiter_verified=False)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RECOVERY_PENDING)  # NOT SUSPENDED
        self.assertEqual(st["last_error"]["code"], "RECOVERY_SAFE_HOLD_UNVERIFIED")
        self.assertIn("loiter", self.gw.calls)               # hold was attempted
        self.assertNotIn("arm", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)

    # A contradiction where the vehicle is already in LOITER (not AUTO) is not
    # under autonomous propulsion, so it must NOT redundantly re-issue LOITER and
    # exits pending into the rearmable SUSPENDED state.
    def test_contradiction_already_loiter_no_redundant_hold(self):
        ctrl = self._recover(self._seed(), mode_name="LOITER", armed=True, home_verified=False)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "RECOVERY_MODE_MISMATCH")
        self.assertNotIn("loiter", self.gw.calls)   # already holding -> no redundant LOITER

    # Recovery NEVER auto-disarms -- not even on a physical safe-hold.
    def test_safe_hold_never_auto_disarms(self):
        ctrl = self._recover(self._seed(), mode_name="AUTO", armed=True, home_verified=False)
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertNotIn("disarm", self.gw.calls)
        self.assertEqual(self.gw.write_calls, ["loiter"])

    # 7. An old active_operation_id is never resurrected as active.
    def test_old_operation_id_not_resurrected(self):
        # Even on the happy path the persisted id is cleared, not carried forward.
        ctrl = self._recover(self._seed(), mode_name="AUTO", armed=True, home_verified=True)
        self.assertIsNone(ctrl.status()["active_operation_id"])
        # And on a fail-closed / safe-hold path.
        ctrl2 = self._recover(self._seed(), mode_name="AUTO", armed=True, home_verified=False)
        self.assertIsNone(ctrl2.status()["active_operation_id"])

    # A persisted PAUSED reconciles against a fresh LOITER hold (no write).
    def test_paused_matching_evidence_reconciles(self):
        ctrl = self._recover(self._seed(state=mec.PAUSED), mode_name="LOITER",
                             armed=True, home_verified=True)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.PAUSED)
        self.assertTrue(st["recovery"]["reconciled"])
        self.assertEqual(self.gw.write_calls, [])

    # RETURNING_HOME cannot be auto-reconciled; ARMED/AUTO -> physical safe-hold.
    def test_returning_home_not_reconcilable_armed_auto_safe_holds(self):
        ctrl = self._recover(self._seed(state=mec.RETURNING_HOME), mode_name="AUTO", armed=True)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "RECOVERY_NOT_RECONCILABLE")
        self.assertEqual(self.gw.write_calls, ["loiter"])

    # Recovery never sends ARM/AUTO merely because persisted state requested it:
    # even when the vehicle is disarmed AND not in AUTO, recovery stays read-only
    # (a definitive disarmed contradiction -> rearmable SUSPENDED, no writes).
    def test_recovery_never_issues_arm_or_auto(self):
        ctrl = self._recover(self._seed(), mode_name="LOITER", armed=False)
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertNotIn("arm", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        self.assertNotIn("loiter", self.gw.calls)
        self.assertNotIn("set_home", self.gw.calls)

    # armed UNKNOWN/stale (a fresh bool is unavailable) is a TEMPORARY read gap, not
    # a contradiction: stay RECOVERY_PENDING (retryable), never a hold, never SUSPENDED.
    def test_running_armed_unknown_stays_pending_transient(self):
        # A stale heartbeat makes armed evidence unavailable (None), not False.
        ctrl = self._recover(self._seed(), mode_name="AUTO", armed=True, home_verified=True,
                             heartbeat_age_s=999.0)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RECOVERY_PENDING)
        self.assertEqual(st["last_error"]["code"], "RECOVERY_ARMED_UNCONFIRMED")
        self.assertEqual(self.gw.write_calls, [])


class TestRecoveryRetry(_Base):
    """RECOVERY_PENDING caused by TEMPORARY/unavailable evidence at startup (the
    reproduced case: the vehicle Flask service on 127.0.0.1:8080 not up yet when
    the Local Agent starts after a reboot) must retry reconciliation automatically
    on a bounded cadence -- never stay permanently pending once evidence becomes
    available. Retry is driven from the main-loop tick (observe()); a 0 interval
    makes each observe() retry synchronously and deterministically."""

    def _seed(self, **fields):
        path = os.path.join(self.dir, "me_status_retry.json")
        import json
        data = {"state": mec.RUNNING, "mission_id": "m1",
                "original_route_hash": self.route_hash,
                "active_route_hash": self.route_hash,
                "active_operation_id": "OLD-OP-retry",
                "verified_home": dict(_HOME), "home_verification_distance_m": 0.05}
        data.update(fields)
        with open(path, "w") as f:
            json.dump(data, f)
        return mec.StatusStore(path=path)

    def _pending_8080_down(self, interval=0.0, **gw):
        """Recover with /agent/state connection-refused (8080 not up): lands in
        RECOVERY_PENDING with zero writes. Returns the controller, configured to
        retry at ``interval`` (0 = synchronous per observe())."""
        for k, v in gw.items():
            setattr(self.gw, k, v)
        self.gw.state_read_error = ConnectionError(
            "http://127.0.0.1:8080/agent/state: connection refused")
        ctrl = mec.MissionExecutionController(
            cfg=_cfg(recovery_retry_interval_s=interval), gateway=self.gw,
            status_store=self._seed(), clock=Clock())
        ctrl.recover_after_restart()
        self.gw.calls = []          # only count traffic AFTER the failed startup
        return ctrl

    # 1. Initial 8080 unavailable -> RECOVERY_PENDING, zero writes.
    def test_initial_8080_unavailable_pending_zero_writes(self):
        ctrl = self._pending_8080_down()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RECOVERY_PENDING)
        self.assertFalse(st["recovery"]["reconciled"])
        self.assertEqual(self.gw.write_calls, [])

    # 2. Later retry with matching evidence -> restored RUNNING, zero writes.
    def test_retry_matching_evidence_restores_running(self):
        ctrl = self._pending_8080_down()
        # 8080 comes up with fresh matching AUTO/armed/Home evidence.
        self.gw.state_read_error = None
        self.gw.mode_name = "AUTO"
        self.gw.armed = True
        self.gw.home_verified = True
        ctrl.observe(self._snapshot(mode="AUTO"), None)   # retry fires (interval 0)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RUNNING)
        self.assertTrue(st["recovery"]["reconciled"])
        self.assertEqual(self.gw.write_calls, [])
        self.assertNotIn("arm", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)

    # 3. Later retry with MANUAL + disarmed -> exits pending into the rearmable
    #    SUSPENDED state, zero writes (must NOT LOITER a disarmed vehicle).
    def test_retry_manual_disarmed_exits_to_suspended(self):
        ctrl = self._pending_8080_down()
        self.gw.state_read_error = None
        self.gw.mode_name = "MANUAL"
        self.gw.armed = False
        self.gw.home_verified = True
        ctrl.observe(self._snapshot(mode="MANUAL"), None)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "RECOVERY_DISARMED")
        self.assertEqual(self.gw.write_calls, [])

    # 4. Later retry with armed AUTO + mismatch -> verified LOITER -> SUSPENDED.
    def test_retry_armed_auto_mismatch_verified_loiter_suspended(self):
        ctrl = self._pending_8080_down()
        self.gw.state_read_error = None
        self.gw.mode_name = "AUTO"
        self.gw.armed = True
        self.gw.pixhawk_route_hash = "sha256:0000000000000000"   # hash mismatch
        ctrl.observe(self._snapshot(mode="AUTO"), None)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(self.gw.write_calls, ["loiter"])
        self.assertEqual(st["recovery"]["safe_hold"], "VERIFIED_LOITER")
        self.assertNotIn("arm", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)

    # 5. Retry stops after terminal reconciliation -- no further reconciliation
    #    reads once restored, and the state stays put.
    def test_retry_stops_after_terminal(self):
        ctrl = self._pending_8080_down()
        self.gw.state_read_error = None
        self.gw.mode_name = "AUTO"
        self.gw.armed = True
        self.gw.home_verified = True
        ctrl.observe(self._snapshot(mode="AUTO"), None)
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)
        # Further ticks must NOT run another reconciliation (no readback traffic).
        self.gw.calls = []
        for _ in range(3):
            ctrl.observe(self._snapshot(mode="AUTO"), None)
        self.assertNotIn("pixhawk", self.gw.calls)
        self.assertEqual(self.gw.write_calls, [])
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)

    # 6. HTTP /status stays responsive while a retry reconciliation read is in
    #    flight -- the ~2.5 s read runs off the tick and never holds the state lock.
    def test_status_responsive_during_retry(self):
        import threading
        import time as _time
        entered = threading.Event()
        release = threading.Event()

        def slow_read(gw, idx):
            entered.set()
            release.wait(2.0)      # block the reconciliation read in flight

        ctrl = self._pending_8080_down(interval=1.0)
        self.gw.state_read_error = None
        self.gw.mode_name = "AUTO"
        self.gw.armed = True
        self.gw.home_verified = True
        self.gw.on_state_read = slow_read
        ctrl.observe(self._snapshot(mode="AUTO"), None)   # spawns the retry thread
        self.assertTrue(entered.wait(2.0))                # retry is mid-read
        try:
            t0 = _time.time()
            st = ctrl.status()                            # must NOT block on the read
            elapsed = _time.time() - t0
            self.assertLess(elapsed, 0.5)
            self.assertEqual(st["state"], mec.RECOVERY_PENDING)  # read not done yet
        finally:
            release.set()

    # 7. No overlapping retries: while one retry is in flight, further ticks are
    #    no-ops (single-flight guard) -- exactly one reconciliation, no duplicate
    #    safe-hold writes.
    def test_no_overlapping_retries(self):
        import threading
        import time as _time
        reads = []
        reads_lock = threading.Lock()
        release = threading.Event()

        def slow_read(gw, idx):
            with reads_lock:
                reads.append(idx)
            release.wait(2.0)

        ctrl = self._pending_8080_down()   # synchronous retry (interval 0)
        self.gw.state_read_error = None
        self.gw.mode_name = "AUTO"
        self.gw.armed = True
        self.gw.pixhawk_route_hash = "sha256:0000000000000000"  # -> definitive hold
        self.gw.on_state_read = slow_read
        # Run the first (blocking) retry off-thread so we can fire more ticks while
        # it is in flight.
        first = threading.Thread(
            target=lambda: ctrl.observe(self._snapshot(mode="AUTO"), None))
        first.start()
        for _ in range(200):               # wait until the first read is in flight
            with reads_lock:
                if reads:
                    break
            _time.sleep(0.01)
        for _ in range(3):                 # concurrent ticks must be no-ops
            ctrl.observe(self._snapshot(mode="AUTO"), None)
        with reads_lock:
            self.assertEqual(len(reads), 1)   # exactly one reconciliation in flight
        release.set()
        first.join(2.0)
        self.assertFalse(first.is_alive())
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertEqual(self.gw.write_calls, ["loiter"])   # no duplicate holds


# ── Settled operations clear the active operation id (task: id semantics) ─────
class TestActiveOperationIdLifecycle(_Base):
    def _ready_ctrl(self, **kw):
        ctrl = self._ctrl(**kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        return ctrl

    def test_successful_start_clears_active_operation_id(self):
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertIsNotNone(res["operation_id"])          # returned for the caller
        self.assertIsNone(ctrl.status()["active_operation_id"])  # but not "active"

    def test_successful_pause_and_resume_clear_active_operation_id(self):
        ctrl = self._ready_ctrl()
        ctrl.start("m1")
        self.gw.current_seq = 2
        ctrl.pause()
        self.assertEqual(ctrl.status()["state"], mec.PAUSED)
        self.assertIsNone(ctrl.status()["active_operation_id"])
        ctrl.resume()
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)
        self.assertIsNone(ctrl.status()["active_operation_id"])


# ── Pause / Resume ────────────────────────────────────────────────────────────
class _Running(_Base):
    def _running_ctrl(self, **kw):
        ctrl = self._ctrl(**kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        ctrl.start("m1")
        return ctrl


class TestPause(_Running):
    def test_successful_pause(self):
        ctrl = self._running_ctrl()
        self.gw.current_seq = 2
        res = ctrl.pause()
        self.assertEqual(res["outcome"], mec.PAUSED)
        self.assertEqual(res["verified_mode"], "LOITER")
        self.assertEqual(ctrl.status()["sequence"]["before_pause"], 2)

    def test_pause_idempotent(self):
        ctrl = self._running_ctrl()
        ctrl.pause()
        res2 = ctrl.pause()
        self.assertTrue(res2.get("idempotent"))
        self.assertEqual(res2["current_state"], mec.PAUSED)

    def test_pause_retains_mission_and_sequence(self):
        ctrl = self._running_ctrl()
        self.gw.current_seq = 3
        ctrl.pause()
        st = ctrl.status()
        self.assertEqual(st["sequence"]["current"], 3)
        self.assertEqual(st["sequence"]["count"], 4)

    def test_pause_mission_not_loaded_fails(self):
        ctrl = self._running_ctrl()
        self.gw.mission_count = 0
        res = ctrl.pause()
        self.assertEqual(res["error"]["code"], "MISSION_NOT_LOADED")

    def test_pause_rejected_during_replanning(self):
        ctrl = self._running_ctrl()
        # Simulate replanning holding the write arbiter.
        tok = write_arbiter.acquire(write_arbiter.OWNER_REPLANNING)
        try:
            res = ctrl.pause()
        finally:
            write_arbiter.release(tok)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["error"]["code"], "REPLANNING_ACTIVE")


class TestResume(_Running):
    def _paused_ctrl(self, **kw):
        ctrl = self._running_ctrl(**kw)
        self.gw.current_seq = 2
        ctrl.pause()
        return ctrl

    def test_successful_resume_continuation(self):
        ctrl = self._paused_ctrl()
        self.gw.auto_sets_seq = 2  # continues from paused seq
        res = ctrl.resume()
        self.assertEqual(res["outcome"], mec.RUNNING)
        st = ctrl.status()
        self.assertEqual(st["sequence"]["at_resume"], 2)
        self.assertEqual(st["sequence"]["first_after_resume"], 2)
        self.assertTrue(st["sequence"]["continuation_verified"])

    def test_resume_idempotent_when_running(self):
        ctrl = self._running_ctrl()
        res = ctrl.resume()
        self.assertTrue(res.get("idempotent"))
        self.assertEqual(res["current_state"], mec.RUNNING)

    def test_resume_wrong_mission(self):
        ctrl = self._paused_ctrl()
        self.gw.mission_id = "m-other"
        n = len(self.gw.calls)
        res = ctrl.resume()
        self.assertEqual(res["error"]["code"], "WRONG_MISSION_LOADED")
        self.assertNotIn("auto", self.gw.calls[n:])  # resume never commanded AUTO

    def test_resume_auto_failure(self):
        ctrl = self._paused_ctrl()
        self.gw.auto_verified = False
        res = ctrl.resume()
        self.assertEqual(res["error"]["code"], "AUTO_NOT_VERIFIED")
        self.assertEqual(res["outcome"], mec.FAILED)

    def test_resume_sequence_restart_detected(self):
        ctrl = self._paused_ctrl()
        self.gw.auto_sets_seq = 0  # unexpected reset to the beginning
        res = ctrl.resume()
        self.assertEqual(res["outcome"], mec.RUNNING)  # AUTO verified
        st = ctrl.status()
        self.assertFalse(st["sequence"]["continuation_verified"])
        self.assertEqual(st["last_error"]["code"], "MISSION_SEQUENCE_RESTART_DETECTED")

    def test_resume_home_unverified(self):
        ctrl = self._paused_ctrl()
        self.gw.home_verified = False
        res = ctrl.resume()
        self.assertEqual(res["error"]["code"], "HOME_UNVERIFIED")

    def test_resume_blocked_when_authority_not_local_agent(self):
        # P0-2: Take Control (authority revoked to OPERATOR) before Resume's
        # AUTO write must block AUTO -- the vehicle stays in the paused
        # LOITER hold, never autonomously resumes once authority is OPERATOR.
        ctrl = self._paused_ctrl()
        self.gw.authority = "OPERATOR"
        n = len(self.gw.calls)
        res = ctrl.resume()
        self.assertEqual(res["outcome"], mec.SUSPENDED)
        self.assertEqual(res["error"]["code"], "AUTHORITY_LOST")
        self.assertNotIn("auto", self.gw.calls[n:])


# ── Replanning handoff ────────────────────────────────────────────────────────
class TestReplanHandoff(_Running):
    def test_running_to_returning_home_on_revised(self):
        ctrl = self._running_ctrl()
        far = self._snapshot(lat=56.70, lon=12.95)  # not yet at Home
        ctrl.observe(far, {"fsm_state": "MONITORING", "running": False})
        ctrl.observe(far, {"fsm_state": "MONITORING_REVISED", "running": False,
                           "revised_mission_hash": "sha256:abc"})
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RETURNING_HOME)
        self.assertEqual(st["active_route_hash"], "sha256:abc")

    def test_running_derived_replanning_and_blocks_ops(self):
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {"fsm_state": "PLANNING", "running": True})
        st = ctrl.status()
        self.assertEqual(st["effective_state"], "REPLANNING")
        self.assertTrue(st["replanning"]["active"])
        self.assertFalse(st["can_pause"])
        res = ctrl.pause()
        self.assertEqual(res["error"]["code"], "REPLANNING_ACTIVE")

    def test_replan_failure_suspends(self):
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {"fsm_state": "PLANNING", "running": True})
        ctrl.observe(self._snapshot(), {"fsm_state": "SAFE_HOLD", "running": False})
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "REPLANNING_NOT_SUCCESSFUL")

    # P0-3 regression / E3 field-run follow-up: run-20260820-150834-usv-2-
    # ae61e617 -- decision_policy requested a HOLD-only safety hold
    # (communication-loss-driven HIGH risk), replan_controller.
    # _direct_safe_hold() never attempted PLANNING/VALIDATING/UPLOAD, yet the
    # FSM lands on the SAME terminal SAFE_HOLD a genuine failed replan
    # attempt would. hold_only=True must produce a distinct, accurate
    # outcome -- never REPLANNING_NOT_SUCCESSFUL (implies an attempt was made
    # and failed), and -- since SAFE_HOLD means the physical hold was
    # POSITIVELY PROVEN -- never SUSPENDED either: a positively-proven,
    # deliberate hold-only outcome is a normal controlled pause, not an
    # execution failure (E3 physical field run: this must settle in PAUSED,
    # not SUSPENDED, so it is never mistaken for a failure requiring rearm).
    def test_hold_only_safe_hold_pauses_not_suspends(self):
        ctrl = self._running_ctrl()
        self.gw.mode_name = "LOITER"   # the hold physically settled
        self.gw.current_seq = 2
        # No PLANNING/VALIDATING/UPLOAD edge at all -- straight to SAFE_HOLD,
        # exactly like _direct_safe_hold()'s single-transaction path.
        ctrl.observe(self._snapshot(), {"fsm_state": "SAFE_HOLD", "running": False,
                                        "hold_only": True,
                                        "reason_codes": ["COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION"]})
        st = ctrl.status()
        self.assertEqual(st["state"], mec.PAUSED)
        self.assertNotEqual(st["state"], mec.SUSPENDED)
        # A positively-proven hold-only pause is NOT an error/failure.
        self.assertIsNone(st["last_error"])
        self.assertTrue(st["can_resume"])
        self.assertFalse(st["can_pause"])   # already PAUSED
        # A fresh vehicle-state read at the moment of the transition proves
        # the retained sequence -- the SAME evidence _run_pause() captures for
        # an operator-issued Pause -- not whatever the (possibly pre-settle)
        # snapshot passed into observe() showed.
        self.assertEqual(st["sequence"]["before_pause"], 2)
        self.assertEqual(st["sequence"]["current"], 2)
        reason = st["history"][-1]["reason"]
        self.assertIn("PAUSED", reason)
        self.assertIn("positively proven", reason)

    def test_genuine_safe_hold_after_attempted_planning_still_reports_replanning_failure(self):
        # Same terminal fsm (SAFE_HOLD), but reached via a genuine attempt
        # (PLANNING ran first, hold_only absent/False) -- REPLANNING_NOT_
        # SUCCESSFUL is still correct here; P0-3 must not blur this case.
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {"fsm_state": "PLANNING", "running": True,
                                        "hold_only": False})
        ctrl.observe(self._snapshot(), {"fsm_state": "SAFE_HOLD", "running": False,
                                        "hold_only": False})
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "REPLANNING_NOT_SUCCESSFUL")

    # ── P0 SAFE_HOLD-invariant follow-up: HOLD-only SUSPENDED (physical hold
    # never proven) must not be reported as a planner failure either ────────────
    # replan_controller._acquire_hold_settle()/_hold_not_proven() can now end a
    # HOLD-only transaction on SUSPENDED (HOLD_SETTLE_TIMEOUT /
    # LOITER_REASSERT_NOT_VERIFIED) instead of SAFE_HOLD. "No replan attempted"
    # is still true there -- no route was ever built/validated/uploaded -- so
    # REPLANNING_NOT_SUCCESSFUL (which implies an attempt was made and failed)
    # must never be reported for it. hold_only ALONE is the authoritative
    # signal: replan_controller.py's FSM structurally guarantees an
    # ACTION_REQUEST_HOLD transaction can never enter PLANNING/VALIDATING/
    # UPLOAD, so hold_only == True already proves zero replan attempts --
    # retry_count is NOT used (it counts RETRIES within the plan/validate/
    # upload loop, so retry_count == 0 is equally true of a genuine first
    # attempt that hasn't retried yet -- it is not evidence either way).
    def test_a_hold_only_safe_hold_still_pauses_not_suspends(self):
        # Task item 4A (E3 follow-up): hold_only + SAFE_HOLD (proven) settles
        # in PAUSED, never SUSPENDED and never REPLANNING_NOT_SUCCESSFUL,
        # from hold_only alone.
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SAFE_HOLD", "running": False,
            "hold_only": True,
            "reason_codes": ["COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION"],
        })
        st = ctrl.status()
        self.assertEqual(st["state"], mec.PAUSED)
        self.assertNotEqual(st["state"], mec.SUSPENDED)
        self.assertIsNone(st["last_error"])

    def test_b_hold_only_suspended_hold_settle_timeout_is_not_replanning_failure(self):
        # Task item 4B: SUSPENDED via HOLD_SETTLE_TIMEOUT must NOT say
        # REPLANNING_NOT_SUCCESSFUL, and must accurately name the physical
        # hold-proof failure. retry_count is deliberately OMITTED from this
        # status payload -- classification must not depend on it being present.
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SUSPENDED", "running": False,
            "hold_only": True,
            "last_error": {
                "code": "HOLD_SETTLE_TIMEOUT",
                "message": ("HOLD-SETTLE not proven within 10.0s (last observed groundspeed "
                            "0.63 m/s vs threshold 0.5 m/s, last reason ARMED_LOITER_GROUNDSPEED_TOO_HIGH); "
                            "vehicle remains in verified LOITER."),
            },
            "reason_codes": ["COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION"],
        })
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertNotEqual(st["last_error"]["code"], "REPLANNING_NOT_SUCCESSFUL")
        self.assertEqual(st["last_error"]["code"], "HOLD_SETTLE_TIMEOUT")
        self.assertIn("physical hold could not be positively proven", st["last_error"]["message"])
        self.assertEqual(st["last_error"]["reason_codes"],
                         ["COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION"])

    def test_c_hold_only_suspended_loiter_reassert_not_verified_is_accurate(self):
        # Task item 4C: SUSPENDED via LOITER_REASSERT_NOT_VERIFIED (the final
        # defensive re-assert itself failed to verify) must be equally
        # accurate, never REPLANNING_NOT_SUCCESSFUL.
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SUSPENDED", "running": False,
            "hold_only": True,
            "last_error": {
                "code": "LOITER_REASSERT_NOT_VERIFIED",
                "message": ("HOLD-SETTLE was proven, but the final defensive LOITER "
                            "re-assertion could not be verified immediately before "
                            "certifying the hold; failing closed instead of certifying "
                            "an unproven hold."),
            },
        })
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertNotEqual(st["last_error"]["code"], "REPLANNING_NOT_SUCCESSFUL")
        self.assertEqual(st["last_error"]["code"], "LOITER_REASSERT_NOT_VERIFIED")
        self.assertIn("physical hold could not be positively proven", st["last_error"]["message"])

    def test_d_genuine_attempted_replan_failure_still_reports_replanning_not_successful(self):
        # Task item 4D: a genuine attempted-and-failed replan (hold_only=False)
        # still reports REPLANNING_NOT_SUCCESSFUL.
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {"fsm_state": "PLANNING", "running": True})
        ctrl.observe(self._snapshot(), {"fsm_state": "SAFE_HOLD", "running": False,
                                        "hold_only": False, "retry_count": 2})
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "REPLANNING_NOT_SUCCESSFUL")

    def test_retry_count_is_not_consulted_for_hold_only_classification(self):
        # retry_count must NOT be evidence: a status payload reporting
        # hold_only=True + SUSPENDED + HOLD_SETTLE_TIMEOUT with a (structurally
        # impossible, but deliberately adversarial) nonzero retry_count must
        # STILL classify as hold-proof-failed -- retry_count is simply never
        # read for this decision.
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SUSPENDED", "running": False,
            "hold_only": True, "retry_count": 3,
            "last_error": {"code": "HOLD_SETTLE_TIMEOUT", "message": "HOLD-SETTLE not proven"},
        })
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "HOLD_SETTLE_TIMEOUT")
        self.assertNotEqual(st["last_error"]["code"], "REPLANNING_NOT_SUCCESSFUL")

    def test_authority_lost_during_hold_only_transaction_is_not_reclassified(self):
        # Narrow-scope guard: hold_only=True + SUSPENDED but a code OUTSIDE
        # the hold-proof-failure set (e.g. a genuine authority loss
        # before/while LOITER was ever requested, replan_controller's own
        # _suspend()/AUTHORITY_LOST) must NOT be reclassified -- only
        # HOLD_SETTLE_TIMEOUT/LOITER_REASSERT_NOT_VERIFIED are. Authority-loss
        # diagnostic wording is unchanged by this fix.
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SUSPENDED", "running": False,
            "hold_only": True,
            "last_error": {"code": "AUTHORITY_LOST",
                           "message": "Control authority is not LOCAL_AGENT before commanding LOITER."},
        })
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertEqual(st["last_error"]["code"], "REPLANNING_NOT_SUCCESSFUL")

    def test_arbiter_prevents_simultaneous_writes(self):
        # A mission-execution operation holds the arbiter for its duration; while
        # held, a REPLANNING acquire fails -> the two cannot write at once.
        self.assertIsNone(write_arbiter.current_owner())
        tok = write_arbiter.acquire(write_arbiter.OWNER_MISSION_EXECUTION)
        self.assertIsNone(write_arbiter.acquire(write_arbiter.OWNER_REPLANNING))
        write_arbiter.release(tok)
        self.assertIsNotNone(write_arbiter.acquire(write_arbiter.OWNER_REPLANNING))


# ── Communication-loss hold-only pause (E3 physical field-run fix) ────────────
# A hold-only REQUEST_HOLD whose physical hold is POSITIVELY PROVEN (fsm
# SAFE_HOLD) must settle in PAUSED -- a normal controlled pause reusing the
# EXACT SAME state/Resume/Stop semantics an operator-issued Pause already has
# -- never SUSPENDED. Reconnection (comm CONNECTED) must never itself trigger
# AUTO; only an explicit operator Resume may. An UNPROVEN hold (hold-settle
# timeout / LOITER re-assert failure) and a genuinely attempted-and-failed
# replan both still fail closed to SUSPENDED, exactly as before this fix.
class TestHoldOnlyPause(_Running):
    def _hold_paused_ctrl(self, **kw):
        """RUNNING -> hold-only REQUEST_HOLD -> proven SAFE_HOLD -> PAUSED,
        the same communication-loss path the E3 field run exercised. Records
        the gw.calls length right after Start (before the hold-only
        transition) as self.calls_before_hold, so callers can assert on
        vehicle traffic from the hold-only transition onward, excluding
        Start's own ARM/LOITER/SET_HOME/AUTO sequence."""
        ctrl = self._running_ctrl(**kw)
        self.calls_before_hold = len(self.gw.calls)
        self.gw.mode_name = "LOITER"   # the hold physically settled
        self.gw.current_seq = 2
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SAFE_HOLD", "running": False,
            "hold_only": True,
            "reason_codes": ["COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION"],
        })
        self.assertEqual(ctrl.status()["state"], mec.PAUSED)
        return ctrl

    # 1. RUNNING + comm DISCONNECTED -> HIGH/HOLD -> REQUEST_HOLD -> LOITER
    # confirmed -> hold settle confirmed -> SAFE_HOLD -> mission execution
    # PAUSED (decision_policy/risk_model/comm classifier are exercised by
    # their own dedicated tests; here the replan controller's proven SAFE_HOLD
    # outcome is the boundary this controller reacts to).
    def test_running_comm_loss_hold_settles_to_paused(self):
        ctrl = self._hold_paused_ctrl()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.PAUSED)
        self.assertIsNone(st["last_error"])
        self.assertEqual(st["replanning"]["fsm_state"], "SAFE_HOLD")
        self.assertFalse(st["replanning"]["active"])
        # The hold-only pause path never itself commands AUTO -- LOITER was
        # already commanded/settled by replan_controller (out of scope here);
        # this controller only reacts to the proven SAFE_HOLD outcome.
        self.assertNotIn("auto", self.gw.calls[self.calls_before_hold:])

    # 2. + 9. Reconnection: PAUSED + LOITER + comm CONNECTED -> remains
    # PAUSED, no AUTO write, no matter how many further steady-state polls
    # observe comm having recovered (mission-execution never itself consumes
    # comm_state -- the replan FSM's own trigger-generation latch is what
    # keeps a recovered-but-still-SAFE_HOLD condition from restarting a
    # transaction; see replan_controller.py, unmodified here).
    def test_reconnection_alone_never_auto_resumes(self):
        ctrl = self._hold_paused_ctrl()
        n = len(self.gw.calls)
        for _ in range(20):
            ctrl.observe(self._snapshot(mode="LOITER"), {
                "fsm_state": "SAFE_HOLD", "running": False, "hold_only": True,
            })
        st = ctrl.status()
        self.assertEqual(st["state"], mec.PAUSED)
        self.assertNotIn("auto", self.gw.calls[n:])
        self.assertNotIn("arm", self.gw.calls[n:])

    # 3. Explicit Resume after recovery: PAUSED + CONNECTED + valid
    # authority/mission proof -> the existing guarded Resume transaction ->
    # AUTO -> progression proof -> RUNNING.
    def test_explicit_resume_after_recovery_uses_guarded_resume_transaction(self):
        ctrl = self._hold_paused_ctrl()
        self.gw.auto_sets_seq = 2   # continues from the retained paused sequence
        res = ctrl.resume()
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["verified_mode"], "AUTO")
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RUNNING)
        self.assertTrue(st["sequence"]["continuation_verified"])
        self.assertIn("auto", self.gw.calls)

    # 3b. Resume from a hold-only PAUSED still freshly checks authority --
    # the SAME guarded transaction an operator-issued Pause's Resume uses,
    # regardless of which path entered PAUSED (mirrors
    # test_resume_blocked_when_authority_not_local_agent in TestResume).
    def test_resume_from_hold_only_paused_blocked_when_authority_not_local_agent(self):
        ctrl = self._hold_paused_ctrl()
        self.gw.authority = "OPERATOR"
        n = len(self.gw.calls)
        res = ctrl.resume()
        self.assertEqual(res["outcome"], mec.SUSPENDED)
        self.assertEqual(res["error"]["code"], "AUTHORITY_LOST")
        self.assertNotIn("auto", self.gw.calls[n:])

    # 5. Hold-settle timeout -> SUSPENDED, not PAUSED (the hold was never
    # positively proven).
    def test_hold_settle_timeout_suspends_not_pauses(self):
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SUSPENDED", "running": False, "hold_only": True,
            "last_error": {"code": "HOLD_SETTLE_TIMEOUT", "message": "HOLD-SETTLE not proven"},
        })
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertNotEqual(st["state"], mec.PAUSED)

    # 6. LOITER verification/reassertion failure -> SUSPENDED, not PAUSED.
    def test_loiter_reassert_failure_suspends_not_pauses(self):
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SUSPENDED", "running": False, "hold_only": True,
            "last_error": {"code": "LOITER_REASSERT_NOT_VERIFIED",
                           "message": "final defensive LOITER re-assertion could not be verified"},
        })
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertNotEqual(st["state"], mec.PAUSED)

    # 7. Authority/proof failure during the hold-only transaction itself --
    # existing fail-closed SUSPENDED behaviour, never reclassified to PAUSED.
    def test_authority_lost_during_hold_only_transaction_stays_suspended(self):
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SUSPENDED", "running": False, "hold_only": True,
            "last_error": {"code": "AUTHORITY_LOST",
                           "message": "Control authority is not LOCAL_AGENT before commanding LOITER."},
        })
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertNotEqual(st["state"], mec.PAUSED)

    # 8. A genuine replanning failure (an actual plan/validate/upload attempt,
    # hold_only False) remains SUSPENDED and must never be mistaken for the
    # new hold-only PAUSED path.
    def test_genuine_replan_failure_stays_suspended_not_paused(self):
        ctrl = self._running_ctrl()
        ctrl.observe(self._snapshot(), {"fsm_state": "PLANNING", "running": True,
                                        "hold_only": False})
        ctrl.observe(self._snapshot(), {"fsm_state": "SAFE_HOLD", "running": False,
                                        "hold_only": False})
        st = ctrl.status()
        self.assertEqual(st["state"], mec.SUSPENDED)
        self.assertNotEqual(st["state"], mec.PAUSED)

    # Recorder/summary correctness (task "RECORDER / SUMMARY CHECK"): the
    # fresh vehicle-state read taken at the moment of a fail-closed SUSPENDED
    # outcome (unproven hold) is attached as terminal_evidence, exactly like
    # _end_operation's ensure_loiter path already does -- never left to fall
    # back to a stale pre-hold telemetry sample.
    def test_suspended_hold_proof_failure_captures_fresh_terminal_evidence(self):
        ctrl = self._running_ctrl()
        self.gw.mode_name = "LOITER"    # the defensive re-assert IS holding LOITER
        events = []
        ctrl._recorder = type("R", (), {
            "record_event": lambda self, *a, **kw: events.append((a, kw)),
            "finalize_async": lambda self, *a, **kw: None,
        })()
        ctrl.observe(self._snapshot(), {
            "fsm_state": "SUSPENDED", "running": False, "hold_only": True,
            "last_error": {"code": "HOLD_SETTLE_TIMEOUT", "message": "HOLD-SETTLE not proven"},
        })
        [(args, kwargs)] = events
        self.assertEqual(args[0], "MISSION_EXECUTION_TERMINAL_EVIDENCE")
        self.assertEqual(kwargs["data"]["final_mode"], "LOITER")


# ── Return-to-Home completion monitor ─────────────────────────────────────────
class TestReturnCompletion(_Running):
    def _returning_ctrl(self, clock, **kw):
        ctrl = self._running_ctrl(clock=clock, **kw)
        # Enter RETURNING_HOME with the vehicle still away from Home, so it does
        # not immediately jump into the arrival-pending window.
        ctrl.observe(self._snapshot(lat=56.70, lon=12.95),
                     {"fsm_state": "MONITORING_REVISED", "running": False}, now=clock.t)
        return ctrl

    def test_revised_auto_leads_to_returning_home(self):
        clock = Clock(1000.0)
        ctrl = self._returning_ctrl(clock)
        self.assertEqual(ctrl.status()["state"], mec.RETURNING_HOME)

    def test_outside_radius_does_not_complete(self):
        clock = Clock(1000.0)
        ctrl = self._returning_ctrl(clock)
        far = self._snapshot(lat=56.70, lon=12.95)  # far from Home
        for _ in range(5):
            d = ctrl.observe(far, None, now=clock.t)
            self.assertFalse(d["final_hold"])
            clock.t += 2.0
        self.assertEqual(ctrl.status()["state"], mec.RETURNING_HOME)

    def test_single_noisy_inside_sample_does_not_complete(self):
        clock = Clock(1000.0)
        ctrl = self._returning_ctrl(clock)
        inside = self._snapshot(lat=_HOME["latitude"], lon=_HOME["longitude"], seq=3, count=4)
        far = self._snapshot(lat=56.70, lon=12.95)
        ctrl.observe(inside, None, now=clock.t); clock.t += 1.0
        ctrl.observe(far, None, now=clock.t); clock.t += 1.0      # noise breaks persistence
        d = ctrl.observe(inside, None, now=clock.t)
        self.assertFalse(d["final_hold"])                          # timer restarted

    def test_stale_position_does_not_complete(self):
        clock = Clock(1000.0)
        ctrl = self._returning_ctrl(clock)
        stale = self._snapshot(lat=_HOME["latitude"], lon=_HOME["longitude"], age=99.0)
        for _ in range(6):
            d = ctrl.observe(stale, None, now=clock.t)
            self.assertFalse(d["final_hold"])
            clock.t += 1.0

    def test_persistence_confirms_and_final_loiter(self):
        clock = Clock(1000.0)
        ctrl = self._returning_ctrl(clock, cfg=_cfg(home_arrival_persistence_s=4.0,
                                                    home_arrival_radius_m=7.5))
        inside = self._snapshot(lat=_HOME["latitude"], lon=_HOME["longitude"], seq=3, count=4)
        signalled = False
        for _ in range(6):
            d = ctrl.observe(inside, None, now=clock.t)
            if d["final_hold"]:
                signalled = True
                break
            clock.t += 1.0
        self.assertTrue(signalled)
        self.assertEqual(ctrl.status()["state"], mec.HOME_ARRIVAL_PENDING)
        res = ctrl.run_final_hold()
        self.assertEqual(res["outcome"], mec.COMPLETED_HOLD)
        st = ctrl.status()
        self.assertTrue(st["return_completion"]["final_loiter_verified"])
        self.assertTrue(st["return_completion"]["arrival_confirmed"])

    def test_final_loiter_failure_does_not_complete(self):
        clock = Clock(1000.0)
        ctrl = self._returning_ctrl(clock, cfg=_cfg(home_arrival_persistence_s=2.0))
        inside = self._snapshot(lat=_HOME["latitude"], lon=_HOME["longitude"], seq=3, count=4)
        for _ in range(5):
            if ctrl.observe(inside, None, now=clock.t)["final_hold"]:
                break
            clock.t += 1.0
        self.gw.loiter_verified = False
        res = ctrl.run_final_hold()
        self.assertEqual(res["error"]["code"], "FINAL_LOITER_NOT_VERIFIED")
        self.assertNotEqual(ctrl.status()["state"], mec.COMPLETED_HOLD)
        self.assertEqual(ctrl.status()["state"], mec.RETURNING_HOME)


# ── Canonical status / arbitration ────────────────────────────────────────────
class TestStatusAndArbitration(_Base):
    def test_status_schema(self):
        ctrl = self._ctrl()
        st = ctrl.status()
        for key in ("supported", "state", "active_operation_id", "mission_id",
                    "original_route_hash", "active_route_hash", "verified_home",
                    "home_verification_distance_m", "mode", "sequence", "replanning",
                    "return_completion", "authority_status", "readiness",
                    "can_start", "can_pause", "can_resume",
                    "last_error", "history"):
            self.assertIn(key, st)
        for key in ("ready", "reason", "detail", "last_evaluated_at"):
            self.assertIn(key, st["readiness"])
        for key in ("current", "count", "before_pause", "at_resume",
                    "first_after_resume", "continuation_verified"):
            self.assertIn(key, st["sequence"])
        for key in ("distance_to_home_m", "arrival_radius_m", "persistence_s",
                    "persistence_progress_s", "arrival_confirmed", "final_loiter_verified"):
            self.assertIn(key, st["return_completion"])

    def test_ready_status_exposes_identity_and_authority(self):
        # Task test 9: READY status exposes the proven mission id, both route
        # hashes, and the real vehicle authority.
        ctrl = self._ctrl()
        ctrl.refresh_readiness()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.READY)
        self.assertTrue(st["can_start"])
        self.assertTrue(st["readiness"]["ready"])
        self.assertEqual(st["mission_id"], "m1")
        self.assertEqual(st["original_route_hash"], self.route_hash)
        self.assertEqual(st["active_route_hash"], self.route_hash)
        self.assertEqual(st["authority_status"], "LOCAL_AGENT")

    def test_ready_false_when_identity_unprovable(self):
        # Task test 10: READY/can_start is false when identity cannot be proven
        # (here, the fresh Pixhawk hash does not match the package), with a
        # precise readiness reason and NO terminal FAILED.
        self.gw.pixhawk_route_hash = "sha256:" + "0" * 64
        ctrl = self._ctrl()
        ctrl.refresh_readiness()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.NOT_READY)
        self.assertFalse(st["can_start"])
        self.assertFalse(st["readiness"]["ready"])
        self.assertTrue(st["readiness"]["reason"].startswith("ROUTE_HASH"))

    def test_ready_false_when_authority_not_local_agent(self):
        # Task test 8/10: authority not LOCAL_AGENT prevents READY.
        self.gw.authority = "OPERATOR"
        ctrl = self._ctrl()
        ctrl.refresh_readiness()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.NOT_READY)
        self.assertFalse(st["can_start"])
        self.assertEqual(st["readiness"]["reason"], "AUTHORITY_NOT_LOCAL_AGENT")

    def test_transient_readiness_timeout_stays_not_ready_not_failed(self):
        # Task: passive readiness must not enter FAILED on a temporary read
        # failure -- it reports NOT_READY with a precise reason instead.
        self.gw.state_read_error = [_Timeout(), _Timeout()]
        ctrl = self._ctrl()
        ctrl.refresh_readiness()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.NOT_READY)
        self.assertEqual(st["readiness"]["reason"], "STATE_TIMEOUT")

    def test_transient_state_timeout_one_bounded_retry(self):
        # Task test 11: a transient state read timeout is retried at most once and
        # then succeeds. Called directly (no observe) so only Start's own reads
        # consume the injected error.
        self.gw.state_read_error = [_Timeout()]   # first read times out, retry OK
        ctrl = self._ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)   # the one bounded retry recovered
        self.assertEqual(self.gw.state_read_error, [])  # exactly one retry consumed the timeout

    def test_two_state_timeouts_fail_state_timeout_no_write(self):
        # The retry is bounded: two consecutive timeouts fail closed as
        # STATE_TIMEOUT (distinct from any mission-identity error), with no write.
        self.gw.state_read_error = [_Timeout(), _Timeout()]
        ctrl = self._ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["error"]["code"], "STATE_TIMEOUT")
        self.assertEqual(self.gw.calls.count("state"), 2)
        self.assertEqual(self.gw.write_calls, [])

    def test_button_state_derivable_from_status(self):
        ctrl = self._ctrl()
        self.assertFalse(ctrl.status()["can_start"])   # NOT_READY
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        self.assertTrue(ctrl.status()["can_start"])    # READY
        ctrl.start("m1")
        st = ctrl.status()
        self.assertFalse(st["can_start"])
        self.assertTrue(st["can_pause"])
        self.assertFalse(st["can_resume"])

    def test_rearm_from_terminal(self):
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        self.gw.loiter_verified = False
        ctrl.start("m1")  # -> FAILED
        self.assertEqual(ctrl.status()["state"], mec.FAILED)
        res = ctrl.rearm()
        self.assertTrue(res["accepted"])
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))


# ── Pre-E2 replan lifecycle: rearm/fresh-readiness must rearm a stale replan ───
class TestReplanResetLifecycleHook(_Base):
    """A terminal FAILED/SAFE_HOLD/SUSPENDED/FALLBACK_RTL replan status left
    over from an earlier, unrelated attempt must not silently persist forever
    and keep flooring risk_model.py's mission component to HIGH (see
    test_risk_model.py). The single sanctioned lifecycle point that invokes
    the injected replan-reset hook (mec._reset_replan -> replan_controller.
    reset()) is the readiness proof's NOT_READY->READY edge
    (_apply_readiness_proof_locked) -- a fresh process, evidence proven for
    the first time while UNBOUND (exactly the pre-Start live scenario in the
    task), AND rearm()'s own re-proof (rearm only unbinds to NOT_READY; it is
    the readiness re-proof it triggers that actually reaches READY and rearms
    a stale replan status) all funnel through this one edge, mirroring the
    reset already wired into a verified Stop (_run_stop)."""

    def _ctrl_with_reset_hook(self, **kw):
        calls = []

        def reset_fn():
            calls.append(1)
            return {"reset": True, "from": "FAILED", "to": "MONITORING"}

        ctrl = self._ctrl(replan_reset_fn=reset_fn, **kw)
        return ctrl, calls

    def test_rearm_invokes_replan_reset_hook_via_its_readiness_reproof(self):
        ctrl, calls = self._ctrl_with_reset_hook()
        ctrl.observe(self._snapshot(mode="LOITER"), None)   # -> READY (edge #1)
        self.gw.loiter_verified = False
        ctrl.start("m1")  # -> FAILED (mission execution's OWN terminal state)
        self.assertEqual(ctrl.status()["state"], mec.FAILED)
        res = ctrl.rearm()
        self.assertTrue(res["accepted"])
        # rearm() unbinds to NOT_READY then synchronously re-proves; the
        # re-proof's own NOT_READY->READY edge fires the hook a second time.
        self.assertEqual(calls, [1, 1])

    def test_fresh_not_ready_to_ready_edge_invokes_replan_reset_hook_once(self):
        ctrl, calls = self._ctrl_with_reset_hook()
        self.assertEqual(ctrl.status()["state"], mec.NOT_READY)
        # First proof: NOT_READY -> READY -- the sanctioned edge (a brand new
        # process, or evidence proven for the first time while UNBOUND, is
        # exactly the pre-Start live scenario the task describes).
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        self.assertEqual(ctrl.status()["state"], mec.READY)
        self.assertEqual(calls, [1])
        # A later poll while ALREADY READY (evidence unchanged) must not
        # re-invoke the hook every cycle -- only the edge does.
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        self.assertEqual(ctrl.status()["state"], mec.READY)
        self.assertEqual(calls, [1])

    def test_reprove_binding_edge_invokes_replan_reset_hook_once(self):
        ctrl, calls = self._ctrl_with_reset_hook()
        self.assertEqual(ctrl.status()["state"], mec.NOT_READY)
        ctrl.observe(self._snapshot(mode="LOITER"), None)   # first proof -> READY
        self.assertEqual(calls, [1])
        # An explicit on-demand reproof while ALREADY READY (Operator "Full
        # Refresh") must not re-fire the hook -- only the NOT_READY->READY edge.
        ctrl.reprove_binding()
        self.assertEqual(calls, [1])

    def test_reset_hook_not_invoked_when_rearm_rejected(self):
        ctrl, calls = self._ctrl_with_reset_hook()
        ctrl.observe(self._snapshot(mode="LOITER"), None)   # -> READY (edge #1), not terminal
        self.assertEqual(calls, [1])
        res = ctrl.rearm()
        self.assertFalse(res["accepted"])
        # Rejected (READY is not a rearmable state) -- no additional hook call.
        self.assertEqual(calls, [1])


# ── Live startup bug: OPERATOR authority pre-Start must still clear a stale ───
# terminal replan (task: fresh mission evidence proven, only the pre-Start
# authority handoff is pending). Reproduces the exact live proof: NOT_READY +
# UNBOUND + proven mission/package/Pixhawk identity + authority OPERATOR + a
# persisted terminal FAILED replan status restored at startup.
class TestStaleReplanResetOnAuthorityBlockedEvidence(_Base):
    def _ctrl_with_replan(self, fsm="FAILED", running=False, **kw):
        self.replan_hook = _ReplanHook(fsm=fsm, running=running)
        return self._ctrl(replan_status_fn=self.replan_hook.status,
                          replan_reset_fn=self.replan_hook.reset, **kw)

    def _observe(self, ctrl, snapshot=None):
        snap = snapshot if snapshot is not None else self._snapshot(mode="LOITER")
        return ctrl.observe(snap, self.replan_hook.status())

    # 1. EXACT LIVE BUG.
    def test_operator_authority_evidence_proven_resets_stale_failed_replan_once(self):
        self.gw.authority = "OPERATOR"
        ctrl = self._ctrl_with_replan(fsm="FAILED")
        self.assertEqual(ctrl.status()["state"], mec.NOT_READY)
        self._observe(ctrl)
        st = ctrl.status()
        # Mission execution correctly stays NOT_READY -- authority is a genuine,
        # expected pre-Start block -- but evidence is proven and start-eligible.
        self.assertEqual(st["state"], mec.NOT_READY)
        self.assertFalse(st["execution_ready"])
        self.assertTrue(st["start_eligible"])
        self.assertTrue(st["authority_blocks_start"])
        self.assertEqual(st["start_block_reason"], "AUTHORITY_NOT_LOCAL_AGENT")
        self.assertEqual(st["binding"]["binding_state"], "UNBOUND")
        self.assertEqual(self.replan_hook.reset_calls, 1)

    # 2. Repeated polls with the SAME proof must not repeat the reset.
    def test_repeated_polls_same_proof_do_not_repeat_reset(self):
        self.gw.authority = "OPERATOR"
        ctrl = self._ctrl_with_replan(fsm="FAILED")
        self._observe(ctrl)
        self.assertEqual(self.replan_hook.reset_calls, 1)
        self._observe(ctrl)
        self._observe(ctrl)
        self.assertEqual(self.replan_hook.reset_calls, 1)
        self.assertEqual(ctrl.status()["state"], mec.NOT_READY)

    # 3. Authority later changes to LOCAL_AGENT (full READY) for the SAME
    #    generation -- must not fire a second, unnecessary reset.
    def test_later_local_agent_authority_same_generation_no_second_reset(self):
        self.gw.authority = "OPERATOR"
        ctrl = self._ctrl_with_replan(fsm="FAILED")
        self._observe(ctrl)
        self.assertEqual(self.replan_hook.reset_calls, 1)
        self.assertEqual(ctrl.status()["state"], mec.NOT_READY)
        self.gw.authority = "LOCAL_AGENT"
        self._observe(ctrl)
        self.assertEqual(ctrl.status()["state"], mec.READY)
        self.assertEqual(self.replan_hook.reset_calls, 1)   # still just the one

    # 4. Invalid route/package evidence -- stale FAILED is NOT auto-cleared.
    def test_invalid_route_evidence_does_not_clear_stale_replan(self):
        self.gw.authority = "OPERATOR"
        self.gw.pixhawk_route_hash = "sha256:" + "9" * 64   # disagrees with package
        ctrl = self._ctrl_with_replan(fsm="FAILED")
        self._observe(ctrl)
        self.assertEqual(ctrl.status()["state"], mec.NOT_READY)
        self.assertFalse(ctrl.status()["start_eligible"])
        self.assertEqual(self.replan_hook.reset_calls, 0)

    # 5. An ACTIVE replan transaction must never be reset by readiness polling.
    def test_active_replan_transaction_never_reset(self):
        self.gw.authority = "OPERATOR"
        ctrl = self._ctrl_with_replan(fsm="PLANNING", running=True)
        # Simulate the controller's cached _last_replan_fsm already reflecting
        # an ACTIVE transaction (as it would once observe() has previously seen
        # replan.status() report PLANNING/HOLD_REQUESTED/etc.) BEFORE the
        # readiness edge is judged -- the first-barrier guard in
        # _maybe_mark_fresh_evidence_reset_locked.
        with ctrl._state_lock:
            ctrl._last_replan_fsm = "PLANNING"
        self._observe(ctrl)
        self.assertEqual(self.replan_hook.reset_calls, 0)
        self._observe(ctrl)
        self.assertEqual(self.replan_hook.reset_calls, 0)
        # Once the transaction is no longer active, the deferred reset can
        # still fire for the same (never-yet-marked) generation.
        self.replan_hook.fsm = "FAILED"
        self.replan_hook.running = False
        with ctrl._state_lock:
            ctrl._last_replan_fsm = "FAILED"
        self._observe(ctrl)
        self.assertEqual(self.replan_hook.reset_calls, 1)

    # 6. A RUNNING/bound current mission is never touched by this edge (the
    #    readiness proof only ever runs while idle/UNBOUND -- NOT_READY/READY).
    def test_running_bound_mission_never_triggers_this_edge(self):
        self.gw.authority = "LOCAL_AGENT"
        ctrl = self._ctrl_with_replan(fsm="MONITORING")
        self._observe(ctrl)
        ctrl.start("m1")
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)
        baseline = self.replan_hook.reset_calls
        self.replan_hook.fsm = "FAILED"      # a genuine CURRENT failure
        self._observe(ctrl)
        # observe()'s OWN replan-failure handoff (unrelated to this edge) may
        # suspend the run, but the readiness-proof reset edge itself never ran
        # (readiness is only evaluated while NOT_READY/READY).
        self.assertEqual(self.replan_hook.reset_calls, baseline)

    # 8. A genuine CURRENT replan failure tied to an active/current execution
    #    remains safety-significant (never silently swallowed by this edge).
    def test_genuine_current_failure_during_live_execution_still_suspends(self):
        self.gw.authority = "LOCAL_AGENT"
        ctrl = self._ctrl_with_replan(fsm="MONITORING")
        self._observe(ctrl)
        ctrl.start("m1")
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)
        self.replan_hook.fsm = "FAILED"
        ctrl.observe(self._snapshot(), {"fsm_state": "FAILED", "running": False})
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertEqual(ctrl.status()["last_error"]["code"], "REPLANNING_NOT_SUCCESSFUL")


# ── Readback freshness: a stale/refreshing cache must never READY or start ─────
class TestStartReadbackFreshness(_Base):
    """GET /agent/pixhawk_mission is now cache-first/non-blocking. These prove a
    stale, refreshing, busy, or too-old CACHED readback can neither promote the
    controller to READY nor be accepted as the Start identity proof -- it fails
    closed with PIXHAWK_READBACK_STALE and NEVER writes to the vehicle."""

    def _observe(self, ctrl):
        ctrl.observe(self._snapshot(mode="LOITER"), None)

    def _assert_start_rejected_unfresh(self, ctrl):
        res = ctrl.start("m1")
        self.assertNotEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["error"]["code"], "PIXHAWK_READBACK_STALE")
        # Fail closed: no vehicle write of any kind happened.
        self.assertEqual(self.gw.write_calls, [])

    def test_stale_cached_readback_blocks_start(self):
        self.gw.pixhawk_stale = True
        self.gw.pixhawk_age_s = 30.0
        ctrl = self._ctrl()
        self._observe(ctrl)
        self.assertNotEqual(ctrl.status()["state"], mec.READY)
        self._assert_start_rejected_unfresh(ctrl)

    def test_refreshing_cached_readback_blocks_start(self):
        self.gw.pixhawk_refreshing = True
        ctrl = self._ctrl()
        self._observe(ctrl)
        self._assert_start_rejected_unfresh(ctrl)

    def test_busy_readback_blocks_start(self):
        self.gw.pixhawk_busy = True
        ctrl = self._ctrl()
        self._observe(ctrl)
        self._assert_start_rejected_unfresh(ctrl)

    def test_cached_readback_older_than_proof_limit_blocks_start(self):
        self.gw.pixhawk_age_s = pp.PROOF_MAX_CACHE_AGE_S + 5.0
        ctrl = self._ctrl()
        self._observe(ctrl)
        self._assert_start_rejected_unfresh(ctrl)

    def test_missing_proof_source_blocks_start(self):
        # A readback with a valid-looking hash/count but NO proof_source must be
        # rejected -- absence of explicit provenance is never treated as fresh.
        self.gw.pixhawk_proof_source = None
        ctrl = self._ctrl()
        self._observe(ctrl)
        self.assertNotEqual(ctrl.status()["state"], mec.READY)
        self._assert_start_rejected_unfresh(ctrl)

    def test_unknown_proof_source_blocks_start(self):
        self.gw.pixhawk_proof_source = "SOMETHING_ELSE"
        ctrl = self._ctrl()
        self._observe(ctrl)
        self._assert_start_rejected_unfresh(ctrl)

    def test_fresh_coordinated_cache_readback_allows_start(self):
        # The default fake readback is a valid, fresh COORDINATED_CACHE proof.
        ctrl = self._ctrl()
        self._observe(ctrl)
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)

    def test_fresh_direct_transaction_readback_allows_start(self):
        # A DIRECT_TRANSACTION readback with a recent completion time is also
        # acceptable proof.
        import time as _t
        self.gw.pixhawk_proof_source = pp.PROOF_SOURCE_DIRECT
        self.gw.pixhawk_proof_completed_at = _t.time()
        self.gw.pixhawk_cached = False
        self.gw.pixhawk_observed_at = None
        self.gw.pixhawk_age_s = None
        self.gw.pixhawk_refresh_generation = None
        ctrl = self._ctrl()
        self._observe(ctrl)
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)

    def test_readiness_not_ready_and_reason_stale(self):
        self.gw.pixhawk_stale = True
        self.gw.pixhawk_age_s = 30.0
        ctrl = self._ctrl()
        status = ctrl.refresh_readiness()
        self.assertFalse(status["can_start"])
        self.assertFalse(status["readiness"]["ready"])
        self.assertEqual(status["readiness"]["reason"], "PIXHAWK_READBACK_STALE")

    def test_readiness_not_ready_when_proof_source_missing(self):
        self.gw.pixhawk_proof_source = None
        ctrl = self._ctrl()
        status = ctrl.refresh_readiness()
        self.assertFalse(status["can_start"])
        self.assertFalse(status["readiness"]["ready"])


# ── No READY/STALE oscillation for an unchanged mission (task section 7/10) ────
class TestReadinessNoOscillation(_Base):
    def test_expired_cache_keeps_ready_and_reports_checking(self):
        # Once proven READY, a cache that lapses past the proof lifetime (with the
        # SAME mission still on the vehicle) must NOT tear readiness down or flap
        # -- it stays READY, reports CHECKING, and retains the last proof.
        ctrl = self._ctrl()
        ctrl.refresh_readiness()
        self.assertEqual(ctrl.status()["state"], mec.READY)
        self.assertFalse(ctrl.status()["readiness"]["checking"])

        self.gw.pixhawk_age_s = pp.PROOF_MAX_CACHE_AGE_S + 20.0   # cache expired
        for _ in range(6):
            ctrl.refresh_readiness()
            st = ctrl.status()
            self.assertEqual(st["state"], mec.READY)              # never oscillates away
            self.assertTrue(st["readiness"]["checking"])
            self.assertTrue(st["can_start"])                      # Start still re-proves fresh
            self.assertIsNotNone(st["readiness"]["last_verified"])

        self.gw.pixhawk_age_s = 0.5                               # fresh again
        ctrl.refresh_readiness()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.READY)
        self.assertFalse(st["readiness"]["checking"])

    def test_refreshing_cache_keeps_ready_and_reports_checking(self):
        ctrl = self._ctrl()
        ctrl.refresh_readiness()
        self.assertEqual(ctrl.status()["state"], mec.READY)
        self.gw.pixhawk_refreshing = True
        for _ in range(4):
            ctrl.refresh_readiness()
            st = ctrl.status()
            self.assertEqual(st["state"], mec.READY)
            self.assertTrue(st["readiness"]["checking"])

    def test_genuine_mismatch_still_demotes(self):
        # Retention is only for TRANSIENT gaps: a genuine, fresh route-hash change
        # (a real inconsistency) must still demote out of READY.
        ctrl = self._ctrl()
        ctrl.refresh_readiness()
        self.assertEqual(ctrl.status()["state"], mec.READY)
        self.gw.pixhawk_route_hash = "sha256:" + "0" * 64        # fresh, but different
        ctrl.refresh_readiness()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.NOT_READY)
        self.assertFalse(st["readiness"]["ready"])
        self.assertFalse(st["readiness"]["checking"])


# ── Start forces a fresh proof and stays fail-closed (task section 6) ─────────
class _ProofGateway(FakeGateway):
    """A gateway that implements prove_pixhawk_mission_readback the way the real
    one does: it requests a coordinator refresh (advancing the generation) and
    returns a readback stamped with the new generation. Records how many refreshes
    were forced so a test can prove Start does not rest on a passively-cached read."""
    def __init__(self):
        super().__init__()
        self.refresh_calls = 0

    def prove_pixhawk_mission_readback(self, *a, **kw):
        self.refresh_calls += 1
        self.pixhawk_refresh_generation = (self.pixhawk_refresh_generation or 0) + 1
        return self.pixhawk_mission_readback()


class TestStartForcesFreshProof(_Base):
    def _proof_ctrl(self):
        gw = _ProofGateway()
        gw.pixhawk_route_hash = self.route_hash
        self.gw = gw
        return self._ctrl()

    def test_start_forces_new_generation_then_succeeds(self):
        ctrl = self._proof_ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        before = self.gw.refresh_calls
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        # Start requested at least one fresh proof (a new generation), not just a
        # passive cached read.
        self.assertGreater(self.gw.refresh_calls, before)

    def test_start_fail_closed_when_forced_proof_unfresh(self):
        ctrl = self._proof_ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        # Even after forcing a refresh, the readback comes back stale -> fail
        # closed with no vehicle write of any kind.
        self.gw.pixhawk_stale = True
        self.gw.pixhawk_age_s = 30.0
        res = ctrl.start("m1")
        self.assertNotEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["error"]["code"], "PIXHAWK_READBACK_STALE")
        self.assertEqual(self.gw.write_calls, [])


# ── Config validation: poll interval vs proof freshness lifetime (section 5) ──
class TestReadinessPollValidation(unittest.TestCase):
    def test_default_poll_interval_is_comfortably_within_proof_lifetime(self):
        cfg = me_cfg.MissionExecutionConfig()
        ok, issues = me_cfg.validate(cfg)
        self.assertTrue(ok, issues)
        self.assertLess(cfg.readiness_poll_interval_s, pp.PROOF_MAX_CACHE_AGE_S)

    def test_poll_interval_exceeding_proof_lifetime_is_rejected(self):
        cfg = me_cfg.MissionExecutionConfig(
            readiness_poll_interval_s=pp.PROOF_MAX_CACHE_AGE_S + 10.0)
        ok, issues = me_cfg.validate(cfg)
        self.assertFalse(ok)
        self.assertTrue(any("readiness_poll_interval_s" in i for i in issues))

    def test_poll_interval_at_full_lifetime_is_rejected(self):
        # Equal to the lifetime is still not "comfortably shorter" -- the ~2.5 s
        # readback + jitter would land past the TTL edge.
        cfg = me_cfg.MissionExecutionConfig(
            readiness_poll_interval_s=pp.PROOF_MAX_CACHE_AGE_S)
        ok, _ = me_cfg.validate(cfg)
        self.assertFalse(ok)

    def test_zero_poll_interval_is_allowed(self):
        ok, _ = me_cfg.validate(
            me_cfg.MissionExecutionConfig(readiness_poll_interval_s=0.0))
        self.assertTrue(ok)


class TestProgressionConfigValidation(unittest.TestCase):
    """Task section 8: validate the progression / ARM / freshness invariants."""
    def _base(self, **kw):
        base = me_cfg.MissionExecutionConfig().to_dict()
        base["readiness_poll_interval_s"] = 0.0  # avoid the readiness invariant
        base.update(kw)
        return me_cfg.MissionExecutionConfig(**base)

    def test_defaults_valid(self):
        ok, issues = me_cfg.validate(self._base())
        self.assertTrue(ok, issues)

    def test_zero_progression_timeout_rejected(self):
        ok, _ = me_cfg.validate(self._base(start_progression_timeout_s=0.0))
        self.assertFalse(ok)

    def test_zero_poll_interval_rejected(self):
        ok, _ = me_cfg.validate(self._base(progression_poll_interval_s=0.0))
        self.assertFalse(ok)

    def test_poll_interval_not_comfortably_below_timeout_rejected(self):
        ok, _ = me_cfg.validate(self._base(start_progression_timeout_s=1.0,
                                           progression_poll_interval_s=0.9))
        self.assertFalse(ok)

    def test_negative_movement_threshold_rejected(self):
        ok, _ = me_cfg.validate(self._base(progression_min_displacement_m=-1.0))
        self.assertFalse(ok)

    def test_zero_movement_threshold_allowed(self):
        ok, issues = me_cfg.validate(self._base(progression_min_displacement_m=0.0))
        self.assertTrue(ok, issues)

    def test_nonpositive_max_position_age_rejected(self):
        ok, _ = me_cfg.validate(self._base(max_position_age_s=0.0))
        self.assertFalse(ok)

    def test_nonpositive_arm_verify_timeout_rejected(self):
        ok, _ = me_cfg.validate(self._base(arm_verify_timeout_s=0.0))
        self.assertFalse(ok)


# ── Normal ORIGINAL-mission completion monitor (task section 3) ────────────────
class TestNormalCompletion(_Running):
    """RUNNING -> COMPLETED_HOLD when the ORIGINAL mission reaches its final
    executable item and holds, on a defensible combination of fresh evidence --
    never one fragile signal, never a momentary final-sequence blip, and never a
    return-to-Home semantics trigger."""

    # The last waypoint of the fixture _ROUTE (index 2) -- the final-item position.
    _FINAL = (_ROUTE[-1]["latitude"], _ROUTE[-1]["longitude"])

    def _final_snap(self, **kw):
        # seq=2/count=3 -> at the final executable item; at the final waypoint.
        base = dict(lat=self._FINAL[0], lon=self._FINAL[1], seq=2, count=3, mode="AUTO")
        base.update(kw)
        return self._snapshot(**base)

    def _cfgc(self, **kw):
        base = dict(mission_complete_persistence_s=4.0,
                    mission_complete_final_item_tolerance=0,
                    mission_complete_position_radius_m=15.0)
        base.update(kw)
        return _cfg(**base)

    def test_genuine_final_waypoint_completes(self):
        clock = Clock(1000.0)
        ctrl = self._running_ctrl(clock=clock, cfg=self._cfgc())
        final = self._final_snap()
        signalled = False
        for _ in range(8):
            if ctrl.observe(final, None, now=clock.t)["final_hold"]:
                signalled = True
                break
            clock.t += 1.0
        self.assertTrue(signalled, "final-item persistence should signal a final hold")
        # Still RUNNING until the verified LOITER runs (no auto-write in observe).
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)
        res = ctrl.run_final_hold()
        self.assertEqual(res["outcome"], mec.COMPLETED_HOLD)
        st = ctrl.status()
        self.assertEqual(st["state"], mec.COMPLETED_HOLD)
        self.assertTrue(st["completion"]["confirmed"])
        self.assertTrue(st["completion"]["final_loiter_verified"])
        self.assertEqual(st["completion"]["evidence"]["mode"], "AUTO")
        # No auto-disarm: never an ARM/disarm write in the completion path.
        self.assertNotIn("arm", self.gw.calls[-3:])

    def test_temporary_final_sequence_then_advances_does_not_complete(self):
        # A single final-sequence observation that later changes must not complete.
        clock = Clock(1000.0)
        ctrl = self._running_ctrl(clock=clock, cfg=self._cfgc())
        final = self._final_snap()
        not_final = self._final_snap(seq=1)  # sequence not yet at the last item
        ctrl.observe(final, None, now=clock.t); clock.t += 1.0
        ctrl.observe(not_final, None, now=clock.t); clock.t += 1.0   # breaks persistence
        d = ctrl.observe(final, None, now=clock.t)
        self.assertFalse(d["final_hold"])
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)

    def test_final_waypoint_but_loiter_unverified_stays_running(self):
        clock = Clock(1000.0)
        ctrl = self._running_ctrl(clock=clock, cfg=self._cfgc(mission_complete_persistence_s=2.0))
        final = self._final_snap()
        for _ in range(5):
            if ctrl.observe(final, None, now=clock.t)["final_hold"]:
                break
            clock.t += 1.0
        self.gw.loiter_verified = False
        res = ctrl.run_final_hold()
        self.assertEqual(res["error"]["code"], "FINAL_LOITER_NOT_VERIFIED")
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)   # fell back to RUNNING, NOT complete
        self.assertNotEqual(ctrl.status()["state"], mec.COMPLETED_HOLD)

    def test_stale_position_does_not_complete(self):
        clock = Clock(1000.0)
        ctrl = self._running_ctrl(clock=clock, cfg=self._cfgc())
        stale = self._final_snap(age=99.0)   # position stale -> position gate withholds
        for _ in range(6):
            d = ctrl.observe(stale, None, now=clock.t)
            self.assertFalse(d["final_hold"])
            clock.t += 1.0
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)

    def test_position_far_from_final_waypoint_does_not_complete(self):
        clock = Clock(1000.0)
        ctrl = self._running_ctrl(clock=clock, cfg=self._cfgc())
        far = self._final_snap(lat=56.70, lon=12.95)   # at final seq but far away
        for _ in range(6):
            d = ctrl.observe(far, None, now=clock.t)
            self.assertFalse(d["final_hold"])
            clock.t += 1.0
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)

    def test_manual_mode_at_final_does_not_complete(self):
        # Operator grabbed MANUAL control -- not a completed autonomous mission.
        clock = Clock(1000.0)
        ctrl = self._running_ctrl(clock=clock, cfg=self._cfgc())
        manual = self._final_snap(mode="MANUAL")
        for _ in range(6):
            d = ctrl.observe(manual, None, now=clock.t)
            self.assertFalse(d["final_hold"])
            clock.t += 1.0
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)

    def test_completion_does_not_trigger_return_home(self):
        # Normal completion must reach COMPLETED_HOLD directly, never via
        # RETURNING_HOME / HOME_ARRIVAL_PENDING (no revised-mission handoff).
        clock = Clock(1000.0)
        ctrl = self._running_ctrl(clock=clock, cfg=self._cfgc())
        final = self._final_snap()
        for _ in range(6):
            if ctrl.observe(final, None, now=clock.t)["final_hold"]:
                break
            clock.t += 1.0
        states = [h["to"] for h in ctrl.status()["history"]]
        self.assertNotIn(mec.RETURNING_HOME, states)
        self.assertNotIn(mec.HOME_ARRIVAL_PENDING, states)
        res = ctrl.run_final_hold()
        self.assertEqual(res["outcome"], mec.COMPLETED_HOLD)

    def test_no_completion_while_replanning(self):
        clock = Clock(1000.0)
        ctrl = self._running_ctrl(clock=clock, cfg=self._cfgc())
        final = self._final_snap()
        # A live replan transaction owns the vehicle: completion must be withheld.
        for _ in range(6):
            d = ctrl.observe(final, {"fsm_state": "PLANNING", "running": True}, now=clock.t)
            self.assertFalse(d["final_hold"])
            clock.t += 1.0
        self.assertFalse(ctrl.status()["completion"]["candidate"])

    def test_restart_from_completed_mission_stays_completed_and_rearmable(self):
        store = _MemStore(mec.COMPLETED_HOLD, mission_id="m1")
        ctrl = self._ctrl(status_store=store)
        ctrl.recover_after_restart()
        self.assertEqual(ctrl.status()["state"], mec.COMPLETED_HOLD)
        res = ctrl.rearm()
        self.assertTrue(res["accepted"])
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))


# ── Rearm: unbind stale identity + bounded execution (task sections 1 & 6) ─────
class _MemStore:
    """In-memory StatusStore stand-in: seeds a persisted state for restart tests
    and never touches disk."""
    def __init__(self, state, mission_id="m1", route_hash="sha256:x", **extra):
        self.data = {"state": state, "mission_id": mission_id,
                     "original_route_hash": route_hash, "active_route_hash": route_hash}
        self.data.update(extra)

    def load_into(self, controller):
        controller._restore(dict(self.data))

    def save_from(self, controller):
        self.data = controller._persistable()


class TestRearmUnbinding(_Base):
    def _failed_ctrl(self):
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        self.gw.loiter_verified = False
        ctrl.start("m1")   # -> FAILED, binds mission_id m1
        self.assertEqual(ctrl.status()["state"], mec.FAILED)
        return ctrl

    def test_rearm_unbinds_previous_mission_identity(self):
        ctrl = self._failed_ctrl()
        # The FAILED mission still retains its bound m1 identity at the top level
        # (this is the stale value that the reproduced bug carried across rearm).
        self.assertEqual(ctrl.status()["mission_id"], "m1")
        # A NEW immutable package (same route, new id) is uploaded, as the Operator
        # re-upload workflow does.
        new_hash = _store_verified_package("msn-new")
        self.gw.pixhawk_route_hash = new_hash
        self.gw.mission_id = None  # Pixhawk carries no operator id
        res = ctrl.rearm()
        self.assertTrue(res["accepted"])
        st = ctrl.status()
        # The obsolete m1 identity must NOT be retained; readiness re-proves and
        # binds the NEW package identity.
        self.assertNotEqual(st["mission_id"], "m1")
        self.assertEqual(st["mission_id"], "msn-new")
        self.assertIsNone(st["binding"]["bound_original_mission_id"])  # unbound until Start
        self.assertEqual(st["binding"]["package_mission_id"], "msn-new")
        self.assertEqual(st["binding"]["binding_state"], "UNBOUND")

    def test_binding_state_bound_while_running(self):
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        ctrl.start("m1")
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RUNNING)
        self.assertEqual(st["binding"]["binding_state"], "BOUND")
        self.assertEqual(st["binding"]["bound_original_mission_id"], "m1")
        self.assertEqual(st["binding"]["package_mission_id"], "m1")

    def test_binding_stale_mismatch_when_new_package_uploaded_under_running(self):
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        ctrl.start("m1")
        # A new package appears while the old mission is still bound/running.
        _store_verified_package("msn-other")
        st = ctrl.status()
        self.assertEqual(st["binding"]["bound_original_mission_id"], "m1")
        self.assertEqual(st["binding"]["package_mission_id"], "msn-other")
        self.assertEqual(st["binding"]["binding_state"], "STALE_MISMATCH")


class TestRearmBoundedConcurrency(_Base):
    """Task section 6: rearm must be bounded and hold NO controller/state lock
    across the (slow) readiness re-proof, so a concurrent GET /status stays
    responsive and there is no deadlock."""

    def test_rearm_does_not_block_status_during_slow_readback(self):
        import threading as _t
        import time as _time
        # Async readiness (interval > 0) so rearm re-proves off the request thread.
        ctrl = self._ctrl(cfg=_cfg(readiness_poll_interval_s=5.0))
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        self.gw.loiter_verified = False
        ctrl.start("m1")   # -> FAILED
        self.assertEqual(ctrl.status()["state"], mec.FAILED)

        # Make the proof-grade readback block until the test releases it, to stand
        # in for a slow Pixhawk readback (e.g. during a mission upload in flight).
        release = _t.Event()
        orig_readback = self.gw.pixhawk_mission_readback
        def _slow_readback():
            release.wait(5.0)
            return orig_readback()
        self.gw.pixhawk_mission_readback = _slow_readback

        t0 = _time.monotonic()
        res = ctrl.rearm()               # must return promptly (bounded)
        rearm_ms = (_time.monotonic() - t0) * 1000.0
        self.assertTrue(res["accepted"])
        self.assertLess(rearm_ms, 1000.0, "rearm must not block on the readback")

        # While the background re-proof is blocked in the readback, GET status must
        # still answer quickly (no lock held across the readback).
        for _ in range(3):
            s0 = _time.monotonic()
            st = ctrl.status()
            self.assertLess((_time.monotonic() - s0) * 1000.0, 500.0)
            self.assertIsNotNone(st["state"])

        release.set()   # let the background refresh finish; no deadlock/hang


# ── New-package replacement lifecycle / state matrix (task section 2) ──────────
class TestOnNewPackageStored(_Base):
    def _force_state(self, state, mission_id="m1"):
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)   # NOT_READY -> READY
        with ctrl._state_lock:
            ctrl._state = state
            ctrl._mission_id = mission_id
            ctrl._original_route_hash = "sha256:old"
            ctrl._active_route_hash = "sha256:old"
            ctrl._sequence_before_pause = 2
            ctrl._verified_home = {"latitude": 56.0, "longitude": 12.0}
        return ctrl

    def _store_new(self, mission_id="msn-new"):
        h = _store_verified_package(mission_id)
        self.gw.pixhawk_route_hash = h
        self.gw.mission_id = None
        return h

    def test_terminal_states_adopt_and_invalidate(self):
        for state in (mec.NOT_READY, mec.READY, mec.SUSPENDED, mec.FAILED, mec.COMPLETED_HOLD):
            with self.subTest(state=state):
                ctrl = self._force_state(state)
                self._store_new("msn-new")
                res = ctrl.on_new_package_stored("msn-new")
                self.assertTrue(res["adopted"], f"{state}: {res}")
                st = ctrl.status()
                # Invalidated: NOT auto-started; prior identity/evidence gone;
                # the new package is the prepared next mission.
                self.assertIn(st["state"], (mec.READY, mec.NOT_READY))
                self.assertEqual(st["mission_id"], "msn-new")
                self.assertIsNone(st["binding"]["bound_original_mission_id"])
                self.assertEqual(st["binding"]["binding_state"], "UNBOUND")
                self.assertIsNone(st["sequence"]["before_pause"])
                self.assertIsNone(st["package_conflict"])

    def test_active_states_do_not_adopt_and_report_conflict(self):
        # RUNNING via a real Start; PAUSED via pause.
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        ctrl.start("m1")
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)
        _store_verified_package("msn-other")
        res = ctrl.on_new_package_stored("msn-other")
        self.assertFalse(res["adopted"])
        self.assertEqual(res["conflict"], "STALE_PACKAGE_DURING_ACTIVE_EXECUTION")
        st = ctrl.status()
        self.assertEqual(st["state"], mec.RUNNING)          # not replaced
        self.assertEqual(st["mission_id"], "m1")            # bound identity intact
        self.assertEqual(st["binding"]["binding_state"], "STALE_MISMATCH")
        self.assertIsNotNone(st["package_conflict"])
        self.assertEqual(st["package_conflict"]["code"], "STALE_PACKAGE_DURING_ACTIVE_EXECUTION")

    def test_paused_state_reports_conflict(self):
        ctrl = self._force_state(mec.PAUSED)
        self._store_new("msn-other")
        res = ctrl.on_new_package_stored("msn-other")
        self.assertFalse(res["adopted"])
        self.assertEqual(ctrl.status()["state"], mec.PAUSED)

    def test_returning_home_reports_conflict(self):
        ctrl = self._force_state(mec.RETURNING_HOME)
        self._store_new("msn-other")
        res = ctrl.on_new_package_stored("msn-other")
        self.assertFalse(res["adopted"])
        self.assertEqual(ctrl.status()["state"], mec.RETURNING_HOME)

    def test_active_replanning_reports_conflict(self):
        ctrl = self._force_state(mec.RUNNING)
        with ctrl._state_lock:
            ctrl._replanning_active = True
        self._store_new("msn-other")
        res = ctrl.on_new_package_stored("msn-other")
        self.assertFalse(res["adopted"])
        self.assertEqual(res["conflict"], "STALE_PACKAGE_DURING_ACTIVE_EXECUTION")

    def test_operation_in_progress_reports_conflict(self):
        ctrl = self._force_state(mec.SUSPENDED)
        ctrl._action_lock.acquire()
        try:
            res = ctrl.on_new_package_stored("msn-other")
        finally:
            ctrl._action_lock.release()
        self.assertFalse(res["adopted"])
        self.assertEqual(res["conflict"], "OPERATION_IN_PROGRESS")

    def test_adopt_does_not_auto_start(self):
        ctrl = self._force_state(mec.COMPLETED_HOLD)
        self._store_new("msn-new")
        n = len(self.gw.write_calls)
        ctrl.on_new_package_stored("msn-new")
        self.assertEqual(self.gw.write_calls[n:], [])       # no vehicle write / no auto-Start


# ── Start eligibility vs execution readiness (task section 2) ──────────────────
class TestStartEligibilityDiagnostics(_Base):
    def test_operator_authority_is_start_eligible_but_not_execution_ready(self):
        # Task section 3: OPERATOR authority (the Operator hands off LOCAL_AGENT
        # before invoking Scout Start). All evidence is proven, so the mission is
        # start_eligible; execution_ready is honestly false (handoff pending).
        self.gw.authority = "OPERATOR"
        ctrl = self._ctrl()
        st = ctrl.refresh_readiness()
        self.assertTrue(st["start_eligible"])          # ready -- only authority pending
        self.assertFalse(st["execution_ready"])        # cannot run RIGHT NOW
        self.assertFalse(st["can_start"])
        self.assertEqual(st["start_block_reason"], "AUTHORITY_NOT_LOCAL_AGENT")
        self.assertTrue(st["authority_blocks_start"])
        # The proven identity is surfaced even though authority is not ours yet.
        self.assertEqual(st["mission_id"], "m1")

    def test_authority_handoff_makes_same_mission_execution_ready_without_rebind(self):
        # Task section 3: after authority becomes LOCAL_AGENT the SAME verified
        # mission becomes execution-ready -- same identity, not replaced/rebound.
        self.gw.authority = "OPERATOR"
        ctrl = self._ctrl()
        st1 = ctrl.refresh_readiness()
        self.assertTrue(st1["start_eligible"])
        self.assertFalse(st1["execution_ready"])
        self.assertEqual(st1["original_route_hash"], self.route_hash)
        # Operator hands off authority.
        self.gw.authority = "LOCAL_AGENT"
        st2 = ctrl.refresh_readiness()
        self.assertTrue(st2["execution_ready"])
        self.assertTrue(st2["can_start"])
        self.assertEqual(st2["state"], mec.READY)
        # Same mission identity / route hash -- not rebound to something else.
        self.assertEqual(st2["mission_id"], "m1")
        self.assertEqual(st2["original_route_hash"], self.route_hash)

    def test_local_agent_ready_is_execution_ready_and_eligible(self):
        ctrl = self._ctrl()
        st = ctrl.refresh_readiness()
        self.assertTrue(st["can_start"])
        self.assertTrue(st["execution_ready"])
        self.assertTrue(st["start_eligible"])
        self.assertIsNone(st["start_block_reason"])
        self.assertFalse(st["authority_blocks_start"])

    def test_evidence_defect_is_not_authority_block(self):
        # A genuine package/Pixhawk evidence defect (not authority) is reported as
        # its own reason and is NOT flagged as an authority handoff.
        self.gw.pixhawk_stale = True
        self.gw.pixhawk_age_s = 30.0
        ctrl = self._ctrl()
        st = ctrl.refresh_readiness()
        self.assertFalse(st["can_start"])
        self.assertEqual(st["start_block_reason"], "PIXHAWK_READBACK_STALE")
        self.assertFalse(st["authority_blocks_start"])


# ── Stop Mission (operator-requested safe abort + reset-to-start) ─────────────
class _ReplanHook:
    """Stand-in for the replan controller's status/reset surface Stop's bounded
    internal reset hook drives (no real replan controller in these tests)."""
    def __init__(self, revised_hash=None, fsm="MONITORING", running=False):
        self.reset_calls = 0
        self.revised_hash = revised_hash
        self.fsm = fsm
        self.running = running

    def status(self):
        return {"fsm_state": self.fsm, "running": self.running,
                "revised_mission_hash": self.revised_hash}

    def reset(self):
        self.reset_calls += 1
        return {"reset": True, "from": self.fsm, "to": "MONITORING"}


class TestStop(_Base):
    def _stop_ctrl(self, revised_hash=None, replan_fsm="MONITORING", **kw):
        self.replan = _ReplanHook(revised_hash=revised_hash, fsm=replan_fsm)
        self.experiment_clears = []

        def _clear_experiment():
            self.experiment_clears.append(True)
            return True

        return self._ctrl(replan_status_fn=self.replan.status,
                          replan_reset_fn=self.replan.reset,
                          experiment_reset_fn=_clear_experiment, **kw)

    def _running(self, **kw):
        ctrl = self._stop_ctrl(**kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)  # NOT_READY -> READY
        # That NOT_READY -> READY promotion is itself now a sanctioned replan-
        # reset lifecycle edge (task: pre-E2 replan lifecycle -- rearms any
        # stale terminal replan status left over from a PREVIOUS mission before
        # a fresh Start), so it already claims one reset via the same injected
        # hook Stop uses below. Record that baseline so callers can assert
        # Stop's OWN reset behaviour as a DELTA, independent of it.
        self.reset_calls_baseline = self.replan.reset_calls
        ctrl.start("m1")
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)
        return ctrl

    def _returning_home_revised(self, revised="sha256:revised"):
        """Drive RUNNING -> RETURNING_HOME with a verified revised safe-return hash
        bound, and simulate that revised route being the one installed."""
        ctrl = self._running(revised_hash=revised, replan_fsm="MONITORING_REVISED")
        far = self._snapshot(lat=56.70, lon=12.95)
        ctrl.observe(far, {"fsm_state": "MONITORING", "running": False})
        ctrl.observe(far, {"fsm_state": "MONITORING_REVISED", "running": False,
                           "revised_mission_hash": revised})
        self.assertEqual(ctrl.status()["state"], mec.RETURNING_HOME)
        self.assertEqual(ctrl.status()["active_route_hash"], revised)
        self.gw.pixhawk_route_hash = revised   # the revised route is installed
        self.gw.pixhawk_route_count = 2
        return ctrl

    # 1. RUNNING original -> Stop -> LOITER -> rewind -> READY/NOT_READY.
    def test_running_original_stop_rewinds_ready(self):
        ctrl = self._running()
        self.gw.current_seq = 2                       # mid-route
        n = len(self.gw.calls)
        res = ctrl.stop()
        self.assertTrue(res["accepted"])
        stop = res["stop"]
        self.assertTrue(stop["hold_verified"])
        self.assertEqual(stop["original_restored"], "NOT_NEEDED")
        self.assertEqual(stop["active_hash_before"], self.route_hash)
        self.assertEqual(stop["original_hash"], self.route_hash)
        self.assertTrue(stop["rewind_verified"])
        self.assertLessEqual(stop["sequence_after"], 1)
        self.assertEqual(stop["authority_after"], "OPERATOR")
        self.assertTrue(stop["ready_for_start"])
        calls = self.gw.calls[n:]
        self.assertIn("loiter", calls)
        self.assertIn("set_current", calls)
        self.assertNotIn("auto", calls)        # never resume AUTO
        self.assertNotIn("arm", calls)         # never (re)arm
        self.assertNotIn("upload", calls)      # original kept, not re-uploaded
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))
        self.assertTrue(ctrl.status()["start_eligible"])
        self.assertEqual(self.replan.reset_calls - self.reset_calls_baseline, 1)
        self.assertEqual(self.experiment_clears, [True])

    # 2. PAUSED -> Stop.
    def test_paused_stop(self):
        ctrl = self._running()
        self.gw.current_seq = 2
        ctrl.pause()
        self.assertEqual(ctrl.status()["state"], mec.PAUSED)
        res = ctrl.stop()
        self.assertTrue(res["accepted"])
        self.assertTrue(res["stop"]["hold_verified"])
        self.assertTrue(res["stop"]["rewind_verified"])
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))

    # 2b. PAUSED via a positively-proven communication-loss hold-only
    # transaction (E3 field-run fix) -> Stop. Explicit Stop from PAUSED
    # remains valid regardless of which path (operator Pause vs. proven
    # hold-only REQUEST_HOLD) entered PAUSED -- the existing Stop transaction
    # is reused unchanged.
    def test_hold_only_paused_stop(self):
        ctrl = self._running()
        self.gw.current_seq = 2
        ctrl.observe(self._snapshot(), {"fsm_state": "SAFE_HOLD", "running": False,
                                        "hold_only": True})
        self.assertEqual(ctrl.status()["state"], mec.PAUSED)
        # The injected replan-status hook is a SEPARATE stand-in from the
        # replan_status dict passed to observe() above (see _ReplanHook) --
        # mirror its terminal fsm the same way test_suspended_after_replan_
        # failure_stop does, so Stop's own "replanning active?" pre-check sees
        # the matching idle/terminal state.
        self.replan.fsm = "SAFE_HOLD"
        res = ctrl.stop()
        self.assertTrue(res["accepted"])
        self.assertTrue(res["stop"]["hold_verified"])
        self.assertTrue(res["stop"]["rewind_verified"])
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))

    # 3. SUSPENDED after replan failure -> Stop.
    def test_suspended_after_replan_failure_stop(self):
        ctrl = self._running(revised_hash="sha256:revised")
        ctrl.observe(self._snapshot(), {"fsm_state": "PLANNING", "running": True})
        ctrl.observe(self._snapshot(), {"fsm_state": "FAILED", "running": False})
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.replan.fsm = "FAILED"       # terminal -> not "active"
        res = ctrl.stop()
        self.assertTrue(res["accepted"])
        self.assertTrue(res["stop"]["hold_verified"])
        self.assertTrue(res["stop"]["rewind_verified"])
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))

    # 4. Active revised return mission -> Stop restores original hash then rewinds.
    def test_revised_return_restores_original_then_rewinds(self):
        ctrl = self._returning_home_revised()
        self.gw.upload_sets_hash = self.route_hash      # restore reads back original
        self.gw.upload_sets_count = len(_ROUTE)
        self.gw.current_seq = 3
        res = ctrl.stop()
        stop = res["stop"]
        self.assertTrue(res["accepted"])
        self.assertTrue(stop["hold_verified"])
        self.assertEqual(stop["active_hash_before"], "sha256:revised")
        self.assertEqual(stop["revised_hash"], "sha256:revised")
        self.assertIs(stop["original_restored"], True)
        self.assertTrue(stop["rewind_verified"])
        # upload (restore) happened BEFORE the rewind.
        self.assertIn("upload", self.gw.calls)
        self.assertIn("set_current", self.gw.calls)
        self.assertLess(self.gw.calls.index("upload"), self.gw.calls.index("set_current"))
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))

    # 5. Unknown Pixhawk hash -> fail closed in LOITER, no reset claimed.
    def test_unknown_pixhawk_hash_fails_closed_in_loiter(self):
        ctrl = self._running()
        self.gw.pixhawk_route_hash = "sha256:unknown-xyz"
        res = ctrl.stop()
        stop = res["stop"]
        self.assertTrue(res["accepted"])           # accepted, then failed closed
        self.assertTrue(stop["hold_verified"])     # LOITER held first
        self.assertIs(stop["original_restored"], False)
        self.assertEqual(res["error"]["code"], "STOP_ACTIVE_MISSION_UNKNOWN")
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertFalse(stop["ready_for_start"])
        self.assertIsNone(stop["authority_after"])   # authority NOT returned
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("set_current", self.gw.calls)   # never rewound
        # no reset claimed BY STOP (the baseline reset is the pre-Start
        # readiness-edge one, not Stop's -- see _running()'s docstring).
        self.assertEqual(self.replan.reset_calls - self.reset_calls_baseline, 0)
        self.assertEqual(self.experiment_clears, [])

    # 6. LOITER verification failure -> no logical reset claimed.
    def test_loiter_verification_failure_no_reset(self):
        ctrl = self._running()
        self.gw.loiter_verified = False
        n = len(self.gw.calls)
        res = ctrl.stop()
        stop = res["stop"]
        self.assertIs(stop["hold_verified"], False)
        self.assertEqual(res["error"]["code"], "STOP_HOLD_NOT_VERIFIED")
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertFalse(stop["ready_for_start"])
        self.assertIsNone(stop["authority_after"])
        stop_calls = self.gw.calls[n:]
        self.assertNotIn("upload", stop_calls)
        self.assertNotIn("set_current", stop_calls)
        self.assertNotIn("auto", stop_calls)       # never AUTO
        self.assertEqual(self.replan.reset_calls - self.reset_calls_baseline, 0)
        self.assertEqual(self.experiment_clears, [])

    # 7. Mission restore upload failure -> remain SUSPENDED (in LOITER).
    def test_restore_upload_failure_remains_suspended(self):
        ctrl = self._returning_home_revised()
        self.gw.upload_verified = False
        res = ctrl.stop()
        stop = res["stop"]
        self.assertTrue(stop["hold_verified"])
        self.assertIs(stop["original_restored"], False)
        self.assertEqual(res["error"]["code"], "STOP_RESTORE_UPLOAD_FAILED")
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertNotIn("set_current", self.gw.calls)    # never rewound
        self.assertEqual(self.replan.reset_calls - self.reset_calls_baseline, 0)

    # 8. Restore readback/hash verification failure -> remain SUSPENDED.
    def test_restore_readback_hash_mismatch_remains_suspended(self):
        ctrl = self._returning_home_revised()
        self.gw.upload_verified = True
        self.gw.upload_sets_hash = "sha256:still-not-original"   # readback != original
        res = ctrl.stop()
        self.assertEqual(res["error"]["code"], "STOP_RESTORE_HASH_MISMATCH")
        self.assertIs(res["stop"]["original_restored"], False)
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertIn("upload", self.gw.calls)
        self.assertNotIn("set_current", self.gw.calls)
        self.assertEqual(self.replan.reset_calls - self.reset_calls_baseline, 0)

    # 9. Rewind ACK accepted but sequence not actually reset -> failure.
    def test_rewind_ack_but_sequence_not_reset_fails(self):
        clock = AdvancingClock(1000.0)
        ctrl = self._running(clock=clock)
        ctrl._sleep = clock.advance
        self.gw.current_seq = 5
        self.gw.rewind_applies = False        # ACK accepted, sequence never moves
        res = ctrl.stop()
        stop = res["stop"]
        self.assertIs(stop["rewind_verified"], False)
        self.assertEqual(stop["sequence_after"], 5)
        self.assertEqual(res["error"]["code"], "STOP_REWIND_NOT_VERIFIED")
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        self.assertIn("set_current", self.gw.calls)   # the rewind WAS attempted
        # but no reset claimed BY STOP itself (delta 0 -- only the pre-Start
        # readiness-edge reset in the baseline happened).
        self.assertEqual(self.replan.reset_calls - self.reset_calls_baseline, 0)

    # 10. Active experiment injection cleared on an explicit Stop.
    def test_experiment_injection_cleared(self):
        ctrl = self._running()
        res = ctrl.stop()
        self.assertIs(res["stop"]["experiment_cleared"], True)
        self.assertEqual(self.experiment_clears, [True])

    # 11. Trigger generation / replan state reset.
    def test_replan_state_reset(self):
        ctrl = self._running()
        res = ctrl.stop()
        self.assertEqual(self.replan.reset_calls - self.reset_calls_baseline, 1)
        self.assertEqual(res["stop"]["replan_reset"], {"reset": True, "from": "MONITORING",
                                                       "to": "MONITORING"})

    # 12. Previous runtime verified Home invalidated (next Start must re-verify it).
    def test_previous_runtime_home_invalidated(self):
        ctrl = self._running()
        self.assertIsNotNone(ctrl.status()["verified_home"])
        ctrl.stop()
        self.assertIsNone(ctrl.status()["verified_home"])
        self.assertIsNone(ctrl.status()["home_verification_distance_m"])

    # 13. Original planning package preserved.
    def test_original_planning_package_preserved(self):
        ctrl = self._running()
        ctrl.stop()
        original = pp.load_original()
        self.assertIsNotNone(original)
        self.assertEqual(original["mission_id"], "m1")
        self.assertTrue(pp.is_usable(pp.load()))

    # 14. No automatic DISARM, no AUTO issued across a full Stop.
    def test_no_disarm_no_auto(self):
        ctrl = self._running()
        n = len(self.gw.calls)
        ctrl.stop()
        stop_calls = self.gw.calls[n:]
        self.assertNotIn("auto", stop_calls)
        self.assertNotIn("arm", stop_calls)   # no arm/disarm surface is ever touched

    # P0-2. Authority lost between the safety-hold LOITER and the mission-
    # restore upload must block the upload -- a mission-changing write is
    # never issued once authority is OPERATOR, even though the preceding
    # LOITER safety hold already succeeded.
    def test_restore_upload_blocked_when_authority_lost(self):
        ctrl = self._returning_home_revised()
        self.gw.authority_values = ["LOCAL_AGENT", "OPERATOR"]  # top gate, then restore gate
        n = len(self.gw.calls)
        res = ctrl.stop()
        self.assertEqual(res["error"]["code"], "AUTHORITY_LOST")
        self.assertEqual(ctrl.status()["state"], mec.SUSPENDED)
        calls = self.gw.calls[n:]
        self.assertIn("loiter", calls)          # safety-exempt hold still happened
        self.assertNotIn("upload", calls)       # but the mission-changing write did not
        self.assertNotIn("set_current", calls)  # rewind never reached either

    # 15. Authority returned to OPERATOR only after a safe hold.
    def test_authority_returned_after_hold(self):
        ctrl = self._running()
        res = ctrl.stop()
        self.assertEqual(res["stop"]["authority_after"], "OPERATOR")
        self.assertEqual(self.gw.authority_set_to, "OPERATOR")
        self.assertIn("set_authority", self.gw.calls)
        self.assertLess(self.gw.calls.index("loiter"), self.gw.calls.index("set_authority"))
        # ...and the authority hand-back is the LAST vehicle write (after rewind).
        self.assertLess(self.gw.calls.index("set_current"), self.gw.calls.index("set_authority"))

    # 16. Concurrent Stop + another mission operation -> BUSY, never blocks.
    def test_concurrent_operation_returns_busy(self):
        ctrl = self._running()
        ctrl._action_lock.acquire()   # simulate another operation in progress
        try:
            res = ctrl.stop()
        finally:
            ctrl._action_lock.release()
        self.assertFalse(res["accepted"])
        self.assertEqual(res["error"]["code"], "OPERATION_IN_PROGRESS")

    def test_stop_conflicts_with_active_replan(self):
        ctrl = self._running()
        tok = write_arbiter.acquire(write_arbiter.OWNER_REPLANNING)
        try:
            res = ctrl.stop()
        finally:
            write_arbiter.release(tok)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["error"]["code"], "REPLANNING_ACTIVE")

    def test_stop_rejected_from_idle_state(self):
        ctrl = self._stop_ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)   # READY, nothing to stop
        res = ctrl.stop()
        self.assertFalse(res["accepted"])
        self.assertEqual(res["error"]["code"], "NOT_STOPPABLE")

    # 17. GET status stays responsive throughout a slow Stop.
    def test_status_responsive_during_slow_stop(self):
        import threading
        import time as _time
        ctrl = self._running()
        gate = threading.Event()
        original_loiter = self.gw.command_loiter

        def slow_loiter():
            gate.wait(3.0)
            return original_loiter()

        self.gw.command_loiter = slow_loiter
        worker = threading.Thread(target=ctrl.stop)
        worker.start()
        try:
            _time.sleep(0.15)          # let the stop thread reach the slow hold write
            started = _time.perf_counter()
            st = ctrl.status()
            elapsed = _time.perf_counter() - started
            self.assertTrue(st["supported"])
            self.assertLess(elapsed, 0.5)   # not blocked behind the in-flight write
        finally:
            gate.set()
            worker.join(4.0)
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))

    # 18. Restart after a successful Stop remains non-running and recoverable.
    def test_restart_after_successful_stop_non_running(self):
        store = _MemStore(mec.NOT_READY)
        ctrl = self._stop_ctrl(status_store=store)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        ctrl.start("m1")
        ctrl.stop()
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))
        # New process: reload persisted state and reconcile.
        ctrl2 = self._stop_ctrl(status_store=store)
        ctrl2.recover_after_restart()
        st = ctrl2.status()
        self.assertNotIn(st["state"], mec._LIVE_STATES)         # non-running
        self.assertNotIn(st["state"], (mec.RUNNING, mec.PAUSED))

    # 19. An interrupted Stop (persisted mid-phase) fails closed on restart.
    def test_interrupted_stop_fails_closed_on_restart(self):
        store = _MemStore(mec.REWINDING_MISSION)
        ctrl = self._stop_ctrl(status_store=store)
        ctrl.recover_after_restart()
        st = ctrl.status()
        self.assertEqual(st["state"], mec.FAILED)
        self.assertEqual(st["last_error"]["code"], "UNKNOWN_AFTER_RESTART")


if __name__ == "__main__":
    unittest.main(verbosity=2)
