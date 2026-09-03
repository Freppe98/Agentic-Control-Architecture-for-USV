"""
Tests for the physically-grounded capacity/current/time energy model (task:
physically-grounded battery model) -- replacing the earlier
distance/usable_range_m abstraction. See mission_feasibility.py's own module
docstring ("Boundary rule" section) for the exact equations:

    route_time_h           = route_distance_m / design_speed_mps / 3600
    required_capacity_Ah   = conservative_current_A * route_time_h
    available_capacity_Ah  = nominal_capacity_Ah * usable_capacity_factor
                              * effective_battery_percent / 100
    reserve_capacity_Ah    = nominal_capacity_Ah * reserve_fraction
    capacity_margin_Ah     = available_capacity_Ah - required_capacity_Ah
                              - reserve_capacity_Ah
    margin_percent         = capacity_margin_Ah / nominal_capacity_Ah * 100
    feasible iff capacity_margin_Ah > 0

All scenarios below use fixed reference values chosen to exercise the
equation with clean, hand-checkable numbers -- NOT necessarily the live
replan_config.py ReplanConfig defaults, which are separate field-calibrated
prototype parameters (see replan_config.py's own comments; currently derived
from Scout energy characterization run run-20260821-130456-usv-2-1b52892f)
that may be recalibrated independently of these equation-correctness checks:
    nominal_capacity_Ah=40.0, conservative_current_A=9.0,
    design_speed_mps=1.0, usable_capacity_factor=0.8,
    mission_reserve_fraction=0.15, rtl_reserve_fraction=0.05
All scenarios in this file drive the MISSION dimension (via `distance_m`),
which still uses the unchanged 0.15 mission reserve -- none of the numeric
margins below change as a result of the RTL-reserve split. Dedicated
distance/battery-matrix and monotonicity coverage for the RTL dimension's own
(smaller) reserve lives in test_rtl_emergency_reserve.py.

    python3 test_mission_feasibility_energy_model.py
"""
import unittest

import mission_feasibility as mf
import risk_config
import risk_model

NOMINAL_CAPACITY_AH = 40.0
CONSERVATIVE_CURRENT_A = 9.0
DESIGN_SPEED_MPS = 1.0
USABLE_CAPACITY_FACTOR = 0.8
MISSION_RESERVE_FRACTION = 0.15
RTL_RESERVE_FRACTION = 0.05

_NOMINAL_NAV = {
    "gps": {"fix_type": {"state": "FRESH", "value": 3}},
    "ekf": {"state": "FRESH", "value": True},
    "position": {"state": "FRESH"},
}
_NOMINAL_HEALTH = {"status": "OK"}
_NOMINAL_IMU = {"imu_health": "OK"}
_NOMINAL_MISSION_STATUS = {"supported": True, "state": "READY"}
_NOMINAL_REPLAN_STATUS = {"fsm_state": "IDLE"}


def _evaluate(distance_m, battery, rtl_distance_m=0.0, **overrides):
    base = dict(
        current_position=(10.0, 10.0),
        position_age_s=1.0,
        mission_route=[{"latitude": 10.0 + distance_m / 111194.9, "longitude": 10.0}],
        current_sequence=1,
        planned_home=(10.0, 10.0),
        rtl_home=(10.0, 10.0),
        rtl_return_distance_m=rtl_distance_m,
        rtl_return_geometry_source=mf.RTL_METHOD_CALLER_SUPPLIED,
        physical_battery_percent=battery,
        injected_battery_percent=None,
        nominal_capacity_Ah=NOMINAL_CAPACITY_AH,
        conservative_current_A=CONSERVATIVE_CURRENT_A,
        design_speed_mps=DESIGN_SPEED_MPS,
        usable_capacity_factor=USABLE_CAPACITY_FACTOR,
        mission_reserve_fraction=MISSION_RESERVE_FRACTION,
        rtl_reserve_fraction=RTL_RESERVE_FRACTION,
        max_position_age_s=5.0,
        now=1000.0,
    )
    base.update(overrides)
    return mf.evaluate_mission_feasibility(**base)


def _risk_for(feasibility_dict):
    return risk_model.evaluate_risk(
        feasibility=feasibility_dict,
        comm_state="CONNECTED",
        control_authority="LOCAL_AGENT",
        mission_execution_status=_NOMINAL_MISSION_STATUS,
        replan_status=_NOMINAL_REPLAN_STATUS,
        navigation_evidence=_NOMINAL_NAV,
        failsafe=_NOMINAL_HEALTH,
        imu=_NOMINAL_IMU,
        cfg=risk_config.DEFAULT,
    )


class Test486mSanityCase(unittest.TestCase):
    """Task section 14's exact worked example: ~486 m route, 83% battery,
    default prototype calibration -> clearly FEASIBLE with an approximately
    +48.4% margin (route_time=0.135 h, consumption=1.215 Ah, usable
    remaining~=26.56 Ah, reserve=6 Ah, margin~=19.35 Ah)."""

    def test_486m_route_83_percent_battery_is_feasible_with_expected_margin(self):
        res = _evaluate(486.0, 83.0)
        self.assertAlmostEqual(res.estimated_mission_duration_h, 0.135, places=3)
        self.assertAlmostEqual(res.estimated_mission_capacity_Ah, 1.215, places=3)
        self.assertAlmostEqual(res.available_capacity_Ah, 26.56, places=2)
        self.assertEqual(res.mission_reserve_capacity_Ah, 6.0)
        self.assertAlmostEqual(res.mission_margin_percent, 48.4, delta=0.1)
        self.assertTrue(res.mission_feasible)
        self.assertEqual(res.status, mf.STATUS_FEASIBLE)


class TestLongMissionBoundaries(unittest.TestCase):
    # A. short route + high battery -> FEASIBLE
    def test_a_short_route_high_battery_feasible(self):
        res = _evaluate(50.0, 95.0)
        self.assertTrue(res.mission_feasible)
        self.assertEqual(res.status, mf.STATUS_FEASIBLE)

    # B. long route consumes nearly all remaining usable capacity -> HIGH/
    #    ELEVATED via existing risk margins (task section 15.B) -- still
    #    FEASIBLE (margin_percent=10.0, positive), but risk's non-
    #    compensatory energy floor (task: aggregate-semantics correction)
    #    demands at least ELEVATED severity below energy_elevated_margin_
    #    percent=15.
    def test_b_long_route_near_limit_elevated_risk(self):
        res = _evaluate(4960.0, 70.0)
        self.assertTrue(res.mission_feasible)
        self.assertAlmostEqual(res.mission_margin_percent, 10.0, delta=0.1)
        risk = _risk_for(res.to_dict())
        self.assertFalse(risk.hard_constraint_violated)
        self.assertIn(risk.level, (risk_model.LEVEL_ELEVATED, risk_model.LEVEL_HIGH))

    # C. required Ah exceeds available minus reserve -> INFEASIBLE
    def test_c_required_capacity_exceeds_available_minus_reserve_infeasible(self):
        res = _evaluate(8000.0, 60.0)
        self.assertLess(res.mission_margin_percent, 0)
        self.assertFalse(res.mission_feasible)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)
        self.assertEqual(res.reason, mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)

    # D. exact zero margin -> INFEASIBLE (zero slack is not a margin)
    def test_d_exact_zero_margin_infeasible(self):
        res = _evaluate(4000.0, 50.0)
        self.assertEqual(res.mission_margin_percent, 0.0)
        self.assertFalse(res.mission_feasible)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)

    # E. RTL route independently feasible while mission infeasible
    def test_e_rtl_independently_feasible_while_mission_infeasible(self):
        res = _evaluate(8000.0, 60.0, rtl_distance_m=10.0)
        self.assertFalse(res.mission_feasible)
        self.assertTrue(res.rtl_return_feasible)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)
        self.assertEqual(res.reason, mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)

    # F. mission route UNKNOWN (unavailable) while RTL remains feasible
    def test_f_mission_route_unavailable_rtl_still_feasible(self):
        res = _evaluate(0.0, 80.0, mission_route=None, rtl_distance_m=10.0)
        self.assertIsNone(res.mission_feasible)
        self.assertEqual(res.reason, mf.REASON_MISSION_UNAVAILABLE)
        self.assertTrue(res.rtl_return_feasible)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)


class TestE2BatteryInjectionRiskProgression(unittest.TestCase):
    """E2 experiment (task section 16): for FIXED mission geometry, lowering
    injected effective battery monotonically reduces available_capacity_Ah
    and mission margin, and the observational/advisory risk LEVEL
    progresses LOW -> ELEVATED -> HIGH -> CRITICAL. No autonomous action is
    taken anywhere in this chain -- every value here comes from pure,
    side-effect-free functions; `recommendation` is read but never acted on
    (see risk_model.py's own module docstring)."""

    _DISTANCE_M = 2000.0  # fixed mission geometry throughout the experiment
    _RTL_DISTANCE_M = 0.0  # fixed, close RTL Home -- isolates the mission
                           # dimension as the binding constraint throughout

    def _at_battery(self, battery):
        res = _evaluate(self._DISTANCE_M, 100.0, rtl_distance_m=self._RTL_DISTANCE_M,
                        injected_battery_percent=battery)
        risk = _risk_for(res.to_dict())
        return res, risk

    def test_battery_injection_progression_low_elevated_high_critical(self):
        available_capacities = []
        margins = []
        levels = []

        for battery in (90.0, 47.0, 37.0, 20.0):
            res, risk = self._at_battery(battery)
            available_capacities.append(res.available_capacity_Ah)
            margins.append(res.mission_margin_percent)
            levels.append(risk.level)

        # available_capacity_Ah and mission margin strictly fall as injected
        # battery falls (monotonic, same fixed geometry throughout).
        self.assertEqual(available_capacities, sorted(available_capacities, reverse=True))
        self.assertEqual(margins, sorted(margins, reverse=True))

        self.assertEqual(levels, [
            risk_model.LEVEL_LOW, risk_model.LEVEL_ELEVATED,
            risk_model.LEVEL_HIGH, risk_model.LEVEL_CRITICAL,
        ])

        # The CRITICAL point is a genuine hard-feasibility violation (margin
        # <= 0), not a score/floor artifact -- and RTL (fixed near-zero
        # distance) remains independently feasible even there.
        critical_res, critical_risk = self._at_battery(20.0)
        self.assertFalse(critical_res.mission_feasible)
        self.assertTrue(critical_res.rtl_return_feasible)
        self.assertTrue(critical_risk.hard_constraint_violated)


if __name__ == "__main__":
    unittest.main()
