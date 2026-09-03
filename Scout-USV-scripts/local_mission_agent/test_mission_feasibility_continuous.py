"""
Continuous mission-energy-feasibility evaluation (task section 5/15): proves
the SAME evaluation local_agent.py's main loop runs every iteration
(decision_snapshot.build_snapshot -> mission_feasibility.evaluate_from_snapshot,
exactly as wired in local_agent.py) produces an updated, correct result as a
mission progresses -- current sequence advancing shrinks the remaining route
(only the remaining waypoints contribute, never the whole original route
again), and a falling effective battery is reflected in the margin -- without
requiring Start to have been pressed first, and without issuing any vehicle
action.

    python3 test_mission_feasibility_continuous.py
"""
import os
import tempfile
import unittest

import decision_snapshot as dsm
import mission_execution_controller as mec
import mission_feasibility as mf
import planning_package as pp
import replan_config

_HOME = {"latitude": 56.6490, "longitude": 12.8700}
# Three waypoints, ~111 m apart (0.001 deg latitude), starting right at Home.
_ROUTE = [
    {"latitude": 56.6490, "longitude": 12.8700, "loiter_time_s": 0},
    {"latitude": 56.6500, "longitude": 12.8700, "loiter_time_s": 0},
    {"latitude": 56.6510, "longitude": 12.8700, "loiter_time_s": 0},
]


def _vehicle_state(lat, lon, seq, battery, age=0.5):
    return {
        "usv_id": "usv-2",
        "telemetry": {"lat": lat, "lng": lon, "battery": battery,
                      "mode_name": "AUTO", "armed": True},
        "mavlink": {"heartbeat_age_s": 0.3, "last_message_age_s": age},
        "mission": {"current_mission_id": "m1", "mission_active": True,
                    "current_waypoint": seq, "mission_count": len(_ROUTE) + 1},
        "agent": {"control_authority": "LOCAL_AGENT",
                  "home_status": {"verified": True, "ready_for_auto": True,
                                  "home_position": dict(_HOME)}},
    }


class TestContinuousFeasibility(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        pkg = pp.build_package("m1", _ROUTE, _HOME, usv_id="usv-2")
        pp.save_package(pkg)
        self.package = pp.load()
        self.cfg = replan_config.ReplanConfig(usable_range_m=3000.0, reserve_margin_percent=10.0)

    def tearDown(self):
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))

    def _evaluate(self, lat, lon, seq, battery, injection=None):
        vs = _vehicle_state(lat, lon, seq, battery)
        snap = dsm.build_snapshot(vs, "CONNECTED", "LOCAL_AGENT", planning_package=self.package)
        return mf.evaluate_from_snapshot(snap, self.package, injection, self.cfg)

    def test_result_updates_as_mission_progresses_and_battery_falls(self):
        # ── At Start: healthy battery, full route ahead, comfortably feasible. ──
        r1 = self._evaluate(56.6490, 12.8700, seq=1, battery=60.0)
        self.assertEqual(r1.status, mf.STATUS_FEASIBLE)
        self.assertTrue(r1.mission_feasible)
        self.assertGreater(r1.mission_margin_percent, 0)
        self.assertEqual(r1.remaining_waypoint_count, 3)
        first_remaining = r1.planned_completion_distance_m

        # ── Later: sequence has advanced (only the LAST waypoint remains) and
        #    effective battery has fallen. Remaining distance must shrink --
        #    proving only the remaining route contributes, not the original
        #    route recomputed from scratch. ──
        r2 = self._evaluate(56.6505, 12.8700, seq=3, battery=25.0)
        self.assertEqual(r2.remaining_waypoint_count, 1)
        self.assertLess(r2.planned_completion_distance_m, first_remaining)
        self.assertEqual(r2.battery_percent, 25.0)
        # The result is genuinely recomputed from the new evidence, not
        # cached/stale (evaluated_at is not asserted distinct here -- two
        # calls in the same test can legitimately land in the same
        # millisecond; the recomputed distance/margin above already proves
        # this is a fresh calculation, not a cached one).
        self.assertNotEqual(r1.mission_margin_percent, r2.mission_margin_percent)

    def test_rtl_distance_independent_of_mission_progress(self):
        """Home semantics correction acceptance case (task section 13):
        Scout sitting exactly at its OWN verified RTL Home (the fixture's
        home_status always reports _HOME) shows a near-zero, strongly-
        feasible RTL return. As the mission progresses and the vehicle moves
        away from Home, RTL distance grows to reflect the ACTUAL current
        position -- it is never derived from, or contaminated by, the
        planned-mission route geometry driving the (independently changing)
        mission distance."""
        r1 = self._evaluate(56.6490, 12.8700, seq=1, battery=90.0)
        self.assertEqual(r1.rtl_return_distance_m, 0.0)
        self.assertTrue(r1.rtl_return_feasible)
        self.assertEqual(r1.rtl_return_geometry_source, mf.RTL_METHOD_STRAIGHT_LINE)

        # Vehicle has physically moved away from Home while the mission
        # progressed -- RTL distance now reflects that real displacement
        # (straight line from the CURRENT position to Home), and the mission
        # distance has independently changed too (fewer waypoints remain).
        r2 = self._evaluate(56.6505, 12.8700, seq=3, battery=90.0)
        self.assertGreater(r2.rtl_return_distance_m, 0.0)
        self.assertNotEqual(r1.planned_completion_distance_m, r2.planned_completion_distance_m)
        self.assertNotEqual(r1.rtl_return_distance_m, r2.rtl_return_distance_m)

    def test_injection_affects_every_iteration_not_just_start(self):
        healthy = self._evaluate(56.6490, 12.8700, seq=1, battery=60.0)
        self.assertTrue(healthy.mission_feasible)

        injected = self._evaluate(56.6490, 12.8700, seq=1, battery=60.0,
                                  injection={"battery_percent": 5.0})
        self.assertEqual(injected.battery_percent, 5.0)
        self.assertEqual(injected.battery_source, mf.SOURCE_INJECTED)
        self.assertFalse(injected.mission_feasible)

    def test_wired_into_controller_readiness_across_iterations(self):
        """The exact local_agent.py wiring: each iteration's result is pushed
        into the controller via update_energy_feasibility(), independent of
        the controller's own FSM state, entirely pre-Start, with zero vehicle
        actions taken to produce any of this evidence. (can_start/
        start_eligible's dependence on this cached value once the mission is
        otherwise READY is covered by
        test_mission_execution_controller_feasibility_gate.py, which drives a
        real FakeGateway through NOT_READY -> READY.)"""
        ctrl = mec.MissionExecutionController(gateway=None)  # never called below
        snap1 = dsm.build_snapshot(_vehicle_state(56.6490, 12.8700, 1, 60.0),
                                   "CONNECTED", "LOCAL_AGENT", planning_package=self.package)
        r1 = mf.evaluate_from_snapshot(snap1, self.package, None, self.cfg)
        ctrl.update_energy_feasibility(r1.to_dict())
        self.assertEqual(ctrl.status()["energy_feasibility"]["status"], mf.STATUS_FEASIBLE)

        # Battery drops to an injected 5% -- feasibility flips to INFEASIBLE
        # automatically on the very next evaluation, before Start is ever
        # requested (Bench Test 2's "readiness should change automatically").
        snap2 = dsm.build_snapshot(_vehicle_state(56.6490, 12.8700, 1, 60.0),
                                   "CONNECTED", "LOCAL_AGENT", planning_package=self.package)
        r2 = mf.evaluate_from_snapshot(snap2, self.package, {"battery_percent": 5.0}, self.cfg)
        ctrl.update_energy_feasibility(r2.to_dict())
        st = ctrl.status()
        self.assertEqual(st["energy_feasibility"]["status"], mf.STATUS_INFEASIBLE)
        self.assertEqual(st["energy_feasibility"]["reason"], mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)

        # Clearing the injection recovers readiness on the next evaluation.
        snap3 = dsm.build_snapshot(_vehicle_state(56.6490, 12.8700, 1, 60.0),
                                   "CONNECTED", "LOCAL_AGENT", planning_package=self.package)
        r3 = mf.evaluate_from_snapshot(snap3, self.package, None, self.cfg)
        ctrl.update_energy_feasibility(r3.to_dict())
        self.assertEqual(ctrl.status()["energy_feasibility"]["status"], mf.STATUS_FEASIBLE)


if __name__ == "__main__":
    unittest.main()
