"""
Standalone tests for replan_controller.py -- the replanning FSM.

    python3 test_replan_controller.py

Uses a fake gateway (no HTTP / no MAVLink / no Pixhawk) and a scratch planning
package. Covers the full success sequence, the fresh pre-replan ORIGINAL-mission
proof (CRITICAL ISSUE 2), the SHARED progression verifier over the revised
mission (CRITICAL ISSUE 1 -- the same mission_progression.py Start/Resume use),
every post-LOITER failure staying in LOITER, bounded retries, SAFE_HOLD,
conditional RTL fallback, authority handling, duplicate suppression, dry-run
(no vehicle writes), restart/status persistence, and the status/event payloads.
"""
import json
import os
import tempfile
import unittest

import decision_policy
import decision_snapshot as dsm
import energy_policy
import planning_package as pp
import replan_config
import replan_controller as rc
import safe_return_planner as srp


_ROUTE = [
    {"latitude": 56.6500, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_OUTBOUND_TRANSIT},
    {"latitude": 56.6510, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
    {"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
]
_HOME = {"latitude": 56.6490, "longitude": 12.8700}
_BOUNDARY = [[56.648, 12.868], [56.653, 12.868], [56.653, 12.872], [56.648, 12.872]]


def _store_verified_package(mission_id="m1", route=None, home=None):
    """Persist a v1-structural, Pixhawk-verified planning package (immutable
    original + active) the way the real acceptance path does, and return its route
    content hash so the fake Pixhawk readback can match it."""
    route = _ROUTE if route is None else route
    home = _HOME if home is None else home
    pkg = pp.build_package(mission_id, route, home, usv_id="usv-2",
                           no_go_zones=[], navigable_boundary=_BOUNDARY)
    pkg["route_hash"] = pkg["original_route_hash"]
    pkg["mission_revision"] = 0
    pkg["immutable"] = True
    pp.store_accepted(pkg, pkg["route_hash"], {"source": "test"})
    return pkg["route_hash"]


# ── Fakes ─────────────────────────────────────────────────────────────────────
class FakeGateway:
    def __init__(self):
        self.authority = "LOCAL_AGENT"
        self.authority_values = None      # optional list, popped per call
        self.authority_raises = False
        self.loiter_verified = True
        self.auto_verified = True
        self.rtl_verified = True
        self.upload = {"accepted": True, "uploaded": True, "verified": True}
        self.home_ok = True
        # Fresh vehicle-state (progression) knobs. After command_auto the fake
        # sets mode AUTO and keeps ACTIVE_TRUE, so the happy path proves
        # progression via signal A on the first watch sample.
        self.mode_name = "AUTO"
        self.armed = True
        self.lat = 56.6520
        self.lon = 12.8700
        self.position_age_s = 0.5
        self.heartbeat_age_s = 0.3
        self.vehicle_mission_id = None    # vehicle_state.mission.current_mission_id
        self.current_seq = 3
        self.mission_count = 4
        self.mission_active = True
        self.mission_active_evidence = "ACTIVE_TRUE"
        # Freshness/age (seconds) of the mission_active_evidence observation
        # (mission_progression.py's freshness-semantics correction: proof A
        # now requires a KNOWN, in-bound age). Fresh by default so the happy
        # path keeps proving via signal A exactly as before.
        self.mission_active_evidence_age_s = 0.3
        self.on_state_read = None
        self._state_reads = 0
        self.state_read_error = None
        # Fresh Pixhawk readback knobs (COORDINATED_CACHE, fresh, matching).
        self.pixhawk_reachable = True
        self.pixhawk_partial = False
        self.pixhawk_route_hash = None    # injected by the fixture to match package
        self.pixhawk_route_count = len(_ROUTE)
        self.pixhawk_error = None
        self.readback_error = None
        self.pixhawk_proof_source = pp.PROOF_SOURCE_CACHE
        self.pixhawk_refresh_generation = 2
        self.pixhawk_stale = False
        self.pixhawk_refreshing = False
        self.pixhawk_busy = False
        self.pixhawk_observed_at = 1000.0
        self.pixhawk_age_s = 0.5
        # HOLD-SETTLE precondition dry-run knobs (see upload_preconditions
        # below). groundspeed_sequence, when set, is popped per call (the
        # last value repeats once exhausted) so a test can model a
        # decelerating vehicle across successive polls.
        self.upload_precondition_groundspeed_sequence = None
        self.upload_precondition_threshold = 0.5
        self.upload_precondition_raises = False
        self.upload_precondition_fn = None
        self.calls = []

    # ── Reads ─────────────────────────────────────────────────────────────
    def current_authority(self):
        self.calls.append("auth")
        if self.authority_raises:
            raise RuntimeError("control_authority unreachable")
        if self.authority_values:
            return self.authority_values.pop(0)
        return self.authority

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
        return {
            "usv_id": "usv-2",
            "telemetry": {"lat": self.lat, "lng": self.lon, "battery": 12,
                          "mode_name": self.mode_name, "armed": self.armed},
            "mavlink": {"heartbeat_age_s": self.heartbeat_age_s, "last_message_age_s": self.position_age_s},
            "mission": {"current_mission_id": self.vehicle_mission_id, "mission_active": self.mission_active,
                        "mission_active_evidence": self.mission_active_evidence,
                        "mission_active_evidence_age_s": self.mission_active_evidence_age_s,
                        "current_waypoint": self.current_seq, "mission_count": self.mission_count},
            "agent": {"control_authority": self.authority,
                      "home_status": {"verified": self.home_ok, "ready_for_auto": self.home_ok,
                                      "home_position": dict(_HOME)}},
        }

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
            "route_content_hash": self.pixhawk_route_hash,
            "route_waypoint_count": self.pixhawk_route_count,
            "error": self.pixhawk_error,
        }
        for key, val in (("proof_source", self.pixhawk_proof_source),
                         ("refresh_generation", self.pixhawk_refresh_generation),
                         ("stale", self.pixhawk_stale), ("refreshing", self.pixhawk_refreshing),
                         ("busy", self.pixhawk_busy), ("observed_at", self.pixhawk_observed_at),
                         ("age_s", self.pixhawk_age_s)):
            if val is not None:
                rb[key] = val
        return rb

    def prove_pixhawk_mission_readback(self, *a, **k):
        return self.pixhawk_mission_readback()

    def home_verified(self):
        self.calls.append("home")
        return self.home_ok

    # ── Writes ────────────────────────────────────────────────────────────
    def command_loiter(self):
        self.calls.append("loiter")
        if self.loiter_verified:
            self.mode_name = "LOITER"
        return {"verified": self.loiter_verified, "observed_mode": 5}

    def command_auto(self):
        self.calls.append("auto")
        if self.auto_verified:
            self.mode_name = "AUTO"
        return {"verified": self.auto_verified, "observed_mode": 10}

    def command_rtl(self):
        self.calls.append("rtl")
        return {"verified": self.rtl_verified, "observed_mode": 11}

    def upload_mission(self, route, command_id, upload_context="AGENT_REPLAN"):
        self.calls.append("upload")
        import route_hash
        h = route_hash.route_content_hash(route)
        r = dict(self.upload)
        r.setdefault("expected_route_content_hash", h)
        r.setdefault("observed_route_content_hash", h if r.get("verified") else None)
        r.setdefault("observed_route_waypoint_count", len(route))
        return r

    # ── HOLD-SETTLE precondition dry-run (E2 replan armed-LOITER upload race
    # fix) -- mirrors services/mission_upload_service.check_upload_
    # preconditions()'s response shape. Defaults to "already settled"
    # (groundspeed 0.0) so every EXISTING test keeps its exact behaviour
    # unchanged; TestHoldSettle below drives groundspeed_sequence /
    # allowed_override / raises to exercise the wait itself.
    def upload_preconditions(self, upload_context="AGENT_REPLAN"):
        self.calls.append("upload_preconditions")
        if self.upload_precondition_raises:
            raise RuntimeError("upload preconditions unreachable")
        if self.upload_precondition_fn is not None:
            return self.upload_precondition_fn(self)
        seq = self.upload_precondition_groundspeed_sequence
        if seq:
            gs = seq.pop(0) if len(seq) > 1 else seq[0]
        else:
            gs = 0.0
        threshold = self.upload_precondition_threshold
        armed = self.armed
        mode = self.mode_name
        allowed, reason = True, None
        if armed:
            if mode != "LOITER":
                allowed, reason = False, "ARMED_NOT_LOITER"
            elif gs is None:
                allowed, reason = False, "ARMED_LOITER_GROUNDSPEED_UNAVAILABLE"
            elif gs > threshold:
                allowed, reason = False, "ARMED_LOITER_GROUNDSPEED_TOO_HIGH"
        pre = {
            "armed": armed, "verified_mode": mode,
            "groundspeed_m_s": gs, "groundspeed_age_s": 0.2,
            "precondition_result": "ALLOW" if allowed else "REJECT",
            "precondition_failure_reason": reason,
        }
        return {
            "allowed": allowed,
            "error_code": None if allowed else "VEHICLE_ARMED",
            "error_message": None if allowed else f"not settled ({reason})",
            "preconditions": pre,
            "armed_loiter_max_groundspeed_m_s": threshold,
            "armed_loiter_max_groundspeed_age_s": 3.0,
        }

    @property
    def write_calls(self):
        return [c for c in self.calls if c in ("loiter", "auto", "rtl", "upload")]


class ExplodingWriteGateway(FakeGateway):
    """Reads (authority) are fine; any WRITE or vehicle/Pixhawk read raises --
    proves dry-run performs no vehicle writes and no progression/proof reads."""
    def command_loiter(self): raise AssertionError("dry-run wrote LOITER")
    def command_auto(self): raise AssertionError("dry-run wrote AUTO")
    def command_rtl(self): raise AssertionError("dry-run wrote RTL")
    def upload_mission(self, *a, **k): raise AssertionError("dry-run uploaded")
    def home_verified(self): raise AssertionError("dry-run read home")
    def read_vehicle_state(self): raise AssertionError("dry-run read vehicle state")
    def pixhawk_mission_readback(self): raise AssertionError("dry-run read Pixhawk")
    def prove_pixhawk_mission_readback(self, *a, **k): raise AssertionError("dry-run proved Pixhawk")
    def upload_preconditions(self, *a, **k): raise AssertionError("dry-run read upload preconditions")


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t


class AdvancingClock:
    """A monotonic clock the progression-watch tests drive deterministically:
    advance(dt) is wired in as the watch's _sleep so each poll interval moves
    virtual time forward with no real sleep -- the full deadline is exercised
    instantly."""
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


def _cfg(**overrides):
    base = dict(autonomous_execution_enabled=True, dry_run=False,
                max_transaction_retries=0, cooldown_s=30.0,
                connect_gap_max_m=150.0, energy_persistence_count=1,
                rtl_fallback_enabled=False,
                # HOLD-SETTLE (E2 replan armed-LOITER upload race fix):
                # persistence_s=0.0 means the FIRST allowed sample already
                # satisfies persistence, so the default "already settled"
                # FakeGateway.upload_preconditions() confirms in exactly one
                # poll with zero real sleep -- every existing test's timing/
                # call-count expectations are unaffected. TestHoldSettle
                # overrides these explicitly to exercise the wait itself.
                replan_hold_settle_timeout_s=2.0,
                replan_hold_settle_poll_interval_s=0.01,
                replan_hold_settle_persistence_s=0.0)
    base.update(overrides)
    return replan_config.ReplanConfig(**{**replan_config.ReplanConfig().to_dict(), **base})


def _energy(decision=energy_policy.DECISION_REPLAN_SAFE_RETURN, simulated=False):
    return energy_policy.EnergyResult(
        decision=decision, reason="", reason_codes=[energy_policy.CODE_CRITICAL_BATTERY],
        triggered_raw=True, persisted=decision == energy_policy.DECISION_REPLAN_SAFE_RETURN,
        consecutive_triggers=1, persistence_required=1, simulated=simulated,
        simulated_fields=[], inputs={"battery_percent": 12, "margin_percent": -5},
    )


# decision_policy.ActionRequest is the SOLE authoritative autonomous trigger
# into replan_controller.observe() (E2 water-trial integration task) --
# energy_policy.EnergyResult above is retained only as evidence/debounce/
# diagnostics. Most `observe()`-driven tests below therefore need an explicit
# ActionRequest to reach "start"; _energy()'s own `decision` no longer
# matters for triggering.
def _action_request(action, generation=1, snapshot_id="snap-1"):
    return decision_policy.ActionRequest(
        action=action,
        source_snapshot_id=snapshot_id,
        reason_codes=("CRITICAL",),
        risk_level="CRITICAL",
        recommendation=("RETURN_HOME" if action == decision_policy.ACTION_REQUEST_RETURN_HOME
                        else "HOLD" if action == decision_policy.ACTION_REQUEST_HOLD else "CONTINUE"),
        feasibility_evidence={"mission_feasible": False, "rtl_return_feasible": True, "status": "INFEASIBLE"},
        generation=generation,
        created_at=1000.0,
    )


def _return_request(**kw):
    return _action_request(decision_policy.ACTION_REQUEST_RETURN_HOME, **kw)


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        self.route_hash = _store_verified_package("m1")
        self.gw = FakeGateway()
        self.gw.pixhawk_route_hash = self.route_hash
        # The mission-execution controller's bound ORIGINAL mission identity that
        # the replan proof checks against (m1, matching hash/count).
        self.bound = {"mission_id": "m1", "original_route_hash": self.route_hash,
                      "original_route_count": len(_ROUTE)}

    def tearDown(self):
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))

    def _original_fn(self):
        return lambda: self.bound

    def _ctrl(self, cfg=None, clock=None, original_fn="default", **kw):
        import time as _time
        if original_fn == "default":
            original_fn = self._original_fn()
        return rc.ReplanController(cfg=cfg or _cfg(), gateway=self.gw,
                                   original_mission_fn=original_fn,
                                   clock=clock or _time.time, **kw)

    def _snapshot(self, authority="LOCAL_AGENT", lat=56.6520, lon=12.8700, seq=3):
        vs = {
            "usv_id": "usv-2",
            "telemetry": {"lat": lat, "lng": lon, "battery": 12, "mode_name": "AUTO", "armed": True},
            "mavlink": {"heartbeat_age_s": 0.3, "last_message_age_s": 0.2},
            "mission": {"current_mission_id": self.gw.vehicle_mission_id, "mission_active": True,
                        "current_waypoint": seq, "mission_count": 4},
            "agent": {"control_authority": authority,
                      "home_status": {"verified": True, "ready_for_auto": True,
                                      "home_position": _HOME}},
        }
        return dsm.build_snapshot(vs, "CONNECTED", authority, planning_package=pp.load())


# ── Success path ──────────────────────────────────────────────────────────────
class TestSuccess(_Base):
    def test_full_fsm_sequence(self):
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        states = [h["to"] for h in ctrl.status()["history"]]
        self.assertEqual(states, [
            rc.HOLD_REQUESTED, rc.HOLD_CONFIRMED, rc.PLANNING, rc.VALIDATING,
            rc.UPLOAD_REQUESTED, rc.VERIFYING_REVISION, rc.RESUME_REQUESTED,
            rc.MONITORING_REVISED,
        ])

    def test_progression_proven_via_active_true(self):
        ctrl = self._ctrl()
        ctrl.run_transaction(self._snapshot())
        st = ctrl.status()
        self.assertEqual(st["revised_progression"]["proof"], "A")
        self.assertTrue(st["revised_progression"]["proven"])

    def test_authority_checked_before_every_write(self):
        ctrl = self._ctrl()
        ctrl.run_transaction(self._snapshot())
        # An 'auth' precedes the top gate + upload + auto (three writes) -> >= 4.
        self.assertGreaterEqual(self.gw.calls.count("auth"), 4)
        first_write_idx = next(i for i, c in enumerate(self.gw.calls) if c in ("loiter", "upload", "auto"))
        self.assertIn("auth", self.gw.calls[:first_write_idx])

    def test_original_proof_runs_before_first_write(self):
        ctrl = self._ctrl()
        ctrl.run_transaction(self._snapshot())
        # The fresh Pixhawk readback ('pixhawk') precedes the first vehicle write.
        first_write = next(i for i, c in enumerate(self.gw.calls) if c in ("loiter", "upload", "auto"))
        self.assertIn("pixhawk", self.gw.calls[:first_write])

    def test_revision_hashes_and_status(self):
        ctrl = self._ctrl()
        ctrl.run_transaction(self._snapshot())
        st = ctrl.status()
        self.assertEqual(st["strategy"], "SAFE_RETURN_HOME")
        self.assertTrue(st["revised_mission_hash"].startswith("sha256:"))
        self.assertTrue(st["upload_outcome"]["verified"])
        self.assertTrue(st["readback_outcome"]["verified"])
        self.assertTrue(st["validation_outcome"]["valid"])
        self.assertEqual(st["revision_number"], 1)
        # Identity evidence recorded (CRITICAL ISSUE 2).
        self.assertEqual(st["original_mission_id"], "m1")
        self.assertEqual(st["original_route_count"], len(_ROUTE))
        self.assertTrue(st["original_mission_proof"]["proven"])
        self.assertIsNotNone(st["revised_route_count"])
        self.assertTrue(st["revised_mission_proof"]["verified"])

    def test_planner_evidence_recorded_on_revision(self):
        # Shortest-safe-return planner evidence (task: RECORDER) lands on the
        # MissionRevision so the thesis can report which strategy won without
        # re-deriving it -- never silently mislabeling a retrace "shortest".
        ctrl = self._ctrl()
        ctrl.run_transaction(self._snapshot())
        rev = ctrl._current_revision
        self.assertIn(rev.planner_strategy,
                      (srp.METHOD_SHORTEST, srp.METHOD_RETRACE_FALLBACK))
        self.assertIsNotNone(rev.planner_route_distance_m)
        self.assertIsInstance(rev.planner_direct_path_valid, bool)
        self.assertIsInstance(rev.planner_candidate_node_count, int)
        self.assertIsInstance(rev.planner_fallback_used, bool)
        self.assertIsInstance(rev.planner_runtime_s, float)


# ── Fresh pre-replan ORIGINAL-mission proof (CRITICAL ISSUE 2) ─────────────────
class TestOriginalMissionProof(_Base):
    def _run(self):
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        return ctrl, res

    def test_matching_id_and_fresh_hash_count_allows_replanning(self):
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)

    def test_null_vehicle_mission_id_allowed_when_bound_proven(self):
        # Flask current_mission_id null is fine IF the bound original mission id is
        # proven AND fresh Pixhawk hash/count match.
        self.gw.vehicle_mission_id = None
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)

    def test_null_bound_original_mission_fails_closed(self):
        self.bound = None
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_ID_MISMATCH)
        self.assertEqual(self.gw.write_calls, [])   # nothing written

    def test_missing_original_fn_fails_closed(self):
        ctrl = rc.ReplanController(cfg=_cfg(), gateway=self.gw, original_mission_fn=None)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_ID_MISMATCH)
        self.assertEqual(self.gw.write_calls, [])

    def test_package_mission_id_differs_from_bound_blocks(self):
        self.bound = {"mission_id": "other-mission", "original_route_hash": self.route_hash,
                      "original_route_count": len(_ROUTE)}
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_ID_MISMATCH)
        self.assertEqual(self.gw.write_calls, [])

    def test_fresh_pixhawk_hash_mismatch_blocks_before_writes(self):
        self.gw.pixhawk_route_hash = "sha256:" + "0" * 64
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_HASH_MISMATCH)
        self.assertEqual(self.gw.write_calls, [])   # no LOITER / upload / AUTO

    def test_fresh_pixhawk_count_mismatch_blocks_before_writes(self):
        self.gw.pixhawk_route_count = len(_ROUTE) + 5
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_COUNT_MISMATCH)
        self.assertEqual(self.gw.write_calls, [])

    def test_partial_readback_blocks(self):
        self.gw.pixhawk_partial = True
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_PROOF_UNAVAILABLE)
        self.assertEqual(self.gw.write_calls, [])

    def test_stale_readback_blocks(self):
        self.gw.pixhawk_stale = True
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_PROOF_UNAVAILABLE)
        self.assertEqual(self.gw.write_calls, [])

    def test_refreshing_readback_blocks(self):
        self.gw.pixhawk_refreshing = True
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_PROOF_UNAVAILABLE)
        self.assertEqual(self.gw.write_calls, [])

    def test_unavailable_readback_blocks(self):
        self.gw.pixhawk_reachable = False
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_PROOF_UNAVAILABLE)
        self.assertEqual(self.gw.write_calls, [])

    def test_readback_transport_error_blocks(self):
        self.gw.readback_error = RuntimeError("mavlink down")
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_PROOF_UNAVAILABLE)
        self.assertEqual(self.gw.write_calls, [])

    def test_stale_package_from_previous_mission_fails_before_writes(self):
        # A package for a DIFFERENT mission than the bound one is stored -> fail
        # closed before any write (its hash also won't match the Pixhawk).
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        _store_verified_package("m-old", route=_ROUTE, home=_HOME)
        # bound original is still m1 -> mission-id mismatch caught first.
        ctrl, res = self._run()
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_ID_MISMATCH)
        self.assertEqual(self.gw.write_calls, [])

    def test_no_failed_proof_path_uploads_or_autos(self):
        for mutate in (lambda: setattr(self.gw, "pixhawk_route_hash", "sha256:" + "1" * 64),
                       lambda: setattr(self.gw, "pixhawk_reachable", False),
                       lambda: setattr(self, "bound", None)):
            self.setUp()
            mutate()
            ctrl, res = self._run()
            self.assertNotIn("upload", self.gw.calls)
            self.assertNotIn("auto", self.gw.calls)


# ── LOITER never confirmed ────────────────────────────────────────────────────
class TestLoiterFailure(_Base):
    def test_loiter_not_verified_fails_without_upload(self):
        self.gw.loiter_verified = False
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], "LOITER_NOT_VERIFIED")
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)


# ── Post-LOITER failures all remain in LOITER (SAFE_HOLD) ──────────────────────
class TestPostLoiterFailures(_Base):
    def test_planner_failure_safe_hold(self):
        # Consistent package + proven original, but the vehicle is far from the
        # approved network -> the planner fails closed on the connector gap.
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot(lat=56.70, lon=12.95))
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(ctrl.status()["last_error"]["code"], "PLANNING_FAILED")
        self.assertIn("loiter", self.gw.calls)      # LOITER was confirmed
        self.assertNotIn("upload", self.gw.calls)

    def test_missing_package_fails_before_writes(self):
        # No package at all -> the ORIGINAL-mission proof fails closed BEFORE any
        # write (no LOITER), leaving the vehicle mode unchanged.
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.FAILED)
        self.assertEqual(ctrl.status()["last_error"]["code"], rc.ORIGINAL_MISSION_PROOF_UNAVAILABLE)
        self.assertEqual(self.gw.write_calls, [])

    def test_validation_failure_safe_hold(self):
        # A no-go zone spanning the FULL width of the navigable boundary at
        # this latitude band -- unlike a small zone the shortest-safe-return
        # planner could route around (see safe_return_planner tests), this
        # leaves genuinely no valid path within approved geometry, so BOTH
        # the shortest search and the retrace fallback fail validation
        # (after LOITER).
        zone = [[56.6513, 12.868], [56.6513, 12.872], [56.6517, 12.872], [56.6517, 12.868]]
        _store_verified_package("m1", route=_ROUTE, home=_HOME)
        pkg = pp.load()
        pkg["no_go_zones"] = [zone]
        pp.save_package(pkg)
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(ctrl.status()["last_error"]["code"], "VALIDATION_FAILED")
        self.assertNotIn("upload", self.gw.calls)

    def test_upload_failure_safe_hold(self):
        self.gw.upload = {"accepted": True, "uploaded": False, "verified": False}
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(ctrl.status()["last_error"]["code"], "UPLOAD_FAILED")
        self.assertNotIn("auto", self.gw.calls)

    def test_readback_mismatch_safe_hold(self):
        self.gw.upload = {"accepted": True, "uploaded": True, "verified": False}
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(ctrl.status()["last_error"]["code"], "READBACK_MISMATCH")
        self.assertNotIn("auto", self.gw.calls)

    def test_resume_failure_reasserts_loiter(self):
        self.gw.auto_verified = False
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(ctrl.status()["last_error"]["code"], "RESUME_FAILED")
        self.assertGreaterEqual(self.gw.calls.count("loiter"), 2)


# ── Shared progression verifier over the revised mission (CRITICAL ISSUE 1) ────
class TestRevisedProgression(_Base):
    def _watch_ctrl(self, timeout=10.0, poll=0.5):
        clock = AdvancingClock(1000.0)
        ctrl = self._ctrl(cfg=_cfg(revised_progression_timeout_s=timeout,
                                   progression_poll_interval_s=poll,
                                   progression_min_displacement_m=1.5),
                          clock=clock)
        ctrl._sleep = clock.advance
        return ctrl, clock

    def test_transient_active_unknown_then_sequence_advance_succeeds(self):
        # Revised AUTO with transient ACTIVE_UNKNOWN succeeds once the sequence
        # later advances -- must NOT fail on the first inactive sample.
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl()
        reads = {"n": 0}
        def advance(gw, i):
            reads["n"] += 1
            if reads["n"] >= 4:          # after a few UNKNOWN samples
                gw.current_seq = 2
        self.gw.on_state_read = advance
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertEqual(ctrl.status()["revised_progression"]["proof"], "B")

    def test_does_not_fail_on_single_inactive_sample(self):
        # A 10 s deadline: a never-progressing run must not fail at ~2-3 s.
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=10.0, poll=0.5)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(ctrl.status()["last_error"]["code"], "PROGRESSION_UNCONFIRMED")
        self.assertGreaterEqual(ctrl.status()["last_error"]["detail"]["actual_elapsed_s"], 10.0)

    def test_full_deadline_honoured(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=6.0, poll=0.4)
        res = ctrl.run_transaction(self._snapshot())
        ev = ctrl.status()["last_error"]["detail"]
        self.assertGreaterEqual(ev["actual_elapsed_s"], 6.0)
        self.assertGreaterEqual(ev["sample_count"], 13)   # ~ 6.0 / 0.4

    def test_timeout_enters_safe_hold_with_verified_loiter(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=4.0)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        # LOITER re-asserted after the timeout (>= 2 loiter commands) and mode LOITER.
        self.assertGreaterEqual(self.gw.calls.count("loiter"), 2)
        self.assertEqual(self.gw.mode_name, "LOITER")

    def test_delayed_active_true_succeeds(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl()
        reads = {"n": 0}
        def activate(gw, i):
            reads["n"] += 1
            if reads["n"] >= 4:
                gw.mission_active_evidence = "ACTIVE_TRUE"
        self.gw.on_state_read = activate
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertEqual(ctrl.status()["revised_progression"]["proof"], "A")

    def test_disarm_fails_immediately(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=10.0)
        reads = {"n": 0}
        def disarm(gw, i):
            reads["n"] += 1
            if reads["n"] >= 3:
                gw.armed = False
        self.gw.on_state_read = disarm
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(ctrl.status()["last_error"]["code"], "VEHICLE_DISARMED")
        self.assertLess(ctrl.status()["last_error"]["detail"]["actual_elapsed_s"], 5.0)

    def test_authority_loss_fails_immediately(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=10.0)
        reads = {"n": 0}
        def take(gw, i):
            reads["n"] += 1
            if reads["n"] >= 3:
                gw.authority = "OPERATOR"
        self.gw.on_state_read = take
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(ctrl.status()["last_error"]["code"], "AUTHORITY_LOST")

    def test_mode_leaving_auto_fails_immediately(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=10.0)
        reads = {"n": 0}
        def flip(gw, i):
            reads["n"] += 1
            if reads["n"] >= 3:
                gw.mode_name = "MANUAL"
        self.gw.on_state_read = flip
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(ctrl.status()["last_error"]["code"], "MODE_LEFT_AUTO")

    def test_gps_jitter_does_not_prove(self):
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        self.gw.lat = 56.6490
        def jitter(gw, i):
            gw.lat = 56.6490 + (0.000002 if i % 2 else -0.000002)  # ~0.2 m
        self.gw.on_state_read = jitter
        ctrl, _ = self._watch_ctrl(timeout=4.0)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(ctrl.status()["last_error"]["code"], "PROGRESSION_UNCONFIRMED")

    def test_uses_shared_mission_progression_verifier(self):
        # Proves Start/Resume/Replan share ONE verifier: the replan path calls
        # mission_progression.watch, not a private second copy.
        import mission_progression as mp
        calls = {"n": 0}
        real = mp.watch
        def spy(ctx, baseline, timeout_s):
            calls["n"] += 1
            return real(ctx, baseline, timeout_s)
        mp.watch = spy
        try:
            ctrl = self._ctrl()
            ctrl.run_transaction(self._snapshot())
        finally:
            mp.watch = real
        self.assertEqual(calls["n"], 1)

    def test_failure_does_not_report_returning_home(self):
        # A progression timeout must NOT reach MONITORING_REVISED (the state the
        # mission-execution handoff turns into RETURNING_HOME).
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.current_seq = 1
        ctrl, _ = self._watch_ctrl(timeout=4.0)
        res = ctrl.run_transaction(self._snapshot())
        self.assertNotEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)


# ── HOLD-SETTLE proof (E2 replan armed-LOITER upload race fix) ─────────────────
# HOLD_CONFIRMED proves MODE HOLD CONFIRMED (verified LOITER); these tests prove
# the transaction additionally waits for PHYSICAL HOLD SETTLED (fresh
# groundspeed at/below the armed-LOITER upload threshold) before ever spending
# an upload attempt -- and that the wait is bounded, never burns a transaction
# retry, and fails closed through the existing fallback hierarchy on timeout.
class TestHoldSettle(_Base):
    def _settle_ctrl(self, timeout=10.0, poll=1.0, persistence=1.0, **cfg_kw):
        clock = AdvancingClock(1000.0)
        ctrl = self._ctrl(cfg=_cfg(replan_hold_settle_timeout_s=timeout,
                                   replan_hold_settle_poll_interval_s=poll,
                                   replan_hold_settle_persistence_s=persistence,
                                   **cfg_kw),
                          clock=clock)
        ctrl._sleep = clock.advance
        return ctrl, clock

    def test_above_threshold_blocks_upload_until_it_settles(self):
        # 0.63 -> 0.55 -> 0.42 (repeats): no upload while > 0.5 m/s; exactly one
        # upload, once the fresh check reports settled and stays settled for
        # the required persistence window.
        self.gw.upload_precondition_groundspeed_sequence = [0.63, 0.55, 0.42]
        ctrl, _clock = self._settle_ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertEqual(self.gw.calls.count("upload"), 1)
        self.assertGreaterEqual(self.gw.calls.count("upload_preconditions"), 3)
        st = ctrl.status()
        self.assertTrue(st["hold_settle"]["confirmed"])
        speeds = [s["groundspeed"] for s in st["hold_settle"]["samples"]]
        self.assertIn(0.63, speeds)
        self.assertIn(0.42, speeds)

    def test_settle_wait_never_consumes_a_transaction_retry(self):
        self.gw.upload_precondition_groundspeed_sequence = [0.63, 0.55, 0.42]
        ctrl, _clock = self._settle_ctrl()
        ctrl.run_transaction(self._snapshot())
        st = ctrl.status()
        # A single, first-try PLANNING/VALIDATING/UPLOAD sequence -- the settle
        # wait's own polls (however many it took) never advance retry_count.
        self.assertEqual(st["retry_count"], 0)
        self.assertEqual(self.gw.calls.count("loiter"), 1)

    def test_never_settling_fails_closed_no_rtl_no_upload(self):
        # Fresh but permanently 0.63 m/s -- never crosses the threshold within
        # the bounded window.
        self.gw.upload_precondition_groundspeed_sequence = [0.63]
        ctrl, _clock = self._settle_ctrl(timeout=3.0, poll=1.0, persistence=1.0)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("rtl", self.gw.calls)
        self.assertIn("loiter", self.gw.calls)   # still safely holding

    def test_never_settling_falls_back_to_rtl_when_feasible(self):
        # Same never-settles condition, but with the existing RTL-fallback
        # hierarchy enabled and currently proven feasible -- HOLD-SETTLE
        # timing out must use the SAME fallback path as retries-exhausted.
        self.gw.upload_precondition_groundspeed_sequence = [0.63]
        ctrl, _clock = self._settle_ctrl(timeout=3.0, poll=1.0, persistence=1.0,
                                         rtl_fallback_enabled=True)
        ctrl._feasibility_fn = _feasibility_fn(True)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.FALLBACK_RTL)
        self.assertNotIn("upload", self.gw.calls)
        self.assertIn("rtl", self.gw.calls)

    def test_stale_groundspeed_never_treated_as_settled(self):
        self.gw.upload_precondition_fn = lambda gw: {
            "allowed": False, "error_code": "VEHICLE_ARMED",
            "error_message": "stale", "armed_loiter_max_groundspeed_m_s": 0.5,
            "armed_loiter_max_groundspeed_age_s": 3.0,
            "preconditions": {"armed": True, "verified_mode": "LOITER",
                              "groundspeed_m_s": 0.0, "groundspeed_age_s": 9.0,
                              "precondition_failure_reason": "ARMED_LOITER_STALE_GROUNDSPEED"},
        }
        ctrl, _clock = self._settle_ctrl(timeout=2.0, poll=1.0, persistence=1.0)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("upload", self.gw.calls)

    def test_unknown_groundspeed_never_treated_as_settled(self):
        self.gw.upload_precondition_fn = lambda gw: {
            "allowed": False, "error_code": "VEHICLE_ARMED",
            "error_message": "unavailable", "armed_loiter_max_groundspeed_m_s": 0.5,
            "armed_loiter_max_groundspeed_age_s": 3.0,
            "preconditions": {"armed": True, "verified_mode": "LOITER",
                              "groundspeed_m_s": None, "groundspeed_age_s": None,
                              "precondition_failure_reason": "ARMED_LOITER_GROUNDSPEED_UNAVAILABLE"},
        }
        ctrl, _clock = self._settle_ctrl(timeout=2.0, poll=1.0, persistence=1.0)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("upload", self.gw.calls)

    def test_disarmed_retains_existing_upload_semantics(self):
        # If the precondition check ever reports disarmed (e.g. an edge case
        # where the vehicle disarmed under LOITER), it is allowed exactly like
        # today's disarmed non-AUTO upload path -- settle confirms promptly.
        self.gw.upload_precondition_fn = lambda gw: {
            "allowed": True, "error_code": None, "error_message": None,
            "armed_loiter_max_groundspeed_m_s": 0.5, "armed_loiter_max_groundspeed_age_s": 3.0,
            "preconditions": {"armed": False, "verified_mode": "HOLD",
                              "groundspeed_m_s": None, "groundspeed_age_s": None,
                              "precondition_failure_reason": None},
        }
        ctrl, _clock = self._settle_ctrl(timeout=3.0, poll=1.0, persistence=1.0)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertEqual(self.gw.calls.count("upload"), 1)

    def test_already_settled_waits_only_the_required_persistence(self):
        # Already well below threshold on every sample -- confirmed after
        # exactly persistence_s, never the full timeout.
        self.gw.upload_precondition_groundspeed_sequence = [0.2]
        ctrl, clock = self._settle_ctrl(timeout=10.0, poll=1.0, persistence=1.0)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        st = ctrl.status()
        self.assertLessEqual(st["hold_settle"]["elapsed_s"], 1.5)

    def test_no_auto_before_upload_succeeds(self):
        self.gw.upload_precondition_groundspeed_sequence = [0.63, 0.55, 0.42]
        ctrl, _clock = self._settle_ctrl()
        ctrl.run_transaction(self._snapshot())
        idx_upload = self.gw.calls.index("upload")
        self.assertNotIn("auto", self.gw.calls[:idx_upload])
        for i, call in enumerate(self.gw.calls):
            if call == "upload_preconditions":
                self.assertLess(i, idx_upload)

    def test_authority_lost_during_settle_wait_suspends(self):
        self.gw.upload_precondition_groundspeed_sequence = [0.63, 0.63, 0.63]
        self.gw.authority_values = ["LOCAL_AGENT", "LOCAL_AGENT", "OPERATOR"]
        ctrl, _clock = self._settle_ctrl(timeout=5.0, poll=1.0, persistence=1.0)
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SUSPENDED)
        self.assertNotIn("upload", self.gw.calls)

    def test_dry_run_never_calls_upload_preconditions(self):
        ctrl = rc.ReplanController(cfg=_cfg(dry_run=True), gateway=ExplodingWriteGateway(),
                                   original_mission_fn=self._original_fn())
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)


# ── HOLD-ONLY communication path uses the SAME HOLD-SETTLE verifier ────────────
# Regression coverage for the DISCONNECTED/REQUEST_HOLD/SAFE_HOLD investigation:
# E3 (run-20260821-142036-usv-2-1506378a) reached SAFE_HOLD ~1.167s after
# HOLD_REQUESTED with zero HOLD-SETTLE polls -- the HOLD-only branch returned
# straight from mode-verified HOLD_CONFIRMED to _direct_safe_hold() BEFORE
# _acquire_hold_settle() was ever reached. These tests prove a HOLD-only
# ActionRequest (decision_policy.ACTION_REQUEST_HOLD) is now bound by the exact
# same bounded, polled, persistence-gated physical-settle proof the energy
# REQUEST_RETURN_HOME path already used -- mode == LOITER alone is never
# sufficient, and a HOLD-only request still never attempts RTL/PLANNING/UPLOAD,
# settled or timed out.
class TestHoldOnlySettle(_Base):
    def _hold_settle_ctrl(self, timeout=10.0, poll=1.0, persistence=1.0, **cfg_kw):
        clock = AdvancingClock(1000.0)
        ctrl = self._ctrl(cfg=_cfg(replan_hold_settle_timeout_s=timeout,
                                   replan_hold_settle_poll_interval_s=poll,
                                   replan_hold_settle_persistence_s=persistence,
                                   **cfg_kw),
                          clock=clock)
        ctrl._sleep = clock.advance
        return ctrl, clock

    def _trigger_hold(self, ctrl, snap):
        req = _action_request(decision_policy.ACTION_REQUEST_HOLD)
        dec = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=req)
        self.assertTrue(dec["start"], dec.get("reason"))

    def test_1_mode_loiter_alone_does_not_reach_safe_hold_while_speed_stays_high(self):
        # Mode becomes LOITER immediately (FakeGateway.command_loiter), but
        # groundspeed stays above the 0.5 m/s threshold for the entire bounded
        # wait. SAFE_HOLD must NEVER be declared on mode confirmation alone --
        # this must poll several times, then fail closed WITHOUT ever
        # claiming SAFE_HOLD (INVARIANT: SAFE_HOLD == positively proven).
        self.gw.upload_precondition_groundspeed_sequence = [0.9, 0.8, 0.7, 0.65]
        ctrl, _clock = self._hold_settle_ctrl(timeout=10.0, poll=1.0, persistence=1.0)
        snap = self._snapshot()
        self._trigger_hold(ctrl, snap)
        res = ctrl.run_transaction(snap)
        # Every sample stayed above threshold -- never settles within the
        # bound. This must fail closed to SUSPENDED, NOT SAFE_HOLD, and only
        # AFTER the bounded wait actually polled several times, never off the
        # first LOITER confirmation.
        self.assertEqual(res["outcome"], rc.SUSPENDED)
        self.assertNotEqual(ctrl.status()["fsm_state"], rc.SAFE_HOLD)
        self.assertGreaterEqual(self.gw.calls.count("upload_preconditions"), 3)
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        self.assertNotIn("rtl", self.gw.calls)
        st = ctrl.status()
        self.assertFalse(st["hold_settle"]["confirmed"])
        self.assertEqual(st["last_error"]["code"], "HOLD_SETTLE_TIMEOUT")
        self.assertIn("HOLD-SETTLE not proven", st["history"][-1]["reason"])

    def test_2_speed_drops_below_threshold_and_persists_then_safe_hold_succeeds(self):
        self.gw.upload_precondition_groundspeed_sequence = [0.9, 0.7, 0.42]
        ctrl, _clock = self._hold_settle_ctrl(timeout=10.0, poll=1.0, persistence=1.0)
        snap = self._snapshot()
        self._trigger_hold(ctrl, snap)
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        st = ctrl.status()
        self.assertTrue(st["hold_settle"]["confirmed"])
        speeds = [s["groundspeed"] for s in st["hold_settle"]["samples"]]
        self.assertIn(0.9, speeds)
        self.assertIn(0.42, speeds)
        self.assertIn("loiter", self.gw.calls)
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        self.assertNotIn("rtl", self.gw.calls)

    def test_3_never_settling_before_timeout_fails_closed_never_rtl_never_safe_hold(self):
        # Permanently above threshold, RTL fallback enabled AND currently
        # feasible: a HOLD-only request must NEVER attempt RTL, timeout or
        # not -- and a timed-out settle must NEVER be certified as SAFE_HOLD.
        self.gw.upload_precondition_groundspeed_sequence = [0.63]
        ctrl, _clock = self._hold_settle_ctrl(timeout=3.0, poll=1.0, persistence=1.0,
                                              rtl_fallback_enabled=True)
        ctrl._feasibility_fn = _feasibility_fn(True)
        snap = self._snapshot()
        self._trigger_hold(ctrl, snap)
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.SUSPENDED)
        self.assertNotEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("rtl", self.gw.calls)
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        st = ctrl.status()
        self.assertFalse(st["hold_settle"]["confirmed"])
        self.assertEqual(st["last_error"]["code"], "HOLD_SETTLE_TIMEOUT")
        reason = st["history"][-1]["reason"]
        self.assertIn("HOLD-SETTLE not proven", reason)
        # Mission remains conservatively suspended -- in an IDLE terminal
        # state, never mid-transaction, never retried automatically.
        self.assertIn(rc.SUSPENDED, rc._IDLE_STATES)

    def test_4_stale_speed_sample_below_threshold_is_not_accepted_as_settled(self):
        # groundspeed_m_s reads 0.0 (well below threshold) but its age (9s)
        # exceeds the freshness bound -- must NOT count as valid settle proof,
        # and must NOT be certified SAFE_HOLD once the bound times out.
        self.gw.upload_precondition_fn = lambda gw: {
            "allowed": False, "error_code": "VEHICLE_ARMED",
            "error_message": "stale", "armed_loiter_max_groundspeed_m_s": 0.5,
            "armed_loiter_max_groundspeed_age_s": 3.0,
            "preconditions": {"armed": True, "verified_mode": "LOITER",
                              "groundspeed_m_s": 0.0, "groundspeed_age_s": 9.0,
                              "precondition_failure_reason": "ARMED_LOITER_STALE_GROUNDSPEED"},
        }
        ctrl, _clock = self._hold_settle_ctrl(timeout=2.0, poll=1.0, persistence=1.0)
        snap = self._snapshot()
        self._trigger_hold(ctrl, snap)
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.SUSPENDED)
        st = ctrl.status()
        self.assertFalse(st["hold_settle"]["confirmed"])
        self.assertEqual(st["last_error"]["code"], "HOLD_SETTLE_TIMEOUT")

    def test_7_final_defensive_reassert_failure_is_never_certified_safe_hold(self):
        # HOLD-SETTLE proves settled normally, but the LAST defensive
        # _ensure_loiter() re-assertion (immediately before certifying the
        # hold) itself fails to verify -- SAFE_HOLD must NOT be entered. The
        # FIRST command_loiter() (HOLD_REQUESTED -> HOLD_CONFIRMED) succeeds
        # as normal; only the LATER defensive re-assert call fails.
        self.gw.upload_precondition_groundspeed_sequence = [0.42]
        ctrl, _clock = self._hold_settle_ctrl(timeout=10.0, poll=1.0, persistence=1.0)
        snap = self._snapshot()
        self._trigger_hold(ctrl, snap)
        first_loiter = self.gw.command_loiter
        call_count = {"n": 0}

        def flaky_loiter():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return first_loiter()
            self.gw.calls.append("loiter")
            return {"verified": False, "observed_mode": 5}

        self.gw.command_loiter = flaky_loiter
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.SUSPENDED)
        self.assertNotEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotEqual(ctrl.status()["fsm_state"], rc.SAFE_HOLD)
        st = ctrl.status()
        # HOLD-SETTLE itself DID confirm (the precondition dry-run read is
        # independent of the later defensive command_loiter() re-assertion).
        self.assertTrue(st["hold_settle"]["confirmed"])
        self.assertEqual(st["last_error"]["code"], "LOITER_REASSERT_NOT_VERIFIED")
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("rtl", self.gw.calls)

    def test_5_energy_return_home_path_still_uses_shared_verifier_unaffected(self):
        # Existing energy REQUEST_RETURN_HOME behaviour is unchanged by the
        # reorder: it already called _acquire_hold_settle() before this fix
        # and continues to.
        self.gw.upload_precondition_groundspeed_sequence = [0.63, 0.55, 0.42]
        ctrl, _clock = self._hold_settle_ctrl(timeout=10.0, poll=1.0, persistence=1.0)
        snap = self._snapshot()
        req = _action_request(decision_policy.ACTION_REQUEST_RETURN_HOME)
        dec = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=req)
        self.assertTrue(dec["start"])
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertEqual(self.gw.calls.count("upload"), 1)
        self.assertFalse(ctrl.status()["hold_only"])

    def test_6_retries_separated_by_configured_poll_interval_not_back_to_back(self):
        # Deterministic controlled clock/sleep (AdvancingClock wired as
        # ctrl._sleep): each poll advances virtual time by exactly the
        # configured poll interval. Proves the wait is a genuine bounded
        # WAIT keyed to elapsed wall-clock time -- never a fixed handful of
        # back-to-back samples executed within milliseconds (the old
        # ~3-polls-in-70ms class of bug).
        self.gw.upload_precondition_groundspeed_sequence = [0.9, 0.8, 0.7, 0.42]
        ctrl, _clock = self._hold_settle_ctrl(timeout=10.0, poll=0.5, persistence=1.0)
        snap = self._snapshot()
        self._trigger_hold(ctrl, snap)
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        samples = ctrl.status()["hold_settle"]["samples"]
        self.assertGreaterEqual(len(samples), 4)
        elapsed = [s["elapsed_s"] for s in samples]
        for a, b in zip(elapsed, elapsed[1:]):
            self.assertAlmostEqual(b - a, 0.5, places=3)


# ── Bounded retries ────────────────────────────────────────────────────────────
class TestRetries(_Base):
    def test_bounded_retries(self):
        self.gw.upload = {"accepted": True, "uploaded": False, "verified": False}
        ctrl = self._ctrl(cfg=_cfg(max_transaction_retries=2))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(self.gw.calls.count("upload"), 3)
        self.assertEqual(ctrl.status()["retry_count"], 2)


# ── RTL fallback ────────────────────────────────────────────────────────────────
def _feasibility_fn(rtl_return_feasible):
    """A feasibility_fn callback returning a fixed rtl_return_feasible value
    (True/False/None), the same shape decision_policy.ActionRequest.
    feasibility_evidence/mission_feasibility.MissionFeasibilityResult.to_dict()
    carry it as."""
    return lambda: {"mission_feasible": True, "rtl_return_feasible": rtl_return_feasible,
                    "status": "FEASIBLE" if rtl_return_feasible else "UNKNOWN"}


class TestFallback(_Base):
    """RTL fallback (E2 water-trial integration task section 15/16): a
    verified Home is necessary but NOT sufficient -- RTL additionally
    requires PROVEN CURRENT rtl_return_feasible is True. False or Unknown
    (no feasibility_fn wired, or it raises/returns something malformed) must
    both fail closed to SAFE_HOLD, never a blind RTL."""

    def test_rtl_fallback_when_enabled_home_verified_and_feasible(self):
        self.gw.upload = {"accepted": True, "uploaded": True, "verified": False}
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.FALLBACK_RTL)
        self.assertIn("rtl", self.gw.calls)

    def test_no_fallback_when_disabled(self):
        self.gw.upload = {"accepted": True, "uploaded": True, "verified": False}
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=False),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("rtl", self.gw.calls)

    def test_no_fallback_when_home_unverified(self):
        self.gw.upload = {"accepted": True, "uploaded": True, "verified": False}
        self.gw.home_ok = False
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("rtl", self.gw.calls)

    def test_no_fallback_when_rtl_infeasible(self):
        # Verified Home + fallback enabled, but current rtl_return_feasible
        # is proven False (e.g. not enough energy left to even reach Home) --
        # must fail closed to SAFE_HOLD, never a blind RTL.
        self.gw.upload = {"accepted": True, "uploaded": True, "verified": False}
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(False))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("rtl", self.gw.calls)
        self.assertEqual(ctrl.status()["last_error"]["code"], "RTL_FALLBACK_INFEASIBLE")

    def test_no_fallback_when_rtl_feasibility_unknown(self):
        # Same, but feasibility is UNKNOWN (None) rather than proven False --
        # "unproven" must be treated exactly as conservatively as "proven
        # infeasible", never defaulted to permissive.
        self.gw.upload = {"accepted": True, "uploaded": True, "verified": False}
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(None))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("rtl", self.gw.calls)
        self.assertEqual(ctrl.status()["last_error"]["code"], "RTL_FALLBACK_INFEASIBLE")

    def test_no_fallback_when_feasibility_fn_not_wired(self):
        # No feasibility_fn at all (fails closed to None/UNKNOWN) -- the
        # default in these tests' _ctrl() helper unless explicitly wired,
        # and realistically what happens if the caller ever forgets to wire
        # it: must NOT silently default to permitting RTL.
        self.gw.upload = {"accepted": True, "uploaded": True, "verified": False}
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True))  # no feasibility_fn
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("rtl", self.gw.calls)

    def test_rtl_fallback_failure_falls_to_safe_hold(self):
        self.gw.upload = {"accepted": True, "uploaded": True, "verified": False}
        self.gw.rtl_verified = False
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(ctrl.status()["last_error"]["code"], "RTL_FALLBACK_FAILED")

    def test_successful_constrained_return_never_commands_rtl(self):
        # E2 success-case proof: with the real tomorrow-night config
        # (rtl_fallback_enabled=True, a proven-feasible RTL) and the DEFAULT
        # gateway (upload/readback succeed on the first try, i.e. the
        # constrained RETRACE_APPROVED path succeeds), native RTL must never
        # be commanded -- fallback is reachable code, not a default action.
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertNotIn("rtl", self.gw.calls)


# ── no_go_clearance_m: RETRACE_APPROVED end-to-end through the FSM ─────────────
# The retrace line here is _ROUTE/_HOME's own straight north-south leg (lon
# ~12.8700). A thin no-go strip is placed 2 m east of that line -- clear of the
# RAW polygon (no_go_clearance_m=0 passes) but inside a 5 m buffered exclusion
# (no_go_clearance_m=5 fails) -- so these tests isolate the clearance
# requirement itself, not merely "some no-go geometry exists".
class TestNoGoClearanceRetrace(_Base):
    _ZONE_FAR = [[56.6495, 12.8730], [56.6495, 12.8740],
                [56.6525, 12.8740], [56.6525, 12.8730]]
    _ZONE_TIGHT = [[56.6495, 12.870032681464282], [56.6495, 12.870163407321417],
                  [56.6525, 12.870163407321417], [56.6525, 12.870032681464282]]

    def _store(self, zone, clearance_m):
        _store_verified_package("m1", route=_ROUTE, home=_HOME)
        pkg = pp.load()
        pkg["no_go_zones"] = [zone]
        pkg["no_go_clearance_m"] = clearance_m
        pp.save_package(pkg)

    def test_g_retrace_succeeds_with_clearance_and_never_commands_rtl(self):
        # G: a no-go zone far enough away that the buffered (5 m) exclusion is
        # still cleared -- RETRACE_APPROVED succeeds, no revised segment enters
        # the buffer, and native RTL is never selected (rtl_fallback enabled +
        # proven feasible, to prove it genuinely wasn't needed, not merely
        # unavailable).
        self._store(self._ZONE_FAR, 5.0)
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertNotIn("rtl", self.gw.calls)
        st = ctrl.status()
        self.assertTrue(st["validation_outcome"]["valid"])
        geometry = st["geometry_validation"]
        self.assertEqual(geometry["no_go_clearance_m"], 5.0)
        self.assertTrue(geometry["no_go_checked"])
        self.assertEqual(st["planning_package"]["no_go_clearance_m"], 5.0)
        self.assertEqual(st["planning_package"]["no_go_zone_count"], 1)
        # H (actual-route energy task): the actual-route energy recheck runs
        # for every successful transaction, no-go retrace included, and does
        # not itself cause any native RTL -- it only ever blocks an INFEASIBLE/
        # UNKNOWN actual route from uploading.
        checks = st["validation_outcome"]["checks"]
        self.assertEqual(checks["revised_route_energy_status"], "FEASIBLE")
        self.assertIsNotNone(checks["revised_route_distance_m"])
        self.assertIsNotNone(checks["revised_route_margin_percent"])

    def test_h_insufficient_clearance_fails_closed_raw_zero_would_pass(self):
        # H: the SAME zone/route passes with no_go_clearance_m=0 (raw-zone-only
        # exclusion, unchanged legacy behaviour)...
        self._store(self._ZONE_TIGHT, 0.0)
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)

    def test_h_insufficient_clearance_fails_closed(self):
        # ...but at no_go_clearance_m=5.0 the only approved geometry (the
        # straight retrace leg) runs inside the buffered exclusion -- no safe
        # retrace exists outside it, so the constrained return fails closed
        # (VALIDATION_FAILED) rather than silently reverting to the raw check.
        self._store(self._ZONE_TIGHT, 5.0)
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertEqual(ctrl.status()["last_error"]["code"], "VALIDATION_FAILED")
        self.assertNotIn("upload", self.gw.calls)

    def test_i_existing_fallback_hierarchy_unchanged_on_clearance_failure(self):
        # I: a no-go-clearance-caused validation failure is just another
        # VALIDATION_FAILED to the retry/fallback machinery -- once retries are
        # exhausted, the EXISTING fallback hierarchy (rtl_fallback_enabled +
        # verified Home + proven-feasible RTL) still governs unchanged: with
        # all three satisfied, verified RTL still engages.
        self._store(self._ZONE_TIGHT, 5.0)
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.FALLBACK_RTL)
        self.assertIn("rtl", self.gw.calls)


# ── Actual revised-route energy recheck (task: revised-route energy
#    feasibility recheck) -- AFTER geometry validation, BEFORE upload ─────────
# A "detour" package: a no-go zone genuinely (not merely by dumb-algorithm
# choice -- see safe_return_planner's shortest-safe-return preference) blocks
# the direct current->Home line, so the shortest SAFE route the approved
# geometry actually proves is substantially LONGER than the direct
# current->Home distance -- exactly the actual-route-vs-direct-estimate gap
# the task targets.
#   Home         = (56.6490, 12.8700)
#   WP1          = (56.6520, 12.8700)   north of Home  (~333 m)
#   WP2          = (56.6520, 12.8800)   east of WP1    (~611 m)
#   WP3          = (56.6490, 12.8800)   south of WP2   (~333 m)  == current position
# direct current(WP3)->Home distance ~ 611 m (straight line west) -- BLOCKED by
# _DETOUR_ZONE, a no-go strip spanning the boundary from its south edge up to
# just below WP1/WP2's latitude, leaving a gap only to the north. The
# shortest-safe-return planner's own visibility-graph search (not a fixed
# WP1/WP2 waypoint retrace) finds the shortest valid way around that gap --
# empirically ~911 m (see test_b), close to but independent of the old
# hand-authored ~1277 m two-leg dogleg.
_DETOUR_HOME = {"latitude": 56.6490, "longitude": 12.8700}
_DETOUR_ROUTE = [
    {"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_OUTBOUND_TRANSIT},
    {"latitude": 56.6520, "longitude": 12.8800, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
    {"latitude": 56.6490, "longitude": 12.8800, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
]
_DETOUR_BOUNDARY = [[56.646, 12.868], [56.646, 12.882], [56.654, 12.882], [56.654, 12.868]]
_DETOUR_ZONE = [[56.646, 12.8730], [56.646, 12.8770], [56.6515, 12.8770], [56.6515, 12.8730]]


class TestActualRouteEnergyRecheck(_Base):
    def _store_detour(self):
        route_hash_val = _store_verified_package("m1", route=_DETOUR_ROUTE, home=_DETOUR_HOME)
        pkg = pp.load()
        pkg["navigable_boundary"] = _DETOUR_BOUNDARY
        pkg["no_go_zones"] = [_DETOUR_ZONE]
        pp.save_package(pkg)
        self.route_hash = route_hash_val
        self.gw.pixhawk_route_hash = route_hash_val
        self.bound = {"mission_id": "m1", "original_route_hash": route_hash_val,
                      "original_route_count": len(_DETOUR_ROUTE)}

    def _detour_snapshot(self, battery=8, authority="LOCAL_AGENT"):
        # current position == WP3 (all three waypoints already traversed).
        vs = {
            "usv_id": "usv-2",
            "telemetry": {"lat": 56.6490, "lng": 12.8800, "battery": battery,
                          "mode_name": "AUTO", "armed": True},
            "mavlink": {"heartbeat_age_s": 0.3, "last_message_age_s": 0.2},
            "mission": {"current_mission_id": self.gw.vehicle_mission_id, "mission_active": True,
                        "current_waypoint": 3, "mission_count": 4},
            "agent": {"control_authority": authority,
                      "home_status": {"verified": True, "ready_for_auto": True,
                                      "home_position": _DETOUR_HOME}},
        }
        return dsm.build_snapshot(vs, "CONNECTED", authority, planning_package=pp.load())

    # A -- short constrained route: initial estimate feasible, actual route
    # (the default _ROUTE/_HOME ~333 m retrace) feasible too -> upload
    # proceeds, no RTL fallback.
    def test_a_short_feasible_actual_route_uploads_no_rtl(self):
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertNotIn("rtl", self.gw.calls)
        checks = ctrl.status()["validation_outcome"]["checks"]
        self.assertEqual(checks["revised_route_energy_status"], "FEASIBLE")

    # B -- longer constrained route (the detour package, ~911 m actual, longer
    # than the ~611 m direct current->Home estimate the no-go zone blocks) but
    # still comfortably above the emergency reserve at 40% battery -> upload
    # proceeds, evidence carries the actual distance/margin.
    def test_b_longer_but_feasible_actual_route_uploads(self):
        self._store_detour()
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._detour_snapshot(battery=40))
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        self.assertNotIn("rtl", self.gw.calls)
        checks = ctrl.status()["validation_outcome"]["checks"]
        self.assertEqual(checks["revised_route_energy_status"], "FEASIBLE")
        self.assertGreater(checks["revised_route_distance_m"], 611)   # longer than direct
        self.assertGreater(checks["revised_route_margin_percent"], 0)

    # C -- THE KEY REGRESSION: the initial/direct return estimate says
    # feasible (feasibility_fn(True) is mocked True regardless of distance),
    # but at 8% battery the ACTUAL shortest-safe-return detour around the
    # no-go zone (~911 m, vs. mission_feasibility.py's field-calibrated
    # design current/speed -- see replan_config.py) is long enough that the
    # existing return-energy model says INFEASIBLE -> the revised mission
    # must NOT be uploaded. (Battery level chosen to cross zero margin under
    # the current calibrated conservative_current_A/design_speed_mps -- see
    # _detour_snapshot's default; NOT tied to any named battery threshold.)
    def test_c_actual_detour_infeasible_blocks_upload(self):
        self._store_detour()
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=False),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._detour_snapshot(battery=8))
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("rtl", self.gw.calls)
        err = ctrl.status()["last_error"]
        self.assertEqual(err["reason_code"], "INSUFFICIENT_ENERGY_FOR_RTL_RETURN")
        checks = ctrl.status()["validation_outcome"]["checks"]
        self.assertEqual(checks["revised_route_energy_status"], "INFEASIBLE")
        self.assertLessEqual(checks["revised_route_margin_percent"], 0)

    # D -- actual detour infeasible, but native RTL is CURRENTLY proven
    # feasible and verified Home is available -> the existing fallback
    # hierarchy still engages native RTL (never a blind RTL -- proven here via
    # feasibility_fn(True)).
    def test_d_actual_detour_infeasible_native_rtl_feasible_falls_back(self):
        self._store_detour()
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._detour_snapshot(battery=8))
        self.assertEqual(res["outcome"], rc.FALLBACK_RTL)
        self.assertIn("rtl", self.gw.calls)
        self.assertNotIn("upload", self.gw.calls)

    # E -- actual detour infeasible AND native RTL infeasible (proven False,
    # never a blind RTL on unproven/unknown) -> SAFE_HOLD/LOITER, no upload,
    # no RTL command.
    def test_e_actual_detour_infeasible_native_rtl_infeasible_safe_holds(self):
        self._store_detour()
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=True),
                          feasibility_fn=_feasibility_fn(False))
        res = ctrl.run_transaction(self._detour_snapshot(battery=8))
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("rtl", self.gw.calls)

    # F -- actual-route energy UNKNOWN (no valid battery reading, ArduPilot
    # battery_remaining == -1) on an otherwise short/feasible route -> fail
    # closed exactly like INFEASIBLE: no upload, existing fallback hierarchy
    # only (never coerced to FEASIBLE just because a distance was computable).
    def test_f_actual_route_energy_unknown_fails_closed(self):
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=False),
                          feasibility_fn=_feasibility_fn(True))
        res = ctrl.run_transaction(self._snapshot_with_invalid_battery())
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("upload", self.gw.calls)
        checks = ctrl.status()["validation_outcome"]["checks"]
        self.assertEqual(checks["revised_route_energy_status"], "UNKNOWN")
        self.assertIsNone(checks["revised_route_margin_percent"])

    def _snapshot_with_invalid_battery(self):
        vs = {
            "usv_id": "usv-2",
            "telemetry": {"lat": 56.6520, "lng": 12.8700, "battery": -1,
                          "mode_name": "AUTO", "armed": True},
            "mavlink": {"heartbeat_age_s": 0.3, "last_message_age_s": 0.2},
            "mission": {"current_mission_id": self.gw.vehicle_mission_id, "mission_active": True,
                        "current_waypoint": 3, "mission_count": 4},
            "agent": {"control_authority": "LOCAL_AGENT",
                      "home_status": {"verified": True, "ready_for_auto": True, "home_position": _HOME}},
        }
        return dsm.build_snapshot(vs, "CONNECTED", "LOCAL_AGENT", planning_package=pp.load())

    # G -- ordering proof: the actual-route energy check runs BEFORE upload --
    # an energy rejection means zero upload calls (not merely "no successful
    # upload"), proven directly on the gateway's own call log.
    def test_g_energy_rejection_makes_zero_upload_calls(self):
        self._store_detour()
        ctrl = self._ctrl(cfg=_cfg(rtl_fallback_enabled=False),
                          feasibility_fn=_feasibility_fn(True))
        ctrl.run_transaction(self._detour_snapshot(battery=8))
        self.assertEqual(self.gw.calls.count("upload"), 0)


# ── Authority ───────────────────────────────────────────────────────────────────
class TestAuthority(_Base):
    def test_observe_blocked_when_operator(self):
        ctrl = self._ctrl()
        dec = ctrl.observe(self._snapshot(authority="OPERATOR"), _energy(), action_request=_return_request())
        self.assertFalse(dec["start"])
        self.assertEqual(dec["reason"], rc.BLOCKED_BY_AUTHORITY)
        st = ctrl.status()
        self.assertTrue(st["authority_blocked"])
        self.assertEqual(st["blocked_recommendation"], rc.BLOCKED_BY_AUTHORITY)

    def test_observe_disabled_master_switch(self):
        ctrl = self._ctrl(cfg=_cfg(autonomous_execution_enabled=False))
        dec = ctrl.observe(self._snapshot(), _energy(), action_request=_return_request())
        self.assertFalse(dec["start"])
        self.assertEqual(dec["reason"], "autonomous execution disabled")

    def test_observe_starts_when_permitted(self):
        ctrl = self._ctrl()
        dec = ctrl.observe(self._snapshot(), _energy(), action_request=_return_request())
        self.assertTrue(dec["start"])

    def test_energy_alone_without_action_request_never_starts(self):
        # decision_policy.ActionRequest is the SOLE authoritative trigger (E2
        # water-trial integration task) -- energy_policy.EnergyResult is
        # retained only as evidence/debounce/diagnostics. Even a maximally
        # "wants a return" energy decision must never start a transaction on
        # its own.
        ctrl = self._ctrl()
        dec = ctrl.observe(self._snapshot(), _energy(decision=energy_policy.DECISION_REPLAN_SAFE_RETURN))
        self.assertFalse(dec["start"])
        self.assertEqual(dec["reason"], "no replan required")
        self.assertFalse(ctrl.is_running())

    def test_authority_lost_mid_transaction_suspends(self):
        # LOCAL_AGENT for the top gate and pre-PLANNING, then OPERATOR at pre-UPLOAD.
        self.gw.authority_values = ["LOCAL_AGENT", "LOCAL_AGENT", "OPERATOR", "OPERATOR"]
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SUSPENDED)
        self.assertIn("loiter", self.gw.calls)     # LOITER happened
        self.assertNotIn("upload", self.gw.calls)   # writes stopped before upload

    def test_authority_unknown_fails_closed_before_loiter(self):
        self.gw.authority_raises = True
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SUSPENDED)
        self.assertNotIn("loiter", self.gw.calls)   # never wrote

    def test_authority_lost_before_resume_blocks_auto_after_successful_upload(self):
        # P0-2: Take Control specifically at the pre-RESUME gate, AFTER the
        # revised route has already been uploaded/verified -- AUTO must
        # still be blocked. This is distinct from test_authority_lost_mid_
        # transaction_suspends (which loses authority before upload ever
        # runs) -- here the upload succeeds and only the final AUTO write is
        # denied.
        # Five 'auth' gates precede RESUME on the happy path: the top gate,
        # the HOLD-SETTLE proof, pre-PLANNING, pre-UPLOAD, then pre-RESUME --
        # only the LAST one returns OPERATOR.
        self.gw.authority_values = ["LOCAL_AGENT", "LOCAL_AGENT", "LOCAL_AGENT",
                                    "LOCAL_AGENT", "OPERATOR"]
        ctrl = self._ctrl()
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.SUSPENDED)
        self.assertIn("upload", self.gw.calls)     # the revised route WAS uploaded
        self.assertNotIn("auto", self.gw.calls)    # but AUTO never followed


# ── Duplicate suppression ───────────────────────────────────────────────────────
class TestDuplicateSuppression(_Base):
    def test_run_suppressed_while_action_lock_held(self):
        ctrl = self._ctrl()
        ctrl._action_lock.acquire()
        try:
            res = ctrl.run_transaction(self._snapshot())
            self.assertFalse(res["started"])
        finally:
            ctrl._action_lock.release()

    def test_observe_reports_in_progress_while_locked(self):
        ctrl = self._ctrl()
        ctrl._action_lock.acquire()
        try:
            dec = ctrl.observe(self._snapshot(), _energy())
            self.assertFalse(dec["start"])
            self.assertIn("in progress", dec["reason"])
        finally:
            ctrl._action_lock.release()


# ── Cooldown ────────────────────────────────────────────────────────────────────
class TestCooldown(_Base):
    def test_cooldown_expiry_alone_does_not_retrigger(self):
        # NEW CONTRACT (task section 1): the old auto-retry-after-cooldown is gone.
        # An observe assigns generation 1; the transaction consumes it; an
        # unchanged still-active trigger stays LATCHED even after cooldown expires.
        clock = Clock(1000.0)
        ctrl = self._ctrl(cfg=_cfg(cooldown_s=30.0), clock=clock)
        req = _return_request()
        dec0 = ctrl.observe(self._snapshot(), _energy(), action_request=req, now=clock.t)
        self.assertTrue(dec0["start"])                     # generation 1, permitted
        ctrl.run_transaction(self._snapshot())             # terminal -> consumes generation 1
        # During cooldown: blocked.
        dec = ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1010.0)
        self.assertFalse(dec["start"])
        # After cooldown: STILL blocked -- latched on the consumed generation
        # (cooldown expiry alone must not re-trigger).
        dec2 = ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1040.0)
        self.assertFalse(dec2["start"])
        self.assertIn("latched", dec2["reason"])
        st = ctrl.status()
        self.assertTrue(st["trigger_consumed"])
        self.assertEqual(st["consumed_trigger_generation"], 1)


# ── Terminal trigger-generation latch (task section 1) ────────────────────────
class TestTriggerLatch(_Base):
    def _observe_and_run(self, ctrl, snapshot=None, now=1000.0, action_request="default"):
        snap = snapshot if snapshot is not None else self._snapshot()
        if action_request == "default":
            action_request = _return_request()
        dec = ctrl.observe(snap, _energy(), action_request=action_request, now=now)
        self.assertTrue(dec["start"], dec.get("reason"))
        return ctrl.run_transaction(snap)

    def test_persistent_trigger_after_safe_hold_does_not_retrigger(self):
        # Force a SAFE_HOLD (planner fails on the connector gap), then an unchanged
        # still-active forced-return must NOT start a second transaction.
        ctrl = self._ctrl(cfg=_cfg(cooldown_s=0.0))
        far = self._snapshot(lat=56.70, lon=12.95)
        req = _return_request()
        res = self._observe_and_run(ctrl, snapshot=far, action_request=req)
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        for t in (1001.0, 1002.0, 1005.0):
            dec = ctrl.observe(far, _energy(), action_request=req, now=t)
            self.assertFalse(dec["start"])
        self.assertEqual(ctrl.status()["terminal_reason"], rc.SAFE_HOLD)

    def test_successful_return_also_consumes_generation(self):
        ctrl = self._ctrl(cfg=_cfg(cooldown_s=0.0))
        req = _return_request()
        res = self._observe_and_run(ctrl, action_request=req)
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        st = ctrl.status()
        self.assertTrue(st["trigger_consumed"])
        self.assertEqual(st["terminal_reason"], rc.MONITORING_REVISED)
        # An unchanged active trigger does not start a second transaction.
        dec = ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1001.0)
        self.assertFalse(dec["start"])

    def test_clear_and_reapply_injection_creates_new_generation(self):
        clock = Clock(1000.0)
        ctrl = self._ctrl(cfg=_cfg(cooldown_s=0.0), clock=clock)
        req = _return_request()
        self._observe_and_run(ctrl, action_request=req)     # generation 1 consumed
        self.assertFalse(ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1001.0)["start"])
        # Injection cleared -> the authoritative decision policy now reports
        # NONE (no action_request at all) -> falling edge.
        ctrl.observe(self._snapshot(), _energy(decision=energy_policy.DECISION_MONITOR),
                    action_request=None, now=1002.0)
        # Reapplied -> rising edge -> a NEW generation, permitted again.
        dec = ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1003.0)
        self.assertTrue(dec["start"])
        self.assertEqual(ctrl.status()["trigger_generation"], 2)

    def test_reset_permits_new_generation(self):
        ctrl = self._ctrl(cfg=_cfg(cooldown_s=0.0))
        req = _return_request()
        self._observe_and_run(ctrl, action_request=req)     # generation 1 consumed (SAFE_HOLD/success)
        self.assertFalse(ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1001.0)["start"])
        r = ctrl.reset()
        self.assertTrue(r["reset"])
        # A still-active trigger is now permitted again (new generation).
        dec = ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1002.0)
        self.assertTrue(dec["start"])

    def test_new_mission_resets_trigger_context(self):
        clock = Clock(1000.0)
        ctrl = self._ctrl(cfg=_cfg(cooldown_s=0.0), clock=clock)
        req = _return_request()
        self._observe_and_run(ctrl, action_request=req)     # generation 1 consumed
        self.assertFalse(ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1001.0)["start"])
        ctrl.note_new_mission("new original mission started")
        dec = ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1002.0)
        self.assertTrue(dec["start"])                      # a fresh mission -> fresh trigger context

    def test_trigger_generation_assigned_even_under_operator_authority(self):
        # A trigger first seen under OPERATOR authority still owns a stable
        # generation, so the later LOCAL_AGENT handoff of the SAME condition is
        # one generation (runs once), not a fresh unlatched trigger each loop.
        ctrl = self._ctrl(cfg=_cfg(cooldown_s=0.0))
        req = _return_request()
        op = self._snapshot(authority="OPERATOR")
        dec = ctrl.observe(op, _energy(), action_request=req, now=1000.0)
        self.assertFalse(dec["start"])
        self.assertEqual(dec["reason"], rc.BLOCKED_BY_AUTHORITY)
        self.assertEqual(ctrl.status()["trigger_generation"], 1)
        # Same continuous condition, authority now LOCAL_AGENT -> still generation 1.
        dec2 = ctrl.observe(self._snapshot(), _energy(), action_request=req, now=1001.0)
        self.assertTrue(dec2["start"])
        self.assertEqual(ctrl.status()["trigger_generation"], 1)


# ── Battery diagnostics (task section 5) ──────────────────────────────────────
class TestBatteryDiagnostics(_Base):
    def test_valid_battery_surfaced(self):
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(), _energy())
        bd = ctrl.status()["battery_diagnostics"]
        self.assertIsNotNone(bd)
        self.assertEqual(bd["battery_percent"], 12)
        self.assertTrue(bd["battery_valid"])
        self.assertEqual(bd["battery_raw"], 12)
        self.assertIsNotNone(bd["battery_observed_at"])

    def test_unavailable_battery_reported_not_faked(self):
        # ArduPilot battery_remaining == -1 -> unavailable; diagnostics show the
        # raw -1 and battery_valid False, and percent stays None (never faked).
        snap = self._snapshot()
        vs = {
            "usv_id": "usv-2",
            "telemetry": {"lat": 56.6520, "lng": 12.8700, "battery": -1,
                          "mode_name": "AUTO", "armed": True},
            "mavlink": {"heartbeat_age_s": 0.3, "last_message_age_s": 0.2},
            "mission": {"current_mission_id": self.gw.vehicle_mission_id, "mission_active": True,
                        "current_waypoint": 3, "mission_count": 4},
            "agent": {"control_authority": "LOCAL_AGENT",
                      "home_status": {"verified": True, "ready_for_auto": True, "home_position": _HOME}},
        }
        snap = dsm.build_snapshot(vs, "CONNECTED", "LOCAL_AGENT", planning_package=pp.load())
        ctrl = self._ctrl()
        ctrl.observe(snap, _energy())
        bd = ctrl.status()["battery_diagnostics"]
        self.assertEqual(bd["battery_raw"], -1)
        self.assertFalse(bd["battery_valid"])
        self.assertIsNone(bd["battery_percent"])          # unavailable, never faked


# ── Dry run ─────────────────────────────────────────────────────────────────────
class TestDryRun(_Base):
    def test_dry_run_performs_no_vehicle_writes(self):
        self.gw = ExplodingWriteGateway()
        self.gw.pixhawk_route_hash = self.route_hash
        ctrl = self._ctrl(cfg=_cfg(dry_run=True))
        res = ctrl.run_transaction(self._snapshot())
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)  # full simulated lifecycle
        self.assertEqual(self.gw.write_calls, [])                 # no real writes
        st = ctrl.status()
        self.assertTrue(st["simulated"])
        self.assertEqual(st["mode"], "DRY_RUN")


# ── Restart persistence ─────────────────────────────────────────────────────────
class TestPersistence(_Base):
    def test_status_persists_across_instances(self):
        path = os.path.join(self.dir, "replan_status.json")
        store = rc.StatusStore(path=path)
        ctrl = self._ctrl(status_store=store)
        ctrl.run_transaction(self._snapshot())
        store2 = rc.StatusStore(path=path)
        ctrl2 = self._ctrl(status_store=store2)
        self.assertEqual(ctrl2.status()["fsm_state"], rc.MONITORING_REVISED)
        self.assertEqual(ctrl2.status()["revision_number"], 1)

    def test_interrupted_transaction_recovered_as_failed(self):
        path = os.path.join(self.dir, "replan_status.json")
        with open(path, "w") as f:
            json.dump({"state": rc.UPLOAD_REQUESTED, "active_transition_id": "t-x",
                       "revision_number": 4, "last_error": None,
                       "last_terminal_at": None, "last_revision": None}, f)
        store = rc.StatusStore(path=path)
        ctrl = self._ctrl(status_store=store)
        ctrl.recover_after_restart()
        st = ctrl.status()
        self.assertEqual(st["fsm_state"], rc.FAILED)
        self.assertEqual(st["last_error"]["code"], "UNKNOWN_AFTER_RESTART")
        self.assertEqual(st["last_error"]["interrupted_state"], rc.UPLOAD_REQUESTED)


# ── Status / events / obstacle groundwork ───────────────────────────────────────
class TestStatusAndEvents(_Base):
    def test_status_payload_shape(self):
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(), _energy())
        ctrl.run_transaction(self._snapshot())
        st = ctrl.status()
        for key in ("autonomous_execution_enabled", "dry_run", "current_decision",
                    "reason_codes", "snapshot_id", "fsm_state", "active_transition_id",
                    "strategy", "energy", "authority_status", "retry_count", "last_error",
                    "validation_outcome", "upload_outcome", "readback_outcome",
                    "fallback_enabled", "simulated", "obstacle_execution_enabled",
                    "planning_package", "replan_operation_id", "original_mission_id",
                    "original_route_count", "revised_route_count", "original_mission_proof",
                    "revised_mission_proof", "revised_progression"):
            self.assertIn(key, st)

    def test_obstacle_execution_disabled(self):
        ctrl = self._ctrl()
        self.assertFalse(ctrl.status()["obstacle_execution_enabled"])

    def test_event_callback_receives_completion(self):
        events = []
        ctrl = self._ctrl(event_callback=lambda t, m, s: events.append((t, m, s)))
        ctrl.run_transaction(self._snapshot())
        types_seen = [e[0] for e in events]
        self.assertIn("replan_completed", types_seen)


class TestActionRequestTrigger(_Base):
    """decision_policy.ActionRequest is a second, OR-ed input into the SAME
    observe()/run_transaction() entry point the legacy energy_policy signal
    already uses -- one FSM, one trigger-generation latch, no parallel
    controller (E2 water-trial integration task sections 3/4/6/14)."""

    def test_request_return_home_triggers_one_full_return_transaction(self):
        ctrl = self._ctrl()
        snap = self._snapshot()
        req = _action_request(decision_policy.ACTION_REQUEST_RETURN_HOME)
        dec = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=req)
        self.assertTrue(dec["start"], dec.get("reason"))
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        states = [h["to"] for h in ctrl.status()["history"]]
        self.assertEqual(states, [
            rc.HOLD_REQUESTED, rc.HOLD_CONFIRMED, rc.PLANNING, rc.VALIDATING,
            rc.UPLOAD_REQUESTED, rc.VERIFYING_REVISION, rc.RESUME_REQUESTED,
            rc.MONITORING_REVISED,
        ])

    def test_request_hold_confirms_loiter_then_safe_hold_without_planning(self):
        ctrl = self._ctrl()
        snap = self._snapshot()
        req = _action_request(decision_policy.ACTION_REQUEST_HOLD)
        dec = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=req)
        self.assertTrue(dec["start"], dec.get("reason"))
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        states = [h["to"] for h in ctrl.status()["history"]]
        self.assertEqual(states, [rc.HOLD_REQUESTED, rc.HOLD_CONFIRMED, rc.SAFE_HOLD])
        self.assertIn("loiter", self.gw.calls)          # LOITER was confirmed
        self.assertNotIn("upload", self.gw.calls)        # no return route attempted
        self.assertNotIn("auto", self.gw.calls)
        self.assertNotIn("rtl", self.gw.calls)

    def test_status_hold_only_true_for_hold_request_false_for_return_request(self):
        # P0-3: status()["hold_only"] is the signal mission_execution_
        # controller._apply_replan_handoff uses to tell "no replan attempted"
        # (this HOLD-only path) apart from "a replan was attempted and
        # failed" -- both can terminate on the exact same SAFE_HOLD state.
        ctrl = self._ctrl()
        snap = self._snapshot()
        hold_req = _action_request(decision_policy.ACTION_REQUEST_HOLD)
        ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=hold_req)
        ctrl.run_transaction(snap)
        self.assertEqual(ctrl.status()["fsm_state"], rc.SAFE_HOLD)
        self.assertTrue(ctrl.status()["hold_only"])

        ctrl2 = self._ctrl()
        return_req = _action_request(decision_policy.ACTION_REQUEST_RETURN_HOME)
        ctrl2.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=return_req)
        ctrl2.run_transaction(snap)
        self.assertEqual(ctrl2.status()["fsm_state"], rc.MONITORING_REVISED)
        self.assertFalse(ctrl2.status()["hold_only"])

    def test_request_hold_is_unaffected_by_energy_policys_own_decision(self):
        # decision_policy.ActionRequest is the SOLE authoritative trigger (E2
        # water-trial integration task) -- energy_policy.EnergyResult no
        # longer contributes to `want` at all, so a HOLD action request
        # behaves identically regardless of what the legacy energy heuristic
        # independently reports (retained only as evidence/diagnostics).
        ctrl = self._ctrl()
        snap = self._snapshot()
        req = _action_request(decision_policy.ACTION_REQUEST_HOLD)
        dec = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_REPLAN_SAFE_RETURN), action_request=req)
        self.assertTrue(dec["start"])
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        self.assertNotIn("upload", self.gw.calls)

    def test_repeated_observe_with_persistent_return_request_does_not_duplicate(self):
        clock = Clock(1000.0)
        ctrl = self._ctrl(cfg=_cfg(cooldown_s=0.0), clock=clock)
        snap = self._snapshot()
        req = _action_request(decision_policy.ACTION_REQUEST_RETURN_HOME)
        dec = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=req, now=1000.0)
        self.assertTrue(dec["start"])
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)
        # Same persistent action request, next cycle: no duplicate transaction.
        dec2 = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=req, now=1001.0)
        self.assertFalse(dec2["start"])
        self.assertTrue(ctrl.status()["trigger_consumed"])

    def test_repeated_observe_with_persistent_hold_request_does_not_duplicate(self):
        clock = Clock(1000.0)
        ctrl = self._ctrl(cfg=_cfg(cooldown_s=0.0), clock=clock)
        snap = self._snapshot()
        req = _action_request(decision_policy.ACTION_REQUEST_HOLD)
        dec = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=req, now=1000.0)
        self.assertTrue(dec["start"])
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.SAFE_HOLD)
        dec2 = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=req, now=1001.0)
        self.assertFalse(dec2["start"])
        self.assertTrue(ctrl.status()["trigger_consumed"])

    def test_status_exposes_action_request_for_recorder(self):
        ctrl = self._ctrl()
        snap = self._snapshot()
        req = _action_request(decision_policy.ACTION_REQUEST_RETURN_HOME, snapshot_id="snap-99")
        ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR), action_request=req)
        st = ctrl.status()
        self.assertEqual(st["action_request"]["action"], decision_policy.ACTION_REQUEST_RETURN_HOME)
        self.assertEqual(st["action_request"]["source_snapshot_id"], "snap-99")

    def test_action_request_none_never_starts_regardless_of_energy(self):
        # decision_policy.ActionRequest is the SOLE authoritative trigger:
        # omitting it entirely means no autonomous trigger is possible, no
        # matter how strongly energy_policy's own (diagnostic-only) decision
        # would have wanted a return under the old, removed behaviour.
        ctrl = self._ctrl()
        dec = ctrl.observe(self._snapshot(), _energy(decision=energy_policy.DECISION_REPLAN_SAFE_RETURN))
        self.assertFalse(dec["start"])
        self.assertEqual(dec["reason"], "no replan required")

    def test_energy_reason_codes_still_surfaced_as_diagnostics(self):
        # Evidence/debounce/diagnostics role (E2 water-trial integration
        # task): energy_policy's own decision/reason codes/inputs are still
        # recorded on status() for observability even though they no longer
        # drive `want` -- an action_request is what actually starts anything.
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(), _energy(decision=energy_policy.DECISION_REPLAN_SAFE_RETURN))
        st = ctrl.status()
        self.assertEqual(st["current_decision"], energy_policy.DECISION_REPLAN_SAFE_RETURN)
        self.assertIn(energy_policy.CODE_CRITICAL_BATTERY, st["reason_codes"])


# ── Pre-E2 replan lifecycle: stale terminal FAILED must not persist forever ────
class TestStaleFailedResetLifecycle(_Base):
    """Reproduces the exact pre-E2 bug: a replan transaction is triggered
    (correctly, via an authoritative ActionRequest) while mission execution is
    still READY/UNBOUND -- observe() itself has no mission-execution-state
    gate, only an authority/consistency/cooldown/latch gate, so this is
    legitimate to START. It then fails closed at the fresh ORIGINAL-mission
    proof (no bound original mission identity yet -- CRITICAL ISSUE 2), never
    touching the vehicle, and lands in terminal FAILED. Without an explicit
    reset, that terminal FAILED is permanent: it has no time-based expiry and
    no dependency on mission-execution state, so it keeps flooring
    risk_model.py's mission component to HIGH/MISSION_REPLAN_TROUBLE (see
    test_risk_model.py) even after mission execution is rearmed and READY
    again for a brand new, unrelated attempt. reset() (wired by this task into
    mission_execution_controller.rearm() and the NOT_READY->READY readiness
    edge, see test_mission_execution_controller.py) is the sanctioned lifecycle
    step back to MONITORING -- refused mid-transaction, preserving history/
    last_revision for audit."""

    def test_unbound_return_request_fails_closed_without_touching_vehicle(self):
        ctrl = self._ctrl(original_fn=lambda: None)  # UNBOUND: no bound original mission
        snap = self._snapshot()
        decision = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR),
                                action_request=_return_request())
        self.assertTrue(decision["start"], decision.get("reason"))
        ctrl.run_transaction(snap)
        st = ctrl.status()
        self.assertEqual(st["fsm_state"], rc.FAILED)
        self.assertEqual(st["last_error"]["code"], rc.ORIGINAL_MISSION_ID_MISMATCH)
        self.assertEqual(self.gw.write_calls, [])   # no LOITER/upload/AUTO -- proof runs first

    def test_reset_returns_stale_failed_to_monitoring_preserving_audit(self):
        ctrl = self._ctrl(original_fn=lambda: None)
        snap = self._snapshot()
        ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR),
                     action_request=_return_request())
        ctrl.run_transaction(snap)
        self.assertEqual(ctrl.status()["fsm_state"], rc.FAILED)
        history_before = ctrl.status()["history"]
        revision_before = ctrl.status()["last_revision"]
        self.assertTrue(history_before)
        self.assertIsNotNone(revision_before)

        result = ctrl.reset()
        self.assertTrue(result["reset"], result.get("reason"))
        self.assertEqual(result["from"], rc.FAILED)

        st = ctrl.status()
        self.assertEqual(st["fsm_state"], rc.MONITORING)
        self.assertIsNone(st["last_error"])
        # Audit evidence of the stale failure is preserved, not erased.
        self.assertEqual(st["history"], history_before)
        self.assertEqual(st["last_revision"], revision_before)

    def test_reset_refused_while_transaction_running(self):
        ctrl = self._ctrl(original_fn=lambda: None)
        snap = self._snapshot()
        ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR),
                     action_request=_return_request())
        ctrl._action_lock.acquire()   # simulate an in-flight transaction
        try:
            result = ctrl.reset()
            self.assertFalse(result["reset"])
        finally:
            ctrl._action_lock.release()

    def test_healthy_ready_unbound_never_starts_or_fails(self):
        # Item A: mission execution READY + UNBOUND with healthy evidence (no
        # ActionRequest wanting anything) must stay in MONITORING -- UNBOUND
        # alone is never itself a trigger or a failure.
        ctrl = self._ctrl(original_fn=lambda: None)
        snap = self._snapshot()
        decision = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR),
                                action_request=_action_request(decision_policy.ACTION_NONE))
        self.assertFalse(decision["start"])
        st = ctrl.status()
        self.assertEqual(st["fsm_state"], rc.MONITORING)
        self.assertIsNone(st["last_error"])

    def test_reset_then_bound_return_request_completes_successfully(self):
        # After a fresh, real mission Start actually binds the original
        # mission identity, a still-active/re-raised return request must be
        # able to run a full, successful transaction -- reset() must not
        # leave the controller permanently unable to replan.
        binding = {"value": None}
        ctrl = self._ctrl(original_fn=lambda: binding["value"])
        snap = self._snapshot()
        ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR),
                     action_request=_return_request())
        ctrl.run_transaction(snap)
        self.assertEqual(ctrl.status()["fsm_state"], rc.FAILED)

        ctrl.reset()
        self.assertEqual(ctrl.status()["fsm_state"], rc.MONITORING)

        binding["value"] = self.bound   # the (simulated) Start has now bound it
        decision = ctrl.observe(snap, _energy(decision=energy_policy.DECISION_MONITOR),
                                action_request=_return_request())
        self.assertTrue(decision["start"], decision.get("reason"))
        res = ctrl.run_transaction(snap)
        self.assertEqual(res["outcome"], rc.MONITORING_REVISED)


# ── Config validation (fail closed) ─────────────────────────────────────────────
class TestConfigValidation(unittest.TestCase):
    def _base(self, **kw):
        return replan_config.ReplanConfig(**{**replan_config.ReplanConfig().to_dict(), **kw})

    def test_zero_progression_timeout_rejected(self):
        ok, _ = replan_config.validate(self._base(revised_progression_timeout_s=0.0))
        self.assertFalse(ok)

    def test_poll_not_below_timeout_rejected(self):
        ok, _ = replan_config.validate(self._base(revised_progression_timeout_s=1.0,
                                                  progression_poll_interval_s=0.9))
        self.assertFalse(ok)

    def test_nonpositive_position_age_rejected(self):
        ok, _ = replan_config.validate(self._base(max_position_age_s=0.0))
        self.assertFalse(ok)

    def test_zero_hold_settle_timeout_rejected(self):
        ok, _ = replan_config.validate(self._base(replan_hold_settle_timeout_s=0.0))
        self.assertFalse(ok)

    def test_hold_settle_poll_not_below_timeout_rejected(self):
        ok, _ = replan_config.validate(self._base(replan_hold_settle_timeout_s=1.0,
                                                  replan_hold_settle_poll_interval_s=2.0))
        self.assertFalse(ok)

    def test_negative_hold_settle_persistence_rejected(self):
        ok, _ = replan_config.validate(self._base(replan_hold_settle_persistence_s=-1.0))
        self.assertFalse(ok)

    def test_hold_settle_persistence_above_timeout_rejected(self):
        ok, _ = replan_config.validate(self._base(replan_hold_settle_timeout_s=1.0,
                                                  replan_hold_settle_persistence_s=2.0))
        self.assertFalse(ok)

    def test_defaults_valid(self):
        ok, issues = replan_config.validate(replan_config.ReplanConfig())
        self.assertTrue(ok, issues)


if __name__ == "__main__":
    unittest.main(verbosity=2)
