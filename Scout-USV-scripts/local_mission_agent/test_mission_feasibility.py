"""
Unit tests for mission_feasibility.py -- the mission-energy-feasibility
evidence model, corrected to keep two distinct Home concepts (task: "mission-
energy-feasibility Home semantics correction") and covering the physically-
grounded capacity/current/time energy model (task: physically-grounded
battery model):

    mission_feasible     -- can the remaining OPERATOR-PLANNED mission still
                             be completed to its PLANNED mission Home/end?
    rtl_return_feasible  -- could the vehicle abandon NOW and safely return to
                             the CURRENT VERIFIED Pixhawk/RTL Home?

    python3 test_mission_feasibility.py

Pure-function tests only: no gateway, no HTTP, no disk I/O -- every case
constructs explicit evidence and asserts the resulting
MissionFeasibilityResult, exactly the "deterministic, side-effect-free,
highly testable" module this exercises.

Energy model constants used throughout -- fixed reference values chosen for
clean, hand-checkable arithmetic, NOT necessarily equal to the live
replan_config.py ReplanConfig defaults (those are separate field-calibrated
prototype parameters, currently derived from Scout energy characterization
run run-20260821-130456-usv-2-1b52892f, and may be recalibrated independently
of these equation-correctness checks):
    nominal_capacity_Ah = 40.0, conservative_current_A = 9.0,
    design_speed_mps = 1.0, usable_capacity_factor = 0.8,
    mission_reserve_fraction = 0.15, rtl_reserve_fraction = 0.05
so that, for a distance d (m) and effective battery b (%):
    available_capacity_Ah        = 0.32 * b
    mission_reserve_capacity_Ah  = 6.0
    rtl_reserve_capacity_Ah      = 2.0
    required_capacity_Ah         = 0.0025 * d
    mission_margin_percent       = (0.32*b - 0.0025*d - 6.0) / 40.0 * 100
                                  = 0.8*b - 0.00625*d - 15.0
    rtl_return_margin_percent    = (0.32*b - 0.0025*d - 2.0) / 40.0 * 100
                                  = 0.8*b - 0.00625*d - 5.0
The two reserves are deliberately different (task: RTL-reserve semantics
correction) -- see mission_feasibility.py's module docstring's "TWO DISTINCT
RESERVES" section for the full derivation; dedicated distance/battery-matrix
and monotonicity coverage for the RTL reserve lives in
test_rtl_emergency_reserve.py.
Route-identity tests (the mission-route-identity-safety invariant) live in
test_mission_feasibility_route_identity.py; capacity-model boundary/injection
scenarios (486 m sanity case, INFEASIBLE boundaries, E2 injection progression)
live in test_mission_feasibility_energy_model.py.
"""
import unittest

import geo
import mission_feasibility as mf

# Fixed reference capacity-model constants (equation-correctness checks --
# NOT necessarily equal to replan_config.py's live, field-calibrated
# ReplanConfig defaults) -- shared by every _evaluate() call below.
NOMINAL_CAPACITY_AH = 40.0
CONSERVATIVE_CURRENT_A = 9.0
DESIGN_SPEED_MPS = 1.0
USABLE_CAPACITY_FACTOR = 0.8
MISSION_RESERVE_FRACTION = 0.15
RTL_RESERVE_FRACTION = 0.05


def _evaluate(**kwargs):
    base = dict(
        current_position=(10.0, 10.0),
        position_age_s=1.0,
        mission_route=[{"latitude": 10.0, "longitude": 10.0}],
        current_sequence=1,
        planned_home=(10.0, 10.0),
        rtl_home=(10.0, 10.0),
        physical_battery_percent=80.0,
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
    base.update(kwargs)
    return mf.evaluate_mission_feasibility(**base)


class TestResolveEffectiveBattery(unittest.TestCase):
    def test_no_reading_at_all_is_unknown_not_zero(self):
        # "Never replace invalid physical battery with zero."
        pct, source = mf.resolve_effective_battery(None, None)
        self.assertIsNone(pct)
        self.assertIsNone(source)

    def test_physical_only(self):
        pct, source = mf.resolve_effective_battery(72.0, None)
        self.assertEqual(pct, 72.0)
        self.assertEqual(source, mf.SOURCE_PHYSICAL)

    def test_injection_overrides_physical(self):
        pct, source = mf.resolve_effective_battery(94.0, 5.0)
        self.assertEqual(pct, 5.0)
        self.assertEqual(source, mf.SOURCE_INJECTED)

    def test_injection_with_no_physical_reading(self):
        pct, source = mf.resolve_effective_battery(None, 5.0)
        self.assertEqual(pct, 5.0)
        self.assertEqual(source, mf.SOURCE_INJECTED)


class TestFeasibility(unittest.TestCase):
    # 1. healthy battery + short mission + close RTL Home -> FEASIBLE
    def test_healthy_battery_short_mission_feasible(self):
        route = [{"latitude": 10.001, "longitude": 10.0}]  # ~111 m
        res = _evaluate(mission_route=route, physical_battery_percent=80.0,
                        rtl_home=(10.00045, 10.0))  # ~50 m from current position
        self.assertEqual(res.status, mf.STATUS_FEASIBLE)
        self.assertEqual(res.reason, mf.REASON_SUFFICIENT_ENERGY)
        self.assertTrue(res.mission_feasible)
        self.assertTrue(res.rtl_return_feasible)
        self.assertEqual(res.battery_source, mf.SOURCE_PHYSICAL)

    # 2. 5% effective battery + same mission -> INFEASIBLE
    def test_injected_low_battery_same_mission_infeasible(self):
        route = [{"latitude": 10.001, "longitude": 10.0}]
        res = _evaluate(mission_route=route, physical_battery_percent=80.0,
                        injected_battery_percent=5.0)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)
        self.assertEqual(res.reason, mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)
        self.assertFalse(res.mission_feasible)
        self.assertEqual(res.battery_percent, 5.0)
        self.assertEqual(res.battery_source, mf.SOURCE_INJECTED)
        # Physical reading is still preserved in the evidence.
        self.assertEqual(res.physical_battery_percent, 80.0)

    # 3. exact boundary case, MISSION dimension -> defined/tested behaviour
    #    (margin > 0 required). Zero distance on both dimensions ->
    #    mission_margin_percent = 0.8*battery - 15; battery=18.75 makes that
    #    exactly 0. The RTL dimension has its OWN, smaller reserve (task:
    #    RTL-reserve semantics correction) so it is comfortably feasible here
    #    even though the mission dimension is not -- see
    #    test_rtl_boundary_margin_exactly_zero_is_infeasible below for the
    #    RTL dimension's own, distinct boundary.
    def test_mission_boundary_margin_exactly_zero_is_infeasible(self):
        res = _evaluate(physical_battery_percent=18.75)
        self.assertEqual(res.mission_margin_percent, 0.0)
        self.assertFalse(res.mission_feasible)
        self.assertGreater(res.rtl_return_margin_percent, 0.0)
        self.assertTrue(res.rtl_return_feasible)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)
        self.assertEqual(res.reason, mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)

    def test_mission_boundary_margin_just_above_zero_is_feasible(self):
        # Margins are rounded to 2 decimals (repo convention -- see
        # energy_policy.py), so the smallest representable positive margin is
        # 0.01%, not an arbitrarily small epsilon.
        res = _evaluate(physical_battery_percent=18.76)
        self.assertGreater(res.mission_margin_percent, 0.0)
        self.assertTrue(res.mission_feasible)
        self.assertTrue(res.rtl_return_feasible)
        self.assertEqual(res.status, mf.STATUS_FEASIBLE)

    # 3b. exact boundary case, RTL dimension -> its OWN, smaller reserve
    #     (0.05 vs the mission's 0.15) means its zero-distance boundary sits
    #     at a much lower battery: rtl_return_margin_percent =
    #     0.8*battery - 5; battery=6.25 makes that exactly 0. This is the
    #     precise fix for the reported bug (RTL effectively infeasible below
    #     ~18.75% SOC even at 0 m) -- the RTL floor is now ~6.25% SOC, not
    #     ~18.75%.
    def test_rtl_boundary_margin_exactly_zero_is_infeasible(self):
        res = _evaluate(physical_battery_percent=6.25)
        self.assertEqual(res.rtl_return_margin_percent, 0.0)
        self.assertFalse(res.rtl_return_feasible)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)

    def test_rtl_boundary_margin_just_above_zero_is_feasible(self):
        res = _evaluate(physical_battery_percent=6.26)
        self.assertGreater(res.rtl_return_margin_percent, 0.0)
        self.assertTrue(res.rtl_return_feasible)

    # 4. invalid battery -> UNKNOWN / fail closed
    def test_invalid_battery_unknown(self):
        res = _evaluate(physical_battery_percent=None, injected_battery_percent=None)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertEqual(res.reason, mf.REASON_BATTERY_INVALID)
        self.assertIsNone(res.mission_feasible)
        self.assertIsNone(res.rtl_return_feasible)

    # 5. stale current position -> UNKNOWN / fail closed (both dimensions)
    def test_stale_position_unknown(self):
        res = _evaluate(position_age_s=100.0, max_position_age_s=5.0)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertEqual(res.reason, mf.REASON_POSITION_STALE)
        self.assertIsNone(res.mission_feasible)
        self.assertIsNone(res.rtl_return_feasible)

    def test_missing_position_unknown(self):
        res = _evaluate(current_position=None, position_age_s=1.0)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertEqual(res.reason, mf.REASON_POSITION_STALE)
        self.assertIsNone(res.mission_feasible)
        self.assertIsNone(res.rtl_return_feasible)

    # 6. no mission route -> mission UNKNOWN (RTL dimension still computed)
    def test_no_mission_unknown_but_rtl_still_computed(self):
        res = _evaluate(mission_route=None, rtl_home=(10.0001, 10.0))
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertEqual(res.reason, mf.REASON_MISSION_UNAVAILABLE)
        self.assertIsNone(res.mission_feasible)
        self.assertTrue(res.rtl_return_feasible)  # independent evidence, still available

    # 7. no verified RTL Home -> RTL dimension UNKNOWN (mission unaffected)
    def test_no_rtl_home_unknown(self):
        route = [{"latitude": 10.001, "longitude": 10.0}]
        res = _evaluate(mission_route=route, rtl_home=None)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertEqual(res.reason, mf.REASON_RTL_HOME_UNAVAILABLE)
        self.assertIsNone(res.rtl_return_feasible)
        self.assertIsNone(res.rtl_home)
        # Mission dimension is unaffected by a missing RTL Home.
        self.assertTrue(res.mission_feasible)

    # 8. planned mission Home far from current RTL Home -> the two distances
    #    target genuinely different destinations (the bench-evidence case).
    def test_planned_home_far_from_rtl_home_uses_distinct_targets(self):
        pos = (56.6635397, 12.8813428)     # Scout, in the garage
        rtl_home = (56.663544, 12.881348)  # ~1 m away -- current verified Home
        planned_home = (56.6635397, 12.8813428)  # the operator-authored planning Home
        route = [
            {"latitude": 56.6503285, "longitude": 12.8708991},  # ~1.6 km away, near the lake
        ]
        res = _evaluate(current_position=pos, mission_route=route, current_sequence=1,
                        planned_home=planned_home, rtl_home=rtl_home,
                        physical_battery_percent=92.0)
        # planned_completion targets the far lake route; rtl_return targets
        # the close garage Home -- neither distance is anywhere near the other.
        self.assertGreater(res.planned_completion_distance_m, 1000.0)
        self.assertLess(res.rtl_return_distance_m, 50.0)
        self.assertEqual(res.planned_home["latitude"], planned_home[0])
        self.assertEqual(res.rtl_home["latitude"], rtl_home[0])
        self.assertEqual(res.planned_home["source"], mf.HOME_SOURCE_PLANNING_PACKAGE)
        self.assertEqual(res.rtl_home["source"], mf.HOME_SOURCE_PIXHAWK_VERIFIED_HOME)
        self.assertEqual(res.rtl_return_geometry_source, mf.RTL_METHOD_STRAIGHT_LINE)

    # 9. Scout sitting AT its RTL Home -> rtl_return_distance_m ~= 0 and the
    #    RTL margin is strongly positive with a healthy battery, independent
    #    of how far away the planned mission itself is.
    def test_at_rtl_home_return_distance_near_zero_and_strongly_feasible(self):
        pos = (56.6635397, 12.8813428)
        rtl_home = (56.6635397, 12.8813428)  # exactly here
        route = [{"latitude": 56.6503285, "longitude": 12.8708991}]  # far mission
        res = _evaluate(current_position=pos, mission_route=route, current_sequence=1,
                        planned_home=(56.6635397, 12.8813428), rtl_home=rtl_home,
                        physical_battery_percent=92.0)
        self.assertEqual(res.rtl_return_distance_m, 0.0)
        self.assertTrue(res.rtl_return_feasible)
        # Zero-distance RTL margin is 0.8*battery - 15 = 58.6% at battery=92.
        self.assertGreater(res.rtl_return_margin_percent, 40.0)
        # The far planned mission stays large and is judged on its own terms.
        self.assertGreater(res.planned_completion_distance_m, 1000.0)

    # 10. route already includes final planned-Home connector -> Home not
    #     double counted, and the geometry-source tag says so.
    def test_route_ending_at_planned_home_not_double_counted_and_tagged(self):
        pos = (10.0, 10.0)
        planned_home = (10.003, 10.0)
        route = [
            {"latitude": 10.001, "longitude": 10.0, "segment": "OUTBOUND_TRANSIT"},
            {"latitude": 10.002, "longitude": 10.0, "segment": "PRIMARY_SURVEY"},
            {"latitude": 10.003, "longitude": 10.0, "segment": "RETURN"},  # == planned_home
        ]
        res = _evaluate(current_position=pos, mission_route=route, current_sequence=1,
                        planned_home=planned_home, rtl_home=(10.0, 10.0))
        expected = geo.path_length_m([pos] + [(wp["latitude"], wp["longitude"]) for wp in route])
        self.assertAlmostEqual(res.planned_completion_distance_m, round(expected, 1), places=1)
        # If planned_home had been appended a second time, the distance would
        # be strictly larger than the plain route length above.
        double_counted = expected + geo.haversine_m(route[-1]["latitude"], route[-1]["longitude"],
                                                     planned_home[0], planned_home[1])
        self.assertLess(res.planned_completion_distance_m, round(double_counted, 1) + 1)
        self.assertEqual(res.mission_geometry_source,
                         mf.MISSION_METHOD_REMAINING_ROUTE_ENDS_AT_PLANNED_HOME)

    # 11. route does NOT end at planned Home -- planning_package.py's own
    #     documented contract explicitly does not assume the route's last
    #     waypoint is Home (segments are optional; "the retrace safe-return
    #     strategy still works" without one). No connector is invented here:
    #     mission completion is judged purely on finishing the approved
    #     route, and the geometry-source tag reflects that no coincidence was
    #     found (never fabricates a "go to planned_home too" leg).
    def test_route_not_ending_at_planned_home_no_connector_invented(self):
        pos = (10.0, 10.0)
        planned_home = (10.5, 10.0)  # nowhere near the route
        route = [
            {"latitude": 10.001, "longitude": 10.0},
            {"latitude": 10.002, "longitude": 10.0},
        ]
        res = _evaluate(current_position=pos, mission_route=route, current_sequence=1,
                        planned_home=planned_home, rtl_home=(10.0, 10.0))
        expected = geo.path_length_m([pos] + [(wp["latitude"], wp["longitude"]) for wp in route])
        self.assertAlmostEqual(res.planned_completion_distance_m, round(expected, 1), places=1)
        self.assertEqual(res.mission_geometry_source, mf.MISSION_METHOD_REMAINING_ROUTE)
        # planned_home is still reported as evidence even though it plays no
        # part in the distance.
        self.assertEqual(res.planned_home["latitude"], planned_home[0])

    # 12. current Pixhawk (RTL) Home changes -> rtl_return_distance updates,
    #     planned_completion_distance is completely unaffected.
    def test_rtl_home_change_updates_only_rtl_distance(self):
        pos = (10.0, 10.0)
        route = [{"latitude": 10.001, "longitude": 10.0}]
        close = _evaluate(current_position=pos, mission_route=route,
                          rtl_home=(10.0, 10.0), planned_home=(10.5, 10.0))
        far = _evaluate(current_position=pos, mission_route=route,
                        rtl_home=(10.01, 10.0), planned_home=(10.5, 10.0))
        self.assertNotEqual(close.rtl_return_distance_m, far.rtl_return_distance_m)
        self.assertEqual(close.planned_completion_distance_m, far.planned_completion_distance_m)

    # 13. planned package Home changes in fixture -> planned_home evidence
    #     changes; planned_completion_distance and rtl_return_distance are
    #     BOTH unaffected (this repo's route contract does not tie mission
    #     completion to planned_home unless the route itself already ends
    #     there -- see test 11 above).
    def test_planned_home_change_does_not_move_either_distance(self):
        pos = (10.0, 10.0)
        route = [{"latitude": 10.001, "longitude": 10.0}]
        a = _evaluate(current_position=pos, mission_route=route,
                     rtl_home=(10.0, 10.0), planned_home=(10.5, 10.0))
        b = _evaluate(current_position=pos, mission_route=route,
                     rtl_home=(10.0, 10.0), planned_home=(20.0, 20.0))
        self.assertNotEqual(a.planned_home, b.planned_home)
        self.assertEqual(a.planned_completion_distance_m, b.planned_completion_distance_m)
        self.assertEqual(a.rtl_return_distance_m, b.rtl_return_distance_m)

    # 14. mid-mission sequence -> only remaining route contributes
    def test_mid_mission_sequence_excludes_passed_waypoints(self):
        pos = (10.0, 10.0)
        route = [
            {"latitude": 10.001, "longitude": 10.0},
            {"latitude": 10.002, "longitude": 10.0},
            {"latitude": 10.003, "longitude": 10.0},
        ]
        res = _evaluate(current_position=pos, mission_route=route, current_sequence=2)
        # seq=2 -> index 1 (target-inclusive) -> remaining is route[1:] (2 wps).
        self.assertEqual(res.remaining_waypoint_count, 2)
        expected = geo.path_length_m([pos, (route[1]["latitude"], route[1]["longitude"]),
                                      (route[2]["latitude"], route[2]["longitude"])])
        self.assertAlmostEqual(res.planned_completion_distance_m, round(expected, 1), places=1)

    # 15. mission complete / final waypoint -> remaining distance behaves
    #     correctly, and the Ah-based diagnostics read exactly zero too.
    def test_mission_complete_zero_remaining(self):
        route = [
            {"latitude": 10.001, "longitude": 10.0},
            {"latitude": 10.002, "longitude": 10.0},
        ]
        res = _evaluate(mission_route=route, current_sequence=len(route) + 1,
                        physical_battery_percent=50.0)
        self.assertEqual(res.remaining_waypoint_count, 0)
        self.assertEqual(res.planned_completion_distance_m, 0.0)
        self.assertEqual(res.estimated_mission_duration_h, 0.0)
        self.assertEqual(res.estimated_mission_capacity_Ah, 0.0)
        self.assertTrue(res.mission_feasible)

    # 16. injected battery overrides physical battery, identically for BOTH
    #     margins (task section 10.15) -- proven via two evaluations that
    #     differ only in which battery reading drives the calculation. The
    #     capacity model scales margin_percent by usable_capacity_factor per
    #     percentage point of battery (0.8, not 1:1, unlike the old distance/
    #     usable-range model), so the delta is battery_delta * factor.
    def test_injected_battery_affects_both_margins_identically(self):
        route = [{"latitude": 10.001, "longitude": 10.0}]
        physical = _evaluate(mission_route=route, rtl_home=(10.0003, 10.0),
                             physical_battery_percent=80.0)
        injected = _evaluate(mission_route=route, rtl_home=(10.0003, 10.0),
                             physical_battery_percent=80.0, injected_battery_percent=5.0)
        self.assertEqual(injected.battery_percent, 5.0)
        # Same distances (evidence unrelated to battery), different margins,
        # both shifted by exactly the same battery-delta-scaled amount.
        self.assertEqual(physical.planned_completion_distance_m, injected.planned_completion_distance_m)
        self.assertEqual(physical.rtl_return_distance_m, injected.rtl_return_distance_m)
        battery_delta = injected.battery_percent - physical.battery_percent
        expected_margin_delta = battery_delta * USABLE_CAPACITY_FACTOR
        # places=1: available_capacity_Ah and each margin_percent are rounded
        # independently (4dp / 2dp respectively), so a tiny <=0.01-point
        # double-rounding residue is expected, not a real scaling error.
        self.assertAlmostEqual(injected.mission_margin_percent - physical.mission_margin_percent,
                               expected_margin_delta, places=1)
        self.assertAlmostEqual(injected.rtl_return_margin_percent - physical.rtl_return_margin_percent,
                               expected_margin_delta, places=1)

    # 17. physical battery remains separately preserved in evidence
    def test_physical_battery_preserved(self):
        res = _evaluate(physical_battery_percent=90.0, injected_battery_percent=5.0)
        self.assertEqual(res.physical_battery_percent, 90.0)
        self.assertEqual(res.injected_battery_percent, 5.0)

    # 18. RTL margin calculated separately from mission margin
    def test_rtl_margin_independent_of_mission_margin(self):
        route = [{"latitude": 10.001, "longitude": 10.0}]  # ~111 m
        res = _evaluate(mission_route=route, rtl_home=(10.0045, 10.0),  # ~500 m
                        physical_battery_percent=80.0)
        self.assertNotEqual(res.mission_margin_percent, res.rtl_return_margin_percent)
        self.assertNotEqual(res.estimated_mission_capacity_Ah, res.estimated_rtl_capacity_Ah)

    # 19. mission infeasible but RTL feasible -> correct distinct result
    #     (the module's headline example: a mission that can no longer be
    #     completed may still have a perfectly safe way home right now). A
    #     ~5.6 km remaining route at 50% battery exceeds the available
    #     capacity net of reserve (margin ~ -9.75%); the RTL leg stays tiny.
    def test_mission_infeasible_rtl_feasible(self):
        route = [{"latitude": 10.05, "longitude": 10.0}]  # ~5560 m -> big mission cost
        res = _evaluate(mission_route=route, rtl_home=(10.0001, 10.0),
                        physical_battery_percent=50.0)
        self.assertFalse(res.mission_feasible)
        self.assertTrue(res.rtl_return_feasible)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)
        self.assertEqual(res.reason, mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)

    # 20. mission feasible + RTL infeasible -> correct distinct result and
    #     reason (the reverse of 19). With the RTL dimension's own smaller
    #     0.05 reserve, a ~5004 m RTL leg (the pre-split fixture distance) is
    #     now FEASIBLE at 50% battery (margin ~ +3.7%) -- exactly the fix's
    #     intended effect -- so this boundary now needs a longer ~8006 m RTL
    #     leg to exceed available capacity net of the smaller reserve
    #     (margin ~ -15.04%).
    def test_mission_feasible_rtl_infeasible(self):
        route = [{"latitude": 10.0001, "longitude": 10.0}]  # tiny
        res = _evaluate(mission_route=route, rtl_home=(10.072, 10.0),  # ~8006 m
                        physical_battery_percent=50.0)
        self.assertTrue(res.mission_feasible)
        self.assertFalse(res.rtl_return_feasible)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)
        self.assertEqual(res.reason, mf.REASON_INSUFFICIENT_ENERGY_FOR_RTL_RETURN)

    # 21. both feasible
    def test_both_feasible(self):
        route = [{"latitude": 10.0001, "longitude": 10.0}]
        res = _evaluate(mission_route=route, rtl_home=(10.0001, 10.0),
                        physical_battery_percent=80.0)
        self.assertTrue(res.mission_feasible)
        self.assertTrue(res.rtl_return_feasible)
        self.assertEqual(res.status, mf.STATUS_FEASIBLE)

    # 22. both mission and RTL infeasible -> correct result, deterministic
    #     mission-first reason priority (task section 11 case D). A 5%
    #     battery leaves only 1.6 Ah available, below the 6 Ah reserve alone,
    #     so both dimensions are infeasible regardless of the (short) routes.
    def test_both_infeasible_mission_reason_wins(self):
        route = [{"latitude": 10.01, "longitude": 10.0}]
        res = _evaluate(mission_route=route, rtl_home=(10.02, 10.0),
                        physical_battery_percent=5.0)
        self.assertFalse(res.mission_feasible)
        self.assertFalse(res.rtl_return_feasible)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)
        self.assertEqual(res.reason, mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)

    # 23. missing planned Home does NOT block mission feasibility -- this
    #     repo's planning-package contract never requires a route to end at
    #     Home (see test 11), so planned_home is pure provenance, not a
    #     mission-completion precondition. Mission is UNKNOWN only when the
    #     ROUTE itself is unavailable (test_no_mission_unknown... above).
    def test_missing_planned_home_does_not_block_mission(self):
        route = [{"latitude": 10.001, "longitude": 10.0}]
        res = _evaluate(mission_route=route, planned_home=None, rtl_home=(10.0, 10.0))
        self.assertTrue(res.mission_feasible)
        self.assertIsNone(res.planned_home)

    # 24. missing RTL Home -> RTL feasibility UNKNOWN (covered fully in
    #     test_no_rtl_home_unknown above; this proves the "unverified" case
    #     collapses to the SAME UNKNOWN outcome, which is why
    #     evaluate_from_snapshot gates rtl_home on home_valid rather than
    #     passing through whatever coordinates happen to be present).
    def test_rtl_home_none_and_missing_coords_both_unknown(self):
        res_none = _evaluate(rtl_home=None)
        res_partial = _evaluate(rtl_home=(None, None))
        self.assertIsNone(res_none.rtl_return_feasible)
        self.assertIsNone(res_partial.rtl_return_feasible)

    def test_result_is_json_serializable_dict(self):
        import json
        res = _evaluate()
        json.dumps(res.to_dict())  # must not raise

    # 25. route identity defaults to verified=True for pure-function callers
    #     that do not exercise the mission-route-identity-safety axis (see
    #     test_mission_feasibility_route_identity.py for the dedicated
    #     coverage of that invariant).
    def test_route_identity_defaults_verified_for_pure_function_callers(self):
        res = _evaluate()
        self.assertTrue(res.route_identity_verified)
        self.assertIsNone(res.route_identity_reason)

    # 26. the physical energy-model configuration is always echoed on the
    #     result for auditability (task section 12).
    def test_physical_model_config_echoed_on_result(self):
        res = _evaluate()
        self.assertEqual(res.nominal_capacity_Ah, NOMINAL_CAPACITY_AH)
        self.assertEqual(res.conservative_current_A, CONSERVATIVE_CURRENT_A)
        self.assertEqual(res.design_speed_mps, DESIGN_SPEED_MPS)
        self.assertEqual(res.usable_capacity_factor, USABLE_CAPACITY_FACTOR)
        self.assertEqual(res.mission_reserve_fraction, MISSION_RESERVE_FRACTION)
        self.assertEqual(res.rtl_reserve_fraction, RTL_RESERVE_FRACTION)
        self.assertIsNotNone(res.available_capacity_Ah)
        self.assertEqual(res.mission_reserve_capacity_Ah, NOMINAL_CAPACITY_AH * MISSION_RESERVE_FRACTION)
        self.assertEqual(res.rtl_reserve_capacity_Ah, NOMINAL_CAPACITY_AH * RTL_RESERVE_FRACTION)


class TestEvaluateFromSnapshot(unittest.TestCase):
    """Adapter wiring: pulls fields off a decision_snapshot-shaped object the
    same way both real call sites (local_agent.py's continuous loop,
    mission_execution_controller's Start gate) do, and proves the RTL
    dimension is gated on `home_valid` rather than blindly trusting whatever
    home_latitude/home_longitude the snapshot happens to carry (which CAN,
    inside decision_snapshot.build_snapshot, silently fall back to the
    planning-package Home -- exactly the conflation this module must not
    reintroduce)."""

    class _FakeSnapshot:
        latitude = 10.0
        longitude = 10.0
        position_age_s = 1.0
        current_sequence = 1
        home_latitude = 10.00045   # ~50 m away
        home_longitude = 10.0
        home_valid = True
        battery_percent = 80.0
        battery_voltage = 15.8
        battery_current = 0.17

    class _FakeCfg:
        nominal_capacity_Ah = NOMINAL_CAPACITY_AH
        conservative_current_A = CONSERVATIVE_CURRENT_A
        design_speed_mps = DESIGN_SPEED_MPS
        usable_capacity_factor = USABLE_CAPACITY_FACTOR
        mission_reserve_fraction = MISSION_RESERVE_FRACTION
        rtl_reserve_fraction = RTL_RESERVE_FRACTION
        max_position_age_s = 5.0

    def test_wires_snapshot_package_injection_cfg(self):
        package = {"route": [{"latitude": 10.001, "longitude": 10.0}],
                  "home": {"latitude": 10.5, "longitude": 10.0}}
        res = mf.evaluate_from_snapshot(self._FakeSnapshot(), package, None, self._FakeCfg())
        self.assertEqual(res.status, mf.STATUS_FEASIBLE)
        self.assertEqual(res.battery_source, mf.SOURCE_PHYSICAL)
        self.assertAlmostEqual(res.rtl_return_distance_m, 50.0, delta=1.0)
        self.assertEqual(res.rtl_return_geometry_source, mf.RTL_METHOD_STRAIGHT_LINE)

    def test_injection_flows_through_adapter(self):
        package = {"route": [{"latitude": 10.001, "longitude": 10.0}]}
        injection = {"battery_percent": 5.0}
        res = mf.evaluate_from_snapshot(self._FakeSnapshot(), package, injection, self._FakeCfg())
        self.assertEqual(res.battery_percent, 5.0)
        self.assertEqual(res.battery_source, mf.SOURCE_INJECTED)
        self.assertFalse(res.mission_feasible)
        self.assertEqual(res.reason, mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)

    def test_unverified_home_never_used_for_rtl(self):
        """home_valid=False (Pixhawk home_status present but not verified, OR
        decision_snapshot fell back to the planning-package Home because
        home_status carried no coordinates at all) -> RTL dimension is
        UNKNOWN, never silently computed against an unverified/fallback
        position."""
        class Unverified(self._FakeSnapshot):
            home_valid = False

        package = {"route": [{"latitude": 10.001, "longitude": 10.0}]}
        res = mf.evaluate_from_snapshot(Unverified(), package, None, self._FakeCfg())
        self.assertIsNone(res.rtl_return_feasible)
        self.assertIsNone(res.rtl_home)
        self.assertEqual(res.reason, mf.REASON_RTL_HOME_UNAVAILABLE)

    def test_planned_home_sourced_from_package_independent_of_pixhawk(self):
        package = {"route": [{"latitude": 10.001, "longitude": 10.0}],
                  "home": {"latitude": 12.0, "longitude": 13.0}}
        res = mf.evaluate_from_snapshot(self._FakeSnapshot(), package, None, self._FakeCfg())
        self.assertEqual(res.planned_home, {"latitude": 12.0, "longitude": 13.0,
                                            "source": mf.HOME_SOURCE_PLANNING_PACKAGE})
        self.assertEqual(res.rtl_home["source"], mf.HOME_SOURCE_PIXHAWK_VERIFIED_HOME)

    def test_measured_voltage_and_current_surfaced_as_diagnostics_only(self):
        """DecisionSnapshot's own battery_voltage/battery_current are surfaced
        as OBSERVED CURRENT STATE (task section 17) -- never as the energy-
        model's current input, which always uses the configured
        conservative_current_A regardless of what is measured live."""
        package = {"route": [{"latitude": 10.001, "longitude": 10.0}]}
        res = mf.evaluate_from_snapshot(self._FakeSnapshot(), package, None, self._FakeCfg())
        self.assertEqual(res.measured_voltage_V, 15.8)
        self.assertEqual(res.measured_current_A, 0.17)
        self.assertEqual(res.conservative_current_A, CONSERVATIVE_CURRENT_A)

    def test_no_mission_binding_defaults_route_identity_verified(self):
        """A caller that does not wire mission_binding at all (most of this
        test file, and every call site that predates the mission-route-
        identity-safety task) sees unchanged behaviour -- route_identity_
        verified defaults True, never silently blocking a caller that has no
        such evidence source."""
        package = {"route": [{"latitude": 10.001, "longitude": 10.0}]}
        res = mf.evaluate_from_snapshot(self._FakeSnapshot(), package, None, self._FakeCfg())
        self.assertTrue(res.route_identity_verified)


# ── evaluate_route_return_energy (task: revised-route energy feasibility
#    recheck) -- the ACTUAL constrained-route energy check replan_controller.py
#    runs after building/validating a RETRACE_APPROVED route and before
#    upload. Reuses the SAME _dimension_capacity/_available_capacity_Ah/
#    resolve_effective_battery machinery TestFeasibility above exercises via
#    evaluate_mission_feasibility -- no new formula, just a caller-supplied
#    distance instead of the straight-line RTL estimate. At the module's
#    documented b=50%, rtl_reserve_fraction=0.05 constants:
#        margin_percent = 0.8*50 - 0.00625*d - 5.0 = 35.0 - 0.00625*d
#    (matches TestFeasibility's rtl_return_margin_percent formula exactly,
#    since this function is that same dimension generalized over the
#    caller's own reserve_fraction choice, not a distinct model).
class TestEvaluateRouteReturnEnergy(unittest.TestCase):
    def _evaluate(self, **kwargs):
        base = dict(
            distance_m=1000.0,
            physical_battery_percent=50.0,
            injected_battery_percent=None,
            nominal_capacity_Ah=NOMINAL_CAPACITY_AH,
            conservative_current_A=CONSERVATIVE_CURRENT_A,
            design_speed_mps=DESIGN_SPEED_MPS,
            usable_capacity_factor=USABLE_CAPACITY_FACTOR,
            reserve_fraction=RTL_RESERVE_FRACTION,
        )
        base.update(kwargs)
        return mf.evaluate_route_return_energy(**base)

    def test_feasible_positive_margin(self):
        res = self._evaluate(distance_m=1000.0)   # margin = 35.0 - 6.25 = 28.75%
        self.assertEqual(res.status, mf.STATUS_FEASIBLE)
        self.assertTrue(res.feasible)
        self.assertAlmostEqual(res.margin_percent, 28.75, places=2)
        self.assertEqual(res.distance_m, 1000.0)

    def test_infeasible_negative_margin(self):
        res = self._evaluate(distance_m=6000.0)   # margin = 35.0 - 37.5 = -2.5%
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)
        self.assertFalse(res.feasible)
        self.assertLess(res.margin_percent, 0)
        self.assertEqual(res.reason, mf.REASON_INSUFFICIENT_ENERGY_FOR_RTL_RETURN)

    def test_zero_margin_counts_as_infeasible(self):
        # margin = 0 exactly at d = 35.0/0.00625 = 5600 m -- "<=0 is not a
        # margin", the same boundary rule as every other dimension in this
        # module (see module docstring's "Boundary rule" section).
        res = self._evaluate(distance_m=5600.0)
        self.assertEqual(res.status, mf.STATUS_INFEASIBLE)
        self.assertFalse(res.feasible)
        self.assertEqual(res.margin_percent, 0.0)

    def test_missing_battery_is_unknown_never_feasible(self):
        res = self._evaluate(physical_battery_percent=None, injected_battery_percent=None)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertIsNone(res.feasible)
        self.assertEqual(res.reason, mf.REASON_BATTERY_INVALID)
        self.assertIsNone(res.margin_percent)

    def test_missing_distance_is_unknown(self):
        res = self._evaluate(distance_m=None)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertIsNone(res.feasible)
        self.assertEqual(res.reason, mf.REASON_ROUTE_DISTANCE_UNAVAILABLE)
        self.assertIsNone(res.margin_percent)

    def test_injected_battery_overrides_physical(self):
        res = self._evaluate(physical_battery_percent=90.0, injected_battery_percent=5.0)
        self.assertEqual(res.battery_percent, 5.0)
        self.assertEqual(res.battery_source, mf.SOURCE_INJECTED)

    def test_uses_caller_supplied_reserve_fraction_not_a_fixed_one(self):
        # The caller controls WHICH reserve is applied (replan_controller.py
        # always passes rtl_reserve_fraction for a RETURN-HOME action, never
        # mission_reserve_fraction) -- this function itself has no opinion, so
        # a different reserve_fraction genuinely changes the margin.
        rtl = self._evaluate(distance_m=1000.0, reserve_fraction=RTL_RESERVE_FRACTION)
        mission = self._evaluate(distance_m=1000.0, reserve_fraction=MISSION_RESERVE_FRACTION)
        self.assertGreater(rtl.margin_percent, mission.margin_percent)
        self.assertAlmostEqual(rtl.reserve_capacity_Ah, 2.0, places=4)
        self.assertAlmostEqual(mission.reserve_capacity_Ah, 6.0, places=4)

    def test_required_and_available_capacity_evidence_present(self):
        res = self._evaluate(distance_m=1000.0)
        self.assertAlmostEqual(res.available_capacity_Ah, 16.0, places=4)   # 40*0.8*50/100
        self.assertAlmostEqual(res.required_capacity_Ah, 2.5, places=4)     # 9.0*(1000/1/3600)
        self.assertAlmostEqual(res.reserve_capacity_Ah, 2.0, places=4)      # 40*0.05
        self.assertIsNotNone(res.duration_h)

    def test_matches_rtl_dimension_of_evaluate_mission_feasibility(self):
        # Same distance/battery/reserve fed through evaluate_mission_feasibility's
        # own RTL dimension (via rtl_return_distance_m, the caller-supplied-
        # path-length hook that module docstring already documents) must land
        # on the identical margin -- proving this is the SAME model, not a
        # second, slightly different one.
        full = mf.evaluate_mission_feasibility(
            current_position=(10.0, 10.0), position_age_s=1.0,
            mission_route=None, current_sequence=None,
            rtl_home=(10.0, 10.0),
            physical_battery_percent=50.0,
            rtl_return_distance_m=1000.0, rtl_return_geometry_source="TEST",
            nominal_capacity_Ah=NOMINAL_CAPACITY_AH,
            conservative_current_A=CONSERVATIVE_CURRENT_A,
            design_speed_mps=DESIGN_SPEED_MPS,
            usable_capacity_factor=USABLE_CAPACITY_FACTOR,
            mission_reserve_fraction=MISSION_RESERVE_FRACTION,
            rtl_reserve_fraction=RTL_RESERVE_FRACTION,
            max_position_age_s=5.0, now=1000.0,
        )
        direct = self._evaluate(distance_m=1000.0)
        self.assertEqual(full.rtl_return_margin_percent, direct.margin_percent)
        self.assertEqual(full.rtl_return_feasible, direct.feasible)


if __name__ == "__main__":
    unittest.main()
