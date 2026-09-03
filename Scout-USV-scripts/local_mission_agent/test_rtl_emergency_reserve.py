"""
Dedicated coverage for the RTL/emergency-return reserve split (task: RTL-
reserve semantics correction).

BUG THIS FIXES
-----------------------------------------------------------------------
Before this task, mission_feasibility.py's capacity/current/time energy model
(see its own module docstring's "Boundary rule" section) computed a SINGLE
reserve_capacity_Ah from a single reserve_fraction=0.15 and applied it to
BOTH the ongoing-mission-completion margin AND the emergency-return (RTL)
margin. Because a zero-distance return still has to clear the reserve alone
(available_capacity_Ah > reserve_capacity_Ah), sharing the mission's
conservative 15% reserve meant NO RTL could ever be proven feasible below
`125 * 0.15` = 18.75% state of charge -- even when the current verified Home
was a few metres away. The two questions this module answers are genuinely
different (see mission_feasibility.py's own module docstring): "can the
mission still be finished" carries open-ended future-mission uncertainty,
while "can I get home from here, right now" is one well-defined maneuver.
Reusing the larger reserve for the smaller, more urgent question made the
emergency case strictly harder to prove than it needed to be -- precisely
backwards for a safety mechanism.

THE FIX
-----------------------------------------------------------------------
Two independent config knobs now exist (replan_config.ReplanConfig):
  mission_reserve_fraction = 0.15  (unchanged -- conservative, many unknowns)
  rtl_reserve_fraction     = 0.05  (new -- one well-defined immediate return)
Recommended-default derivation (reported to and confirmed by the operator
before implementation): 0.05 is one third of the mission reserve, sized to
buffer the RTL-specific uncertainty sources (straight-line-distance
under-estimate of the real course, final docking/station-keeping, battery-
percent sensor noise) plus a non-zero floor against deep discharge, without
reintroducing a reserve large enough to block a near-home return at a
realistic low-but-nonzero battery level.

EQUATIONS (mirrors mission_feasibility.py's module docstring formula shape,
using this test module's own round CONSERVATIVE_CURRENT_A/DESIGN_SPEED_MPS
fixture constants below for a clean worked example -- NOT the calibrated
field-experiment defaults in replan_config.ReplanConfig, which are
conservative_current_A=3.5 A / design_speed_mps=0.85 m/s; nominal
capacity 40 Ah, usable_capacity_factor 0.8, conservative_current_A 9 A,
design_speed_mps 1.0 m/s):
    required_Ah(d)       = conservative_current_A * d / design_speed_mps / 3600
                          = 0.0025 * d                              (d in metres)
    available_Ah(b)      = nominal_capacity_Ah * usable_capacity_factor * b / 100
                          = 0.32 * b                                 (b = battery %)
    rtl_reserve_Ah        = nominal_capacity_Ah * rtl_reserve_fraction = 2.0 Ah
    rtl_margin_percent(b, d) = (available_Ah(b) - required_Ah(d) - rtl_reserve_Ah)
                               / nominal_capacity_Ah * 100
                             = 0.8*b - 0.00625*d - 5.0
    feasible iff rtl_margin_percent > 0
i.e. at d=0, feasible iff battery > 6.25% (vs. the old shared-reserve floor
of 18.75%).

    python3 test_rtl_emergency_reserve.py
"""
import unittest
from types import SimpleNamespace

import decision_policy as dp
import mission_feasibility as mf
import replan_config
import risk_config
import risk_model

NOMINAL_CAPACITY_AH = 40.0
CONSERVATIVE_CURRENT_A = 9.0
DESIGN_SPEED_MPS = 1.0
USABLE_CAPACITY_FACTOR = 0.8
MISSION_RESERVE_FRACTION = 0.15
RTL_RESERVE_FRACTION = 0.05

# Representative return distances and battery levels (task requirement).
DISTANCES_M = (0.0, 100.0, 300.0, 500.0, 1000.0)
BATTERIES_PCT = (25.0, 20.0, 15.0, 12.0, 8.0, 5.0)

_NOMINAL_NAV = {
    "gps": {"fix_type": {"state": "FRESH", "value": 3}},
    "ekf": {"state": "FRESH", "value": True},
    "position": {"state": "FRESH"},
}
_NOMINAL_HEALTH = {"status": "OK"}
_NOMINAL_IMU = {"imu_health": "OK"}
_NOMINAL_MISSION_STATUS = {"supported": True, "state": "READY"}
_NOMINAL_REPLAN_STATUS = {"fsm_state": "IDLE"}


def _rtl_only(distance_m, battery_percent, **overrides):
    """Isolate the RTL dimension: mission_route=None makes the mission
    dimension UNKNOWN (never FEASIBLE/INFEASIBLE), which the module docstring
    guarantees never affects rtl_return_feasible/rtl_return_margin_percent --
    see mission_feasibility.py's "Three-valued status" section. The RTL
    distance is supplied directly (rtl_return_distance_m) rather than via
    lat/lon so the numbers below match the closed-form equations exactly,
    with no haversine-approximation slack."""
    base = dict(
        current_position=(10.0, 10.0),
        position_age_s=1.0,
        mission_route=None,
        current_sequence=None,
        planned_home=None,
        rtl_home=(10.0, 10.0),
        rtl_return_distance_m=distance_m,
        rtl_return_geometry_source=mf.RTL_METHOD_CALLER_SUPPLIED,
        physical_battery_percent=battery_percent,
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


def _expected_rtl_margin_percent(distance_m, battery_percent, rtl_reserve_fraction=RTL_RESERVE_FRACTION):
    return round(
        USABLE_CAPACITY_FACTOR * battery_percent
        - (CONSERVATIVE_CURRENT_A / DESIGN_SPEED_MPS / 3600.0 * distance_m) / NOMINAL_CAPACITY_AH * 100.0
        - rtl_reserve_fraction * 100.0,
        2,
    )


class TestDefaultReserveValues(unittest.TestCase):
    """The chosen defaults themselves -- guards against either value being
    silently changed without re-deriving the other."""

    def test_rtl_reserve_default_is_one_third_of_mission_reserve(self):
        cfg = replan_config.ReplanConfig()
        self.assertEqual(cfg.mission_reserve_fraction, 0.15)
        self.assertEqual(cfg.rtl_reserve_fraction, 0.05)
        self.assertAlmostEqual(cfg.rtl_reserve_fraction, cfg.mission_reserve_fraction / 3.0)

    def test_rtl_reserve_strictly_smaller_than_mission_reserve(self):
        cfg = replan_config.ReplanConfig()
        self.assertLess(cfg.rtl_reserve_fraction, cfg.mission_reserve_fraction)

    def test_rtl_reserve_env_override_is_independent_of_mission_reserve(self):
        import os
        os.environ["REPLAN_RTL_RESERVE_FRACTION"] = "0.08"
        try:
            cfg = replan_config.load()
            self.assertEqual(cfg.rtl_reserve_fraction, 0.08)
            self.assertEqual(cfg.mission_reserve_fraction, 0.15)  # untouched
        finally:
            del os.environ["REPLAN_RTL_RESERVE_FRACTION"]


class TestRtlReserveMatrix(unittest.TestCase):
    """Representative return distances (0 m, 100 m, 300 m, 500 m, 1 km) x
    battery levels (25%, 20%, 15%, 12%, 8%, 5%) -- task requirement. Every
    cell's margin is checked against the closed-form equation derived above,
    and its feasibility boolean against the same >0 rule the module uses."""

    def test_margin_and_feasibility_match_closed_form_across_matrix(self):
        for battery in BATTERIES_PCT:
            for distance in DISTANCES_M:
                with self.subTest(battery=battery, distance=distance):
                    res = _rtl_only(distance, battery)
                    expected_margin = _expected_rtl_margin_percent(distance, battery)
                    # delta, not places: _dimension_capacity rounds duration_h
                    # (4dp) and required_Ah (4dp) before the final margin_
                    # percent (2dp), so a <=0.01-point double-rounding
                    # residue vs. the un-rounded closed form is expected, not
                    # a scaling error (same convention as test_mission_
                    # feasibility.py's test_injected_battery_affects_both_
                    # margins_identically).
                    self.assertAlmostEqual(res.rtl_return_margin_percent, expected_margin, delta=0.02)
                    self.assertEqual(res.rtl_return_feasible, expected_margin > 0)

    # Spot-check the exact table reported to the operator before implementation.
    def test_reported_table_values(self):
        cases = {
            (25.0, 0.0): 15.0, (25.0, 1000.0): 8.75,
            (20.0, 0.0): 11.0, (20.0, 1000.0): 4.75,
            (15.0, 0.0): 7.0, (15.0, 1000.0): 0.75,
            (12.0, 0.0): 4.6, (12.0, 1000.0): -1.65,
            (8.0, 0.0): 1.4, (8.0, 300.0): -0.475,
            (5.0, 0.0): -1.0,
        }
        for (battery, distance), expected in cases.items():
            with self.subTest(battery=battery, distance=distance):
                res = _rtl_only(distance, battery)
                self.assertAlmostEqual(res.rtl_return_margin_percent, expected, delta=0.01)

    def test_margin_strictly_decreases_with_distance_at_fixed_battery(self):
        for battery in BATTERIES_PCT:
            margins = [_rtl_only(d, battery).rtl_return_margin_percent for d in DISTANCES_M]
            self.assertEqual(margins, sorted(margins, reverse=True))
            self.assertEqual(len(set(margins)), len(margins), "expected strictly distinct margins")

    def test_margin_strictly_increases_with_battery_at_fixed_distance(self):
        ordered_batteries = sorted(BATTERIES_PCT)
        for distance in DISTANCES_M:
            margins = [_rtl_only(distance, b).rtl_return_margin_percent for b in ordered_batteries]
            self.assertEqual(margins, sorted(margins))
            self.assertEqual(len(set(margins)), len(margins), "expected strictly distinct margins")

    def test_zero_distance_feasibility_floor_matches_derivation(self):
        # feasible iff battery > 125 * rtl_reserve_fraction = 6.25% at default.
        just_below = _rtl_only(0.0, 6.24)
        just_above = _rtl_only(0.0, 6.26)
        self.assertFalse(just_below.rtl_return_feasible)
        self.assertTrue(just_above.rtl_return_feasible)

    def test_bug_regression_near_home_low_battery_now_provably_feasible(self):
        """The exact reported bug: Home a few metres away, battery well below
        the OLD shared-15%-reserve floor of 18.75%, now correctly provable
        feasible under the smaller emergency-return reserve."""
        res = _rtl_only(distance_m=5.0, battery_percent=15.0)
        self.assertTrue(res.rtl_return_feasible)
        self.assertGreater(res.rtl_return_margin_percent, 0.0)


class TestMissionReserveUnchanged(unittest.TestCase):
    """Regression guard: the mission-completion reserve must stay
    conservative (unchanged 15%) -- this task narrows the RTL reserve, it
    must never accidentally weaken mission-completion conservatism too."""

    def test_mission_zero_distance_floor_still_18_75_percent(self):
        res = _rtl_only(0.0, 18.75, mission_route=[{"latitude": 10.0, "longitude": 10.0}],
                        current_sequence=1)
        self.assertEqual(res.mission_margin_percent, 0.0)
        self.assertFalse(res.mission_feasible)

    def test_mission_reserve_capacity_ah_still_6_ah_at_default_nominal(self):
        res = _rtl_only(0.0, 80.0, mission_route=[{"latitude": 10.0, "longitude": 10.0}],
                        current_sequence=1)
        self.assertEqual(res.mission_reserve_capacity_Ah, NOMINAL_CAPACITY_AH * MISSION_RESERVE_FRACTION)


class TestReserveIndependence(unittest.TestCase):
    """The two reserve fractions are independent config knobs -- changing one
    must never move the other dimension's margin (module docstring's "TWO
    DISTINCT RESERVES" section)."""

    def test_changing_rtl_reserve_does_not_move_mission_margin(self):
        route = [{"latitude": 10.02, "longitude": 10.0}]  # ~2224 m
        tight = _rtl_only(0.0, 40.0, mission_route=route, current_sequence=1,
                          rtl_reserve_fraction=0.02)
        loose = _rtl_only(0.0, 40.0, mission_route=route, current_sequence=1,
                          rtl_reserve_fraction=0.20)
        self.assertEqual(tight.mission_margin_percent, loose.mission_margin_percent)
        self.assertNotEqual(tight.rtl_return_margin_percent, loose.rtl_return_margin_percent)

    def test_changing_mission_reserve_does_not_move_rtl_margin(self):
        tight = _rtl_only(300.0, 40.0, mission_reserve_fraction=0.05)
        loose = _rtl_only(300.0, 40.0, mission_reserve_fraction=0.30)
        self.assertEqual(tight.rtl_return_margin_percent, loose.rtl_return_margin_percent)


class TestReturnHomeVsHoldDecision(unittest.TestCase):
    """End-to-end acceptance criterion (task): with the fixed reserve
    semantics, wire a real MissionFeasibilityResult through risk_model and
    decision_policy and prove the correct action falls out --
    REQUEST_RETURN_HOME when mission continuation is infeasible but return
    remains feasible, REQUEST_HOLD only when return itself cannot be proven
    feasible either."""

    def _decide(self, mission_distance_m, rtl_distance_m, battery_percent):
        res = mf.evaluate_mission_feasibility(
            current_position=(10.0, 10.0),
            position_age_s=1.0,
            mission_route=[{"latitude": 10.0 + mission_distance_m / 111194.9, "longitude": 10.0}],
            current_sequence=1,
            planned_home=(10.0, 10.0),
            rtl_home=(10.0, 10.0),
            physical_battery_percent=battery_percent,
            injected_battery_percent=None,
            rtl_return_distance_m=rtl_distance_m,
            rtl_return_geometry_source=mf.RTL_METHOD_CALLER_SUPPLIED,
            nominal_capacity_Ah=NOMINAL_CAPACITY_AH,
            conservative_current_A=CONSERVATIVE_CURRENT_A,
            design_speed_mps=DESIGN_SPEED_MPS,
            usable_capacity_factor=USABLE_CAPACITY_FACTOR,
            mission_reserve_fraction=MISSION_RESERVE_FRACTION,
            rtl_reserve_fraction=RTL_RESERVE_FRACTION,
            max_position_age_s=5.0,
            now=1000.0,
        )
        risk = risk_model.evaluate_risk(
            feasibility=res.to_dict(),
            comm_state="CONNECTED",
            control_authority="LOCAL_AGENT",
            mission_execution_status=_NOMINAL_MISSION_STATUS,
            replan_status=_NOMINAL_REPLAN_STATUS,
            navigation_evidence=_NOMINAL_NAV,
            failsafe=_NOMINAL_HEALTH,
            imu=_NOMINAL_IMU,
            cfg=risk_config.DEFAULT,
        )
        action = dp.DecisionPolicy().evaluate(risk, res, SimpleNamespace(snapshot_id="s1"))
        return res, risk, action

    def test_mission_infeasible_rtl_feasible_near_home_low_battery_returns_home(self):
        # 2000 m remaining mission is out of reach at 12% battery under the
        # mission's 15% reserve; 300 m back to the verified Home is well
        # within reach under the smaller 5% RTL reserve.
        res, risk, action = self._decide(mission_distance_m=2000.0, rtl_distance_m=300.0, battery_percent=12.0)
        self.assertFalse(res.mission_feasible)
        self.assertTrue(res.rtl_return_feasible)
        self.assertTrue(risk.hard_constraint_violated)
        self.assertEqual(risk.recommendation, risk_model.RECOMMEND_RETURN)
        self.assertEqual(action.action, dp.ACTION_REQUEST_RETURN_HOME)

    def test_both_infeasible_at_critical_battery_holds(self):
        # Same geometry, battery low enough (5%) that even the smaller RTL
        # reserve cannot be cleared -- return itself cannot be proven, so the
        # policy must fall back to HOLD, never RETURN_HOME.
        res, risk, action = self._decide(mission_distance_m=2000.0, rtl_distance_m=300.0, battery_percent=5.0)
        self.assertFalse(res.mission_feasible)
        self.assertFalse(res.rtl_return_feasible)
        self.assertTrue(risk.hard_constraint_violated)
        self.assertEqual(risk.recommendation, risk_model.RECOMMEND_HOLD)
        self.assertEqual(action.action, dp.ACTION_REQUEST_HOLD)

    def test_bug_regression_previously_unreachable_return_home_now_reachable(self):
        """The exact reported bug, end to end: mission far out of reach, Home
        just 5 m away, battery at 15% -- BELOW the old shared-reserve RTL
        floor of 18.75%, but ABOVE the new RTL-specific floor. Before this
        fix, mission_feasibility would have reported rtl_return_feasible=
        False here (forcing HOLD); after the fix, the decision policy
        correctly requests RETURN_HOME instead."""
        res, risk, action = self._decide(mission_distance_m=5000.0, rtl_distance_m=5.0, battery_percent=15.0)
        self.assertFalse(res.mission_feasible)
        self.assertTrue(res.rtl_return_feasible)
        self.assertEqual(action.action, dp.ACTION_REQUEST_RETURN_HOME)

    def test_both_feasible_continues_no_action(self):
        res, risk, action = self._decide(mission_distance_m=50.0, rtl_distance_m=50.0, battery_percent=90.0)
        self.assertTrue(res.mission_feasible)
        self.assertTrue(res.rtl_return_feasible)
        self.assertEqual(action.action, dp.ACTION_NONE)


if __name__ == "__main__":
    unittest.main()
