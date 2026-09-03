"""
Unit tests for the continuous risk model (risk_model.py / risk_config.py).

Covers (task sections 24-28):
  * per-component behaviour (energy/communication/navigation/health/mission)
  * aggregation (weights, bounded [0,1] score, dominant component, level
    thresholds, determinism)
  * hard-feasibility override (never averaged away)
  * UNKNOWN-evidence floor (critical missing evidence never reads LOW)
  * injection provenance visibility
  * side-effect freedom (no vehicle-affecting call anywhere in the module)

    python3 test_risk_model.py
"""
import math
import unittest

import risk_config
import risk_model as rm


def _feasible(mission_margin=50.0, rtl_margin=50.0, mission_feasible=True,
             rtl_feasible=True, status="FEASIBLE", **extra):
    d = {
        "status": status,
        "mission_feasible": mission_feasible,
        "rtl_return_feasible": rtl_feasible,
        "mission_margin_percent": mission_margin,
        "rtl_return_margin_percent": rtl_margin,
        "battery_percent": 80.0,
        "battery_source": "PHYSICAL",
        "physical_battery_percent": 80.0,
        "injected_battery_percent": None,
    }
    d.update(extra)
    return d


def _nav_evidence(gps_value=3, gps_state="FRESH", ekf_value=True, ekf_state="FRESH",
                  pos_state="FRESH"):
    return {
        "gps": {"fix_type": {"value": gps_value, "state": gps_state, "age_s": 0.2}},
        "ekf": {"value": ekf_value, "state": ekf_state, "age_s": 0.2},
        "position": {"state": pos_state, "age_s": 0.2},
    }


_ME_STATUS_RUNNING = {"supported": True, "state": "RUNNING",
                      "binding": {"binding_state": "BOUND"}}
_ME_STATUS_NOT_INITIALISED = {"supported": False, "state": None}
_REPLAN_MONITORING = {"fsm_state": "MONITORING"}


class TestEnergyComponent(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def test_large_positive_margins_are_low_risk(self):
        r = rm.evaluate_energy(_feasible(mission_margin=60.0, rtl_margin=60.0), self.cfg)
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.reason, rm.REASON_ENERGY_MARGIN_COMFORTABLE)

    def test_margin_approaching_reserve_boundary_is_elevated(self):
        r = rm.evaluate_energy(_feasible(mission_margin=15.0, rtl_margin=25.0), self.cfg)
        self.assertGreater(r.score, 0.0)
        self.assertLess(r.score, 1.0)
        # worst margin (15) governs, not the more comfortable RTL margin.
        self.assertEqual(r.evidence["worst_margin_percent"], 15.0)

    def test_mission_margin_negative_is_hard_constraint(self):
        r = rm.evaluate_energy(_feasible(mission_margin=-5.0, mission_feasible=False), self.cfg)
        self.assertEqual(r.score, 1.0)
        self.assertEqual(r.reason, rm.REASON_ENERGY_HARD_INFEASIBLE)

    def test_rtl_margin_negative_is_hard_constraint(self):
        r = rm.evaluate_energy(_feasible(rtl_margin=-1.0, rtl_feasible=False), self.cfg)
        self.assertEqual(r.score, 1.0)
        self.assertEqual(r.reason, rm.REASON_ENERGY_HARD_INFEASIBLE)

    def test_feasibility_fully_unknown_is_not_low(self):
        r = rm.evaluate_energy(_feasible(mission_feasible=None, rtl_feasible=None,
                                         mission_margin=None, rtl_margin=None,
                                         status="UNKNOWN"), self.cfg)
        self.assertIsNone(r.score)
        self.assertEqual(r.reason, rm.REASON_ENERGY_EVIDENCE_UNAVAILABLE)

    def test_one_dimension_unknown_still_uses_the_other(self):
        # No mission uploaded yet (mission_feasible None), but Home is verified
        # and RTL margin is known -- pre-Start idle state must not read as
        # fully UNKNOWN energy risk.
        r = rm.evaluate_energy(_feasible(mission_feasible=None, mission_margin=None,
                                         rtl_feasible=True, rtl_margin=45.0), self.cfg)
        self.assertIsNotNone(r.score)
        self.assertEqual(r.evidence["worst_margin_percent"], 45.0)


class TestCommunicationComponent(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def test_connected_is_low(self):
        r = rm.evaluate_communication("CONNECTED", "LOCAL_AGENT", "RUNNING", self.cfg)
        self.assertEqual(r.score, 0.0)

    def test_partitioned_is_elevated_or_high(self):
        r = rm.evaluate_communication("PARTITIONED", "LOCAL_AGENT", "RUNNING", self.cfg)
        self.assertGreaterEqual(r.score, 0.25)

    def test_disconnected_is_high_or_critical(self):
        r = rm.evaluate_communication("DISCONNECTED", "OPERATOR", "NOT_READY", self.cfg)
        self.assertGreaterEqual(r.score, 0.5)

    def test_disconnected_with_proven_local_autonomy_is_lower_than_without(self):
        healthy = rm.evaluate_communication("DISCONNECTED", "LOCAL_AGENT", "RUNNING", self.cfg)
        unhealthy = rm.evaluate_communication("DISCONNECTED", "OPERATOR", "NOT_READY", self.cfg)
        self.assertLess(healthy.score, unhealthy.score)
        self.assertTrue(healthy.evidence["autonomous_continuation_proven"])
        self.assertFalse(unhealthy.evidence["autonomous_continuation_proven"])

    def test_missing_comm_state_is_unknown_not_connected(self):
        r = rm.evaluate_communication(None, "LOCAL_AGENT", "RUNNING", self.cfg)
        self.assertIsNone(r.score)


class TestNavigationComponent(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def test_fresh_3d_fix_healthy_ekf_is_low(self):
        r = rm.evaluate_navigation(_nav_evidence(), self.cfg)
        self.assertEqual(r.score, 0.0)

    def test_explicit_gps_degradation_raises_risk_immediately(self):
        r = rm.evaluate_navigation(_nav_evidence(gps_value=2), self.cfg)
        self.assertGreater(r.score, 0.0)
        self.assertEqual(r.reason, rm.REASON_NAVIGATION_GPS_DEGRADED)

    def test_ekf_unhealthy_is_high(self):
        r = rm.evaluate_navigation(_nav_evidence(ekf_value=False), self.cfg)
        self.assertGreaterEqual(r.score, 0.75)
        self.assertEqual(r.reason, rm.REASON_NAVIGATION_EKF_UNHEALTHY)

    def test_stale_position_is_high(self):
        r = rm.evaluate_navigation(_nav_evidence(pos_state="STALE"), self.cfg)
        self.assertGreaterEqual(r.score, 0.75)
        self.assertEqual(r.reason, rm.REASON_NAVIGATION_POSITION_STALE)

    def test_never_observed_evidence_is_unknown(self):
        r = rm.evaluate_navigation({}, self.cfg)
        self.assertIsNone(r.score)
        self.assertEqual(r.reason, rm.REASON_NAVIGATION_EVIDENCE_UNAVAILABLE)

    def test_worst_signal_governs_not_averaged(self):
        # 3D fix + healthy EKF (both 0.0) but STALE position (1.0) -- the
        # aggregate navigation score must reflect the worst signal, not an
        # average that would dilute it toward ~0.33.
        r = rm.evaluate_navigation(_nav_evidence(pos_state="STALE"), self.cfg)
        self.assertEqual(r.score, 1.0)


class TestHealthComponent(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def test_nominal_is_low(self):
        r = rm.evaluate_health({"status": "OK"}, {"imu_health": "OK"}, self.cfg)
        self.assertEqual(r.score, 0.0)

    def test_failsafe_active_is_critical(self):
        r = rm.evaluate_health({"status": "ACTIVE"}, {"imu_health": "OK"}, self.cfg)
        self.assertEqual(r.score, 1.0)

    def test_missing_evidence_is_unknown(self):
        r = rm.evaluate_health({}, {}, self.cfg)
        self.assertIsNone(r.score)


class TestMissionComponent(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def test_running_is_nominal(self):
        r = rm.evaluate_mission(_ME_STATUS_RUNNING, _REPLAN_MONITORING, self.cfg)
        self.assertEqual(r.score, 0.0)

    def test_completed_hold_after_safe_return_is_not_worse_than_running(self):
        # task section 13: a deliberate protective state (LOITER / hold after
        # arrival) must not read as extra risk.
        completed = {"supported": True, "state": "COMPLETED_HOLD", "binding": {}}
        r = rm.evaluate_mission(completed, _REPLAN_MONITORING, self.cfg)
        self.assertEqual(r.score, 0.0)

    def test_suspended_mission_execution_is_elevated(self):
        suspended = {"supported": True, "state": "SUSPENDED", "binding": {}}
        r = rm.evaluate_mission(suspended, _REPLAN_MONITORING, self.cfg)
        self.assertGreater(r.score, 0.0)
        self.assertEqual(r.reason, rm.REASON_MISSION_EXECUTION_TROUBLE)

    def test_replan_safe_hold_is_elevated(self):
        r = rm.evaluate_mission(_ME_STATUS_RUNNING, {"fsm_state": "SAFE_HOLD"}, self.cfg)
        self.assertGreater(r.score, 0.0)
        self.assertEqual(r.reason, rm.REASON_MISSION_REPLAN_TROUBLE)

    def test_controller_not_initialised_is_unknown(self):
        r = rm.evaluate_mission(_ME_STATUS_NOT_INITIALISED, {}, self.cfg)
        self.assertIsNone(r.score)

    def test_ready_unbound_with_monitoring_replan_is_nominal(self):
        # Pre-E2 lifecycle (task): READY + UNBOUND is the LEGITIMATE pre-Start
        # state -- binding_state only ever contributes STALE_MISMATCH's milder
        # score (a different value entirely), never a mission-trouble floor by
        # itself.
        ready_unbound = {"supported": True, "state": "READY",
                         "binding": {"binding_state": "UNBOUND"}}
        r = rm.evaluate_mission(ready_unbound, _REPLAN_MONITORING, self.cfg)
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.reason, rm.REASON_MISSION_NOMINAL)

    def test_ready_unbound_with_stale_failed_replan_is_still_elevated(self):
        # A terminal FAILED replan FSM state floors mission trouble purely
        # from fsm_state -- READY/UNBOUND mission execution does not mask a
        # genuinely-terminal replan failure. This is the floor the lifecycle
        # reset (replan_controller.reset(), wired into rearm()/the fresh
        # readiness edge) must clear for a HISTORICAL failure -- the floor
        # logic itself must not weaken.
        ready_unbound = {"supported": True, "state": "READY",
                         "binding": {"binding_state": "UNBOUND"}}
        r = rm.evaluate_mission(ready_unbound, {"fsm_state": "FAILED"}, self.cfg)
        self.assertGreater(r.score, 0.0)
        self.assertEqual(r.reason, rm.REASON_MISSION_REPLAN_TROUBLE)


class TestAggregation(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def _nominal_kwargs(self, **overrides):
        kwargs = dict(
            feasibility=_feasible(),
            comm_state="CONNECTED",
            control_authority="LOCAL_AGENT",
            mission_execution_status=_ME_STATUS_RUNNING,
            replan_status=_REPLAN_MONITORING,
            navigation_evidence=_nav_evidence(),
            failsafe={"status": "OK"},
            imu={"imu_health": "OK"},
            cfg=self.cfg,
        )
        kwargs.update(overrides)
        return kwargs

    def test_weights_sum_to_one_by_default(self):
        total = (self.cfg.energy_weight + self.cfg.communication_weight
                 + self.cfg.navigation_weight + self.cfg.health_weight
                 + self.cfg.mission_weight)
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_config_validate_accepts_defaults(self):
        ok, issues = risk_config.validate(risk_config.DEFAULT)
        self.assertTrue(ok, issues)

    def test_score_always_bounded_and_not_nan(self):
        for comm in ("CONNECTED", "PARTITIONED", "DISCONNECTED", None):
            r = rm.evaluate_risk(**self._nominal_kwargs(comm_state=comm))
            if r.score is not None:
                self.assertGreaterEqual(r.score, 0.0)
                self.assertLessEqual(r.score, 1.0)
                self.assertFalse(math.isnan(r.score))

    def test_nominal_case_is_low_and_continue(self):
        r = rm.evaluate_risk(**self._nominal_kwargs())
        self.assertEqual(r.level, rm.LEVEL_LOW)
        self.assertEqual(r.recommendation, rm.RECOMMEND_CONTINUE)
        self.assertFalse(r.hard_constraint_violated)
        self.assertEqual(r.confidence, rm.CONFIDENCE_HIGH)

    def test_dominant_component_is_the_worst_offender(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(comm_state="PARTITIONED"))
        self.assertEqual(r.dominant_component, "communication")

    def test_level_thresholds_are_deterministic_at_boundaries(self):
        self.assertEqual(rm._level_for_score(0.0, self.cfg), rm.LEVEL_LOW)
        self.assertEqual(rm._level_for_score(0.24, self.cfg), rm.LEVEL_LOW)
        self.assertEqual(rm._level_for_score(0.25, self.cfg), rm.LEVEL_ELEVATED)
        self.assertEqual(rm._level_for_score(0.49, self.cfg), rm.LEVEL_ELEVATED)
        self.assertEqual(rm._level_for_score(0.50, self.cfg), rm.LEVEL_HIGH)
        self.assertEqual(rm._level_for_score(0.74, self.cfg), rm.LEVEL_HIGH)
        self.assertEqual(rm._level_for_score(0.75, self.cfg), rm.LEVEL_CRITICAL)
        self.assertEqual(rm._level_for_score(1.0, self.cfg), rm.LEVEL_CRITICAL)

    def test_repeated_evaluation_is_deterministic(self):
        r1 = rm.evaluate_risk(**self._nominal_kwargs(now=1000.0))
        r2 = rm.evaluate_risk(**self._nominal_kwargs(now=1000.0))
        d1, d2 = r1.to_dict(), r2.to_dict()
        del d1["evaluated_at"], d2["evaluated_at"]
        self.assertEqual(d1, d2)

    # ── Hard feasibility dominance (acceptance criterion 4) ─────────────────
    def test_infeasible_with_otherwise_low_components_is_critical(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=-2.0, mission_feasible=False)))
        self.assertEqual(r.level, rm.LEVEL_CRITICAL)
        self.assertEqual(r.score, 1.0)
        self.assertTrue(r.hard_constraint_violated)

    def test_weighted_risk_cannot_override_hard_violation(self):
        # Every OTHER component is perfect (0.0); only energy is infeasible.
        # A naive weighted mean would land around 0.3 (ELEVATED) -- the
        # aggregate must still read CRITICAL.
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=-2.0, mission_feasible=False)))
        self.assertNotEqual(r.level, rm.LEVEL_ELEVATED)
        self.assertEqual(r.level, rm.LEVEL_CRITICAL)

    def test_mission_infeasible_rtl_feasible_recommends_return(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=-2.0, mission_feasible=False,
                                  rtl_margin=40.0, rtl_feasible=True)))
        self.assertEqual(r.recommendation, rm.RECOMMEND_RETURN)

    def test_mission_and_rtl_infeasible_recommends_hold(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=-2.0, mission_feasible=False,
                                  rtl_margin=-1.0, rtl_feasible=False)))
        self.assertEqual(r.recommendation, rm.RECOMMEND_HOLD)

    # ── UNKNOWN evidence floor (acceptance criterion 5) ──────────────────────
    def test_major_evidence_missing_is_not_falsely_low(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_feasible=None, rtl_feasible=None,
                                  mission_margin=None, rtl_margin=None, status="UNKNOWN")))
        self.assertNotEqual(r.level, rm.LEVEL_LOW)
        self.assertNotEqual(r.level, rm.LEVEL_ELEVATED)
        self.assertEqual(r.level, rm.LEVEL_UNKNOWN)
        self.assertNotEqual(r.recommendation, rm.RECOMMEND_CONTINUE)

    def test_battery_and_navigation_unavailable_together_is_not_low(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_feasible=None, rtl_feasible=None,
                                  mission_margin=None, rtl_margin=None, status="UNKNOWN"),
            navigation_evidence={}, comm_state="CONNECTED"))
        self.assertNotEqual(r.level, rm.LEVEL_LOW)

    def test_missing_evidence_never_downgrades_a_genuine_high_or_critical(self):
        # Navigation unavailable, but communication (DISCONNECTED, no proven
        # autonomy) AND a near-critical energy margin already independently
        # drive the AVAILABLE-evidence score into HIGH/CRITICAL territory --
        # missing evidence must not soften that back down to UNKNOWN.
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=3.0, rtl_margin=3.0),
            navigation_evidence={}, comm_state="DISCONNECTED",
            control_authority="OPERATOR", mission_execution_status=_ME_STATUS_NOT_INITIALISED))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))


class TestSeverityFloorsAndGoverningLevel(unittest.TestCase):
    """Aggregate-semantics correction task: a serious single-component hazard
    must never be averaged down to a reassuring overall LEVEL, even though
    its weighted contribution alone is small. Every case here goes through
    the FULL evaluate_risk() aggregate (task sections 9-13), not only the
    component function -- that is exactly where the original bug lived."""

    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def _nominal_kwargs(self, **overrides):
        kwargs = dict(
            feasibility=_feasible(),
            comm_state="CONNECTED",
            control_authority="LOCAL_AGENT",
            mission_execution_status=_ME_STATUS_RUNNING,
            replan_status=_REPLAN_MONITORING,
            navigation_evidence=_nav_evidence(),
            failsafe={"status": "OK"},
            imu={"imu_health": "OK"},
            cfg=self.cfg,
        )
        kwargs.update(overrides)
        return kwargs

    # ── The exact bug reported in the task background ────────────────────────
    def test_root_problem_disconnected_no_autonomy_is_no_longer_low(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            comm_state="DISCONNECTED", control_authority="OPERATOR",
            mission_execution_status={"supported": True, "state": "NOT_READY", "binding": {}}))
        # The old bug: weighted mean 0.30*0 + 0.25*0.95 + 0.25*0 + 0.10*0 + 0.10*0
        # = 0.2375 -> LOW. The weighted SCORE is legitimately still 0.2375 --
        # only the governing LEVEL must change.
        self.assertAlmostEqual(r.weighted_score, 0.2375)
        self.assertEqual(r.weighted_level, rm.LEVEL_LOW)
        self.assertEqual(r.component_floor_level, rm.LEVEL_HIGH)
        self.assertEqual(r.component_floor_reason, rm.REASON_COMMUNICATION_DISCONNECTED_NO_AUTONOMY)
        self.assertEqual(r.component_floor_source, "communication")
        self.assertEqual(r.score, 0.2375)
        self.assertNotEqual(r.level, rm.LEVEL_LOW)
        self.assertEqual(r.level, rm.LEVEL_HIGH)

    # ── 9. Communication cases ────────────────────────────────────────────────
    def test_A_connected_all_nominal_is_low(self):
        r = rm.evaluate_risk(**self._nominal_kwargs())
        self.assertEqual(r.level, rm.LEVEL_LOW)

    def test_B_partitioned_all_else_nominal_is_at_least_elevated(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(comm_state="PARTITIONED"))
        self.assertIn(r.level, (rm.LEVEL_ELEVATED, rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))
        self.assertNotEqual(r.level, rm.LEVEL_LOW)

    def test_C_disconnected_local_agent_running_is_at_least_high(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            comm_state="DISCONNECTED", control_authority="LOCAL_AGENT",
            mission_execution_status=_ME_STATUS_RUNNING))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    def test_D_disconnected_operator_not_ready_must_never_be_low(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            comm_state="DISCONNECTED", control_authority="OPERATOR",
            mission_execution_status={"supported": True, "state": "NOT_READY", "binding": {}}))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))
        self.assertNotEqual(r.level, rm.LEVEL_LOW)

    # ── 10. Navigation cases ───────────────────────────────────────────────────
    def test_E_gps_2d_fix_rest_nominal_is_at_least_elevated(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(navigation_evidence=_nav_evidence(gps_value=2)))
        self.assertNotEqual(r.level, rm.LEVEL_LOW)
        self.assertIn(r.level, (rm.LEVEL_ELEVATED, rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    def test_F_gps_no_fix_rest_nominal_is_at_least_high(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(navigation_evidence=_nav_evidence(gps_value=0)))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    def test_G_ekf_unhealthy_rest_nominal_is_at_least_high(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(navigation_evidence=_nav_evidence(ekf_value=False)))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    def test_H_position_stale_rest_nominal_is_at_least_high(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(navigation_evidence=_nav_evidence(pos_state="STALE")))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    # ── 11. Health cases ─────────────────────────────────────────────────────
    def test_I_failsafe_active_everything_else_nominal_is_critical(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(failsafe={"status": "ACTIVE"}))
        self.assertEqual(r.level, rm.LEVEL_CRITICAL)
        self.assertFalse(r.hard_constraint_violated)  # a floor, not the hard-feasibility override
        self.assertEqual(r.recommendation, rm.RECOMMEND_HOLD)

    def test_J_imu_warning_and_stale_floors(self):
        warning = rm.evaluate_risk(**self._nominal_kwargs(imu={"imu_health": "WARNING"}))
        self.assertEqual(warning.component_floor_level, rm.LEVEL_ELEVATED)
        self.assertNotEqual(warning.level, rm.LEVEL_LOW)
        stale = rm.evaluate_risk(**self._nominal_kwargs(imu={"imu_health": "STALE"}))
        self.assertEqual(stale.component_floor_level, rm.LEVEL_HIGH)
        self.assertIn(stale.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    # ── 12. Mission / replan cases ───────────────────────────────────────────
    def test_K_mission_suspended_is_at_least_high(self):
        suspended = {"supported": True, "state": "SUSPENDED", "binding": {}}
        r = rm.evaluate_risk(**self._nominal_kwargs(mission_execution_status=suspended))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    def test_L_replan_safe_hold_is_at_least_high(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(replan_status={"fsm_state": "SAFE_HOLD"}))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    def test_M_stale_mismatch_binding_is_at_least_elevated(self):
        bound = {"supported": True, "state": "RUNNING", "binding": {"binding_state": "STALE_MISMATCH"}}
        r = rm.evaluate_risk(**self._nominal_kwargs(mission_execution_status=bound))
        self.assertNotEqual(r.level, rm.LEVEL_LOW)
        self.assertIn(r.level, (rm.LEVEL_ELEVATED, rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    # ── Pre-E2 replan lifecycle (task) ────────────────────────────────────────
    def test_N_ready_unbound_healthy_is_low_not_hold(self):
        # Item A: mission execution READY + UNBOUND with otherwise-nominal
        # evidence and no active replan trouble must read as LOW/CONTINUE --
        # UNBOUND before Start is legitimate and must never by itself produce
        # a HIGH floor or a HOLD recommendation.
        ready_unbound = {"supported": True, "state": "READY",
                         "binding": {"binding_state": "UNBOUND"}}
        r = rm.evaluate_risk(**self._nominal_kwargs(mission_execution_status=ready_unbound))
        self.assertIsNone(r.component_floor_level)
        self.assertEqual(r.level, rm.LEVEL_LOW)
        self.assertEqual(r.recommendation, rm.RECOMMEND_CONTINUE)

    def test_O_ready_unbound_with_stale_failed_replan_is_still_high(self):
        # Item C: a terminal FAILED replan transaction still floors HIGH even
        # while mission execution is READY/UNBOUND for a fresh attempt -- the
        # floor must not be weakened. Clearing this for a HISTORICAL failure
        # is the job of the lifecycle reset (rearm()/fresh-readiness-edge ->
        # replan_controller.reset()), not of loosening this floor.
        ready_unbound = {"supported": True, "state": "READY",
                         "binding": {"binding_state": "UNBOUND"}}
        r = rm.evaluate_risk(**self._nominal_kwargs(
            mission_execution_status=ready_unbound,
            replan_status={"fsm_state": "FAILED"}))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))
        self.assertEqual(r.component_floor_reason, rm.REASON_MISSION_REPLAN_TROUBLE)
        self.assertEqual(r.recommendation, rm.RECOMMEND_HOLD)

    def test_P_ready_unbound_after_replan_reset_to_monitoring_is_low_again(self):
        # Same READY/UNBOUND mission-execution evidence as test_O, but with
        # the replan FSM returned to MONITORING (the outcome of the sanctioned
        # reset/rearm lifecycle) -- the stale failure must no longer
        # contaminate risk for the new attempt.
        ready_unbound = {"supported": True, "state": "READY",
                         "binding": {"binding_state": "UNBOUND"}}
        r = rm.evaluate_risk(**self._nominal_kwargs(
            mission_execution_status=ready_unbound,
            replan_status={"fsm_state": "MONITORING"}))
        self.assertIsNone(r.component_floor_level)
        self.assertEqual(r.level, rm.LEVEL_LOW)
        self.assertEqual(r.recommendation, rm.RECOMMEND_CONTINUE)

    # ── EXACT live startup proof: NOT_READY + UNBOUND, authority OPERATOR ─────
    def test_Q_not_ready_authority_blocked_with_stale_failed_replan_is_still_high(self):
        # The live bug's reproduction: mission execution reports NOT_READY
        # (authority is OPERATOR -- the pre-Start handoff is pending, a
        # legitimate state), evidence otherwise proven/UNBOUND, and a
        # persisted terminal FAILED replan restored at startup. The floor
        # still applies purely from replan_fsm_state -- state=NOT_READY does
        # not mask a genuinely terminal replan failure any more than
        # state=READY does (test_O).
        not_ready_unbound = {"supported": True, "state": "NOT_READY",
                             "binding": {"binding_state": "UNBOUND"}}
        r = rm.evaluate_risk(**self._nominal_kwargs(
            control_authority="OPERATOR",
            mission_execution_status=not_ready_unbound,
            replan_status={"fsm_state": "FAILED"}))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))
        self.assertEqual(r.component_floor_reason, rm.REASON_MISSION_REPLAN_TROUBLE)
        self.assertEqual(r.recommendation, rm.RECOMMEND_HOLD)

    def test_R_not_ready_authority_blocked_after_replan_reset_is_low_again(self):
        # Same NOT_READY/UNBOUND/OPERATOR-authority evidence as test_Q, but
        # with the replan FSM returned to MONITORING -- the outcome of the
        # mission-execution-controller reset this task adds for the fresh-
        # evidence/authority-pending edge (mission_execution_controller.
        # _maybe_mark_fresh_evidence_reset_locked). The stale historical
        # failure must no longer contaminate risk, and mission execution
        # legitimately stays NOT_READY (authority is still OPERATOR).
        not_ready_unbound = {"supported": True, "state": "NOT_READY",
                             "binding": {"binding_state": "UNBOUND"}}
        r = rm.evaluate_risk(**self._nominal_kwargs(
            control_authority="OPERATOR",
            mission_execution_status=not_ready_unbound,
            replan_status={"fsm_state": "MONITORING"}))
        self.assertIsNone(r.component_floor_level)
        self.assertEqual(r.level, rm.LEVEL_LOW)
        self.assertEqual(r.recommendation, rm.RECOMMEND_CONTINUE)

    def test_mitigation_states_are_not_inflated(self):
        # PAUSED / RETURNING_HOME / COMPLETED_HOLD / replan FALLBACK_RTL are
        # deliberate protective states, not extra risk -- they must not pick
        # up a severity floor merely for being "not RUNNING".
        for state in ("PAUSED", "RETURNING_HOME", "COMPLETED_HOLD"):
            status = {"supported": True, "state": state, "binding": {"binding_state": "BOUND"}}
            r = rm.evaluate_risk(**self._nominal_kwargs(mission_execution_status=status))
            self.assertIsNone(r.component_floor_level, f"{state} must not floor severity")
            self.assertEqual(r.level, rm.LEVEL_LOW)

        r = rm.evaluate_risk(**self._nominal_kwargs(replan_status={"fsm_state": "FALLBACK_RTL"}))
        self.assertIsNone(r.component_floor_level)
        self.assertNotEqual(r.level, rm.LEVEL_CRITICAL)

    # ── 13. Multi-risk combination (thesis experiment E5) ────────────────────
    def test_weighted_score_demonstrates_combination(self):
        energy_only = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=15.0, rtl_margin=25.0)))
        comm_only = rm.evaluate_risk(**self._nominal_kwargs(comm_state="PARTITIONED"))
        combined = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=15.0, rtl_margin=25.0), comm_state="PARTITIONED"))
        self.assertGreater(combined.weighted_score, energy_only.weighted_score)
        self.assertGreater(combined.weighted_score, comm_only.weighted_score)

    def test_tightening_energy_plus_disconnected_increases_weighted_score(self):
        moderate = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=20.0, rtl_margin=20.0),
            comm_state="DISCONNECTED", control_authority="OPERATOR",
            mission_execution_status={"supported": True, "state": "NOT_READY", "binding": {}}))
        tighter = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=8.0, rtl_margin=10.0),
            comm_state="DISCONNECTED", control_authority="OPERATOR",
            mission_execution_status={"supported": True, "state": "NOT_READY", "binding": {}}))
        self.assertGreater(tighter.weighted_score, moderate.weighted_score)
        # The governing level is already HIGH from the communication floor in
        # BOTH cases -- that is expected and fine (task section 13).
        self.assertEqual(moderate.level, rm.LEVEL_HIGH)
        self.assertEqual(tighter.level, rm.LEVEL_HIGH)

    # ── Hard feasibility still dominates every floor (acceptance criterion 8) ─
    def test_hard_violation_outranks_every_floor(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=-2.0, mission_feasible=False),
            comm_state="DISCONNECTED", control_authority="OPERATOR",
            failsafe={"status": "ACTIVE"}))
        self.assertEqual(r.level, rm.LEVEL_CRITICAL)
        self.assertEqual(r.score, 1.0)
        self.assertTrue(r.hard_constraint_violated)
        self.assertEqual(r.hard_override_level, rm.LEVEL_CRITICAL)

    # ── Dominant component nominal semantics (acceptance criterion 11) ───────
    def test_all_zero_components_have_no_dominant_component(self):
        r = rm.evaluate_risk(**self._nominal_kwargs())
        self.assertIsNone(r.dominant_component)
        self.assertEqual(r.dominant_reason, rm.REASON_NOMINAL_AGGREGATE)

    def test_nonzero_case_still_names_a_dominant_component(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(comm_state="PARTITIONED"))
        self.assertEqual(r.dominant_component, "communication")
        self.assertEqual(r.dominant_reason, rm.REASON_COMMUNICATION_PARTITIONED)

    # ── UNKNOWN ordering must not hide a known HIGH/CRITICAL (acceptance 10) ──
    def test_unknown_evidence_does_not_hide_a_known_high_floor(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            navigation_evidence={}, comm_state="DISCONNECTED", control_authority="OPERATOR",
            mission_execution_status={"supported": True, "state": "NOT_READY", "binding": {}}))
        self.assertEqual(r.level, rm.LEVEL_HIGH)
        self.assertNotEqual(r.level, rm.LEVEL_UNKNOWN)

    def test_unknown_still_wins_when_nothing_known_is_bad(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_feasible=None, rtl_feasible=None,
                                  mission_margin=None, rtl_margin=None, status="UNKNOWN")))
        self.assertEqual(r.level, rm.LEVEL_UNKNOWN)
        self.assertIsNone(r.component_floor_level)


class TestEnergySeverityFloor(unittest.TestCase):
    """Final calibration task: F_energy, the non-compensatory minimum
    severity the worst-of-(mission, RTL) margin demands as it approaches the
    hard-feasibility boundary from the positive side. Every case goes
    through the FULL evaluate_risk() aggregate (task section 6), not only
    evaluate_energy() -- the floor is only meaningful once it is competing
    against (and winning over) four otherwise-nominal components' dilution."""

    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def _nominal_kwargs(self, **overrides):
        kwargs = dict(
            feasibility=_feasible(),
            comm_state="CONNECTED",
            control_authority="LOCAL_AGENT",
            mission_execution_status=_ME_STATUS_RUNNING,
            replan_status=_REPLAN_MONITORING,
            navigation_evidence=_nav_evidence(),
            failsafe={"status": "OK"},
            imu={"imu_health": "OK"},
            cfg=self.cfg,
        )
        kwargs.update(overrides)
        return kwargs

    def _at_margin(self, worst_margin, other_margin=60.0, feasible=None):
        """Evaluate the full aggregate with worst_margin as the governing
        (more restrictive) margin and other_margin comfortably clear of it.
        `feasible` mirrors mission_feasibility.py's own real coupling
        (margin > 0 -> feasible True, margin <= 0 -> feasible False) unless
        explicitly overridden."""
        if feasible is None:
            feasible = worst_margin > 0
        return rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=worst_margin, mission_feasible=feasible,
                                  rtl_margin=other_margin, rtl_feasible=True)))

    # A/B -- comfortably clear of the floor thresholds -> no floor at all.
    def test_A_margin_30_no_energy_floor(self):
        r = self._at_margin(30.0)
        self.assertIsNone(r.component_floor_level)
        self.assertEqual(r.level, rm.LEVEL_LOW)

    def test_B_margin_20_no_energy_floor(self):
        r = self._at_margin(20.0)
        self.assertIsNone(r.component_floor_level)
        self.assertEqual(r.level, rm.LEVEL_LOW)

    # C -- just inside the ELEVATED band.
    def test_C_margin_14_is_at_least_elevated(self):
        r = self._at_margin(14.0)
        self.assertEqual(r.component_floor_level, rm.LEVEL_ELEVATED)
        self.assertEqual(r.component_floor_source, "energy")
        self.assertEqual(r.component_floor_reason, rm.REASON_ENERGY_MARGIN_TIGHT)
        self.assertIn(r.level, (rm.LEVEL_ELEVATED, rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))
        self.assertNotEqual(r.level, rm.LEVEL_LOW)

    # D -- the 5% boundary itself: defined to be inside the ELEVATED band
    # (energy_high_margin_percent <= m < energy_elevated_margin_percent).
    def test_D_margin_5_boundary_is_elevated_not_high(self):
        r = self._at_margin(5.0)
        self.assertEqual(r.component_floor_level, rm.LEVEL_ELEVATED)
        self.assertEqual(r.component_floor_reason, rm.REASON_ENERGY_MARGIN_TIGHT)
        self.assertEqual(r.level, rm.LEVEL_ELEVATED)

    # E -- just inside the HIGH band.
    def test_E_margin_4_is_at_least_high(self):
        r = self._at_margin(4.0)
        self.assertEqual(r.component_floor_level, rm.LEVEL_HIGH)
        self.assertEqual(r.component_floor_reason, rm.REASON_ENERGY_MARGIN_NEAR_INFEASIBLE)
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))
        # Three-layer semantic model task: a severity-floor-driven HIGH/
        # CRITICAL does NOT universally imply HOLD -- an energy-governed
        # case with a proven-feasible RTL return recommends RETURN_HOME.
        self.assertEqual(r.dominant_component, "energy")
        self.assertEqual(r.recommendation, rm.RECOMMEND_RETURN)

    # F -- a bare positive margin must not remain merely LOW/ELEVATED
    # (acceptance criterion 1).
    def test_F_margin_0_1_is_high_not_merely_elevated(self):
        r = self._at_margin(0.1)
        self.assertEqual(r.component_floor_level, rm.LEVEL_HIGH)
        self.assertEqual(r.level, rm.LEVEL_HIGH)
        self.assertNotEqual(r.level, rm.LEVEL_ELEVATED)
        self.assertNotEqual(r.level, rm.LEVEL_LOW)

    # G -- exactly zero retains the EXISTING hard-feasibility semantics
    # (mission_feasibility.py: margin <= 0 is infeasible), never a new
    # margin-based CRITICAL rule of this floor's own.
    def test_G_margin_0_is_existing_hard_infeasibility_critical(self):
        r = self._at_margin(0.0, feasible=False)
        self.assertTrue(r.hard_constraint_violated)
        self.assertEqual(r.level, rm.LEVEL_CRITICAL)
        self.assertEqual(r.hard_override_level, rm.LEVEL_CRITICAL)
        # The hard override subsumes the floor -- no energy floor is reported
        # here, exactly as for every other pre-existing hard-violation case.
        self.assertIsNone(r.component_floor_level)

    # H -- negative margin, same hard-violation semantics.
    def test_H_margin_negative_is_critical_hard_violation(self):
        r = self._at_margin(-3.0, feasible=False)
        self.assertTrue(r.hard_constraint_violated)
        self.assertEqual(r.level, rm.LEVEL_CRITICAL)
        self.assertEqual(r.score, 1.0)

    # I -- RTL is the limiting margin (mission comfortable, RTL tight).
    def test_I_rtl_margin_is_limiting_is_high(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=30.0, mission_feasible=True,
                                  rtl_margin=3.0, rtl_feasible=True)))
        self.assertEqual(r.component_floor_level, rm.LEVEL_HIGH)
        self.assertEqual(r.level, rm.LEVEL_HIGH)

    # J -- mission is the limiting margin (RTL comfortable, mission tight).
    def test_J_mission_margin_is_limiting_is_high(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=3.0, mission_feasible=True,
                                  rtl_margin=50.0, rtl_feasible=True)))
        self.assertEqual(r.component_floor_level, rm.LEVEL_HIGH)
        self.assertEqual(r.level, rm.LEVEL_HIGH)

    # ── E2b: deterministic injected-margin progression (thesis experiment) ──
    def test_E2b_progressive_margin_sequence_is_monotonically_more_conservative(self):
        sequence = [25.0, 14.0, 4.0, -1.0]
        results = []
        for margin in sequence:
            feasible = margin > 0
            r = rm.evaluate_risk(**self._nominal_kwargs(
                feasibility=_feasible(mission_margin=margin, mission_feasible=feasible,
                                      rtl_margin=60.0, rtl_feasible=True)))
            results.append(r)

        levels = [r.level for r in results]
        self.assertEqual(levels, [rm.LEVEL_LOW, rm.LEVEL_ELEVATED, rm.LEVEL_HIGH, rm.LEVEL_CRITICAL])

        # The underlying score stays continuous and strictly increasing --
        # only the categorical level makes discrete jumps.
        scores = [r.score for r in results]
        for earlier, later in zip(scores, scores[1:]):
            self.assertLess(earlier, later)

    # ── Multi-risk: energy floor + communication floor, no special-case ─────
    def test_energy_floor_plus_partitioned_communication_no_special_casing(self):
        # Energy margin +10% (ELEVATED floor) together with PARTITIONED
        # communication (also an ELEVATED floor) -- the weighted score must
        # reflect BOTH components, and the governing level must be at least
        # the max of (energy floor, communication floor, weighted level),
        # with no new interaction rule of its own.
        energy_only = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=10.0, rtl_margin=60.0)))
        comm_only = rm.evaluate_risk(**self._nominal_kwargs(comm_state="PARTITIONED"))
        combined = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=10.0, rtl_margin=60.0), comm_state="PARTITIONED"))

        self.assertGreater(combined.weighted_score, energy_only.weighted_score)
        self.assertGreater(combined.weighted_score, comm_only.weighted_score)

        expected_min_level = rm._max_severity(
            energy_only.component_floor_level, comm_only.component_floor_level, combined.weighted_level)
        self.assertEqual(
            rm._max_severity(combined.level, expected_min_level), combined.level)
        self.assertIn(combined.level, (rm.LEVEL_ELEVATED, rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))

    # ── UNKNOWN semantics: no energy floor can be inferred from missing
    # margin evidence (task section 3) -- the separate UNKNOWN-evidence rule
    # governs instead, never a fabricated floor and never LOW.
    def test_missing_energy_evidence_infers_no_floor(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_feasible=None, rtl_feasible=None,
                                  mission_margin=None, rtl_margin=None, status="UNKNOWN")))
        self.assertIsNone(r.component_floor_level)
        self.assertNotEqual(r.level, rm.LEVEL_LOW)
        self.assertEqual(r.level, rm.LEVEL_UNKNOWN)

    # ── Config ordering (task section 4) ─────────────────────────────────────
    def test_config_ordering_is_validated(self):
        ok, issues = risk_config.validate(risk_config.RiskConfig(
            energy_margin_safe_percent=30.0, energy_elevated_margin_percent=15.0,
            energy_high_margin_percent=5.0, energy_margin_critical_percent=0.0))
        self.assertTrue(ok, issues)

        ok, issues = risk_config.validate(risk_config.RiskConfig(
            energy_margin_safe_percent=30.0, energy_elevated_margin_percent=5.0,
            energy_high_margin_percent=15.0, energy_margin_critical_percent=0.0))
        self.assertFalse(ok)


class TestProvenance(unittest.TestCase):
    """Injected evidence must remain visible and distinct from physical
    evidence (task section 15/26)."""

    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def test_injected_low_battery_raises_energy_risk_and_stays_labelled(self):
        physical_high = rm.evaluate_energy(
            _feasible(mission_margin=50.0, rtl_margin=50.0,
                     battery_percent=90.0, physical_battery_percent=90.0), self.cfg)
        injected_low = rm.evaluate_energy(
            _feasible(mission_margin=-5.0, mission_feasible=False,
                     battery_percent=15.0, battery_source="INJECTED",
                     physical_battery_percent=90.0, injected_battery_percent=15.0), self.cfg)
        self.assertLess(physical_high.score, injected_low.score)
        self.assertEqual(injected_low.evidence["battery_source"], "INJECTED")
        self.assertEqual(injected_low.evidence["physical_battery_percent"], 90.0)
        self.assertEqual(injected_low.evidence["injected_battery_percent"], 15.0)

    def test_clearing_injection_returns_to_physical_evidence(self):
        cleared = rm.evaluate_energy(
            _feasible(mission_margin=50.0, rtl_margin=50.0,
                     battery_percent=90.0, battery_source="PHYSICAL",
                     physical_battery_percent=90.0, injected_battery_percent=None), self.cfg)
        self.assertEqual(cleared.evidence["battery_source"], "PHYSICAL")
        self.assertIsNone(cleared.evidence["injected_battery_percent"])
        self.assertEqual(cleared.score, 0.0)


class TestNoSideEffects(unittest.TestCase):
    """Proves risk_model.py never touches a vehicle-affecting function (task
    section 28) -- purely by import surface: the module must not import any
    of the write-capable client/gateway/controller modules, and must not
    define ARM/DISARM/mode/LOITER/RTL/Set-Home/upload/replan-execution
    symbols of its own."""

    _FORBIDDEN_IMPORTS = (
        "api_client", "command_executor", "command_handler", "write_arbiter",
        "mission_execution_gateway", "replan_gateway", "mission_upload_worker",
        "pixhawk_mission", "autonomy_gate",
    )
    _FORBIDDEN_SYMBOLS = (
        "arm", "disarm", "set_mode", "loiter", "rtl", "set_home",
        "upload_mission", "execute_replan",
    )

    def test_module_imports_no_write_capable_dependency(self):
        with open("risk_model.py") as f:
            source = f.read()
        for name in self._FORBIDDEN_IMPORTS:
            self.assertNotIn(f"import {name}", source,
                             f"risk_model.py must not import {name!r} (write-capable)")

    def test_module_defines_no_vehicle_write_function(self):
        for name in self._FORBIDDEN_SYMBOLS:
            self.assertFalse(hasattr(rm, name), f"risk_model.py must not define {name!r}")

    def test_evaluate_risk_is_pure_and_returns_new_object_each_call(self):
        cfg = risk_config.RiskConfig()
        kwargs = dict(
            feasibility=_feasible(), comm_state="CONNECTED", control_authority="LOCAL_AGENT",
            mission_execution_status=_ME_STATUS_RUNNING, replan_status=_REPLAN_MONITORING,
            navigation_evidence=_nav_evidence(), failsafe={"status": "OK"},
            imu={"imu_health": "OK"}, cfg=cfg,
        )
        r1 = rm.evaluate_risk(**kwargs)
        r2 = rm.evaluate_risk(**kwargs)
        self.assertIsNot(r1, r2)
        self.assertEqual(r1.score, r2.score)


class TestRecommendationSemantics(unittest.TestCase):
    """E2 water-trial integration task, section 13's lettered acceptance
    criteria: risk LEVEL (severity) and mission-level RECOMMENDATION are
    related but not identical, and CRITICAL does NOT universally imply HOLD.
    Every case goes through the full evaluate_risk() aggregate."""

    def setUp(self):
        self.cfg = risk_config.RiskConfig()

    def _nominal_kwargs(self, **overrides):
        kwargs = dict(
            feasibility=_feasible(),
            comm_state="CONNECTED",
            control_authority="LOCAL_AGENT",
            mission_execution_status=_ME_STATUS_RUNNING,
            replan_status=_REPLAN_MONITORING,
            navigation_evidence=_nav_evidence(),
            failsafe={"status": "OK"},
            imu={"imu_health": "OK"},
            cfg=self.cfg,
        )
        kwargs.update(overrides)
        return kwargs

    # A -- mission comfortable -> LOW -> CONTINUE.
    def test_A_comfortable_mission_is_low_and_continue(self):
        r = rm.evaluate_risk(**self._nominal_kwargs())
        self.assertEqual(r.level, rm.LEVEL_LOW)
        self.assertEqual(r.recommendation, rm.RECOMMEND_CONTINUE)

    # B -- degraded but acceptable -> ELEVATED -> CONTINUE_WITH_CAUTION.
    def test_B_degraded_but_acceptable_is_continue_with_caution(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(comm_state="PARTITIONED"))
        self.assertEqual(r.level, rm.LEVEL_ELEVATED)
        self.assertEqual(r.recommendation, rm.RECOMMEND_CONTINUE_WITH_CAUTION)

    # C -- energy too low to continue, return feasible -> RETURN_HOME.
    def test_C_energy_critical_return_feasible_is_return_home(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=-2.0, mission_feasible=False,
                                  rtl_margin=40.0, rtl_feasible=True)))
        self.assertEqual(r.level, rm.LEVEL_CRITICAL)
        self.assertEqual(r.recommendation, rm.RECOMMEND_RETURN)

    # D -- critical condition, safe return cannot be proven -> HOLD.
    def test_D_critical_return_unproven_is_hold(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=-2.0, mission_feasible=False,
                                  rtl_margin=-1.0, rtl_feasible=False)))
        self.assertEqual(r.level, rm.LEVEL_CRITICAL)
        self.assertEqual(r.recommendation, rm.RECOMMEND_HOLD)

    # ── E3: communication-only degradation, current policy's actual answer ──
    # (task: "we need to know whether current policy produces CONTINUE_WITH_
    # CAUTION / REQUEST_HOLD / REQUEST_RETURN_HOME" for comm-driven severity).
    # A comm-only DISCONNECTED never reaches RECOMMEND_RETURN: _recommendation
    # only escalates HIGH/CRITICAL to RETURN when the DOMINANT component is
    # energy AND rtl_return_feasible is True -- a pure comm degradation's
    # dominant component is communication, so the answer is the conservative
    # HOLD (station-keep), not a return, with everything else nominal.
    def test_E_disconnected_comm_only_no_autonomous_execution_proof_is_hold(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            comm_state="DISCONNECTED", control_authority="OPERATOR",
            mission_execution_status={"supported": True, "state": "NOT_READY", "binding": {}}))
        self.assertEqual(r.level, rm.LEVEL_HIGH)
        self.assertEqual(r.dominant_component, "communication")
        self.assertEqual(r.recommendation, rm.RECOMMEND_HOLD)
        self.assertNotEqual(r.recommendation, rm.RECOMMEND_RETURN)

    def test_F_disconnected_comm_only_autonomous_execution_proven_is_still_hold(self):
        # Even with a PROVEN autonomous-continuation posture (LOCAL_AGENT
        # authority, RUNNING) -- the lower of the two DISCONNECTED scores
        # (0.70 vs 0.95) -- the floor still reads HIGH (both DISCONNECTED
        # floor reasons map to LEVEL_HIGH), so the recommendation is
        # unchanged: HOLD, never an unearned RETURN_HOME.
        r = rm.evaluate_risk(**self._nominal_kwargs(
            comm_state="DISCONNECTED", control_authority="LOCAL_AGENT",
            mission_execution_status=_ME_STATUS_RUNNING))
        self.assertEqual(r.level, rm.LEVEL_HIGH)
        self.assertEqual(r.recommendation, rm.RECOMMEND_HOLD)

    # E -- CRITICAL does NOT universally imply HOLD: same CRITICAL severity,
    # opposite recommendation, purely a function of proven RTL feasibility.
    def test_E_critical_does_not_universally_imply_hold(self):
        returns = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=-2.0, mission_feasible=False,
                                  rtl_margin=40.0, rtl_feasible=True)))
        holds = rm.evaluate_risk(**self._nominal_kwargs(
            feasibility=_feasible(mission_margin=-2.0, mission_feasible=False,
                                  rtl_margin=-1.0, rtl_feasible=False)))
        self.assertEqual(returns.level, rm.LEVEL_CRITICAL)
        self.assertEqual(holds.level, rm.LEVEL_CRITICAL)
        self.assertNotEqual(returns.recommendation, holds.recommendation)
        self.assertEqual(returns.recommendation, rm.RECOMMEND_RETURN)
        self.assertEqual(holds.recommendation, rm.RECOMMEND_HOLD)

    # Severe navigation uncertainty where movement cannot be justified ->
    # HOLD, even when RTL feasibility happens to read True -- a return route
    # is only as trustworthy as the position it would be planned from.
    def test_navigation_dominant_critical_holds_even_if_rtl_reads_feasible(self):
        r = rm.evaluate_risk(**self._nominal_kwargs(
            navigation_evidence=_nav_evidence(gps_value=0),
            feasibility=_feasible(rtl_margin=40.0, rtl_feasible=True)))
        self.assertIn(r.level, (rm.LEVEL_HIGH, rm.LEVEL_CRITICAL))
        self.assertFalse(r.hard_constraint_violated)
        self.assertEqual(r.dominant_component, "navigation")
        self.assertEqual(r.recommendation, rm.RECOMMEND_HOLD)


if __name__ == "__main__":
    unittest.main()
