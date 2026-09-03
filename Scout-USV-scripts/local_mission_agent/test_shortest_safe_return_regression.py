"""
Regression fixture: shortest-safe-return vs. the pre-existing RETRACE_APPROVED
strategy, over the ACTUAL geometry and trigger position from the real wet E2
run:

    run_id:      run-20260817-142455-usv-2-65f5b086
    mission_id:  msn-7716c5cc7401

Loaded directly from that run's captured evidence
(experiment_runs/run-20260817-142455-usv-2-65f5b086/, gitignored bench
output -- see .gitignore's `experiment_runs/` entry, so this is NOT a
committed test dependency and every test here skips cleanly when that
directory isn't present in the current checkout, e.g. in CI or on a machine
that never ran this bench trial):

  - planning_package.json  -> the approved route (33 wp), navigable_boundary
                              (5 vertices), home_corridor (68 vertices),
                              no_go_zones (1 zone), no_go_clearance_m (0.0)
  - decision_snapshots.jsonl (snapshot_id from revised_mission_r6.json's
    decision_snapshot_id) -> the exact trigger position and current_waypoint
    (24) SAFE_RETURN_HOME planned from
  - revised_mission_r6.json -> Home's runtime-VERIFIED position (synced at
    Start -- see timeline.jsonl's SYNCHRONIZING_PACKAGE event -- distinct
    from the pre-sync planning_package.json home field) and the live run's
    own OLD-algorithm result (26 waypoints / 476.8 m / FEASIBLE at 1.62%
    margin), used here as an authenticity check: this fixture's own
    from-scratch _build_retrace_approved_route call must reproduce that
    476.8 m / 26-point result exactly, proving the loaded geometry/position
    really is the live run's, before trusting the NEW route's comparison
    against it.

No expected percentage is hard-coded in advance -- both routes are actually
computed here, over the real geometry, and compared.

    python3 test_shortest_safe_return_regression.py
"""
import json
import os
import types
import unittest

import geo
import mission_feasibility as mf
import planning_package as pp
import replan_config
import safe_return_planner as srp

_HERE = os.path.dirname(os.path.abspath(__file__))
_RUN_DIR = os.path.join(_HERE, "experiment_runs", "run-20260817-142455-usv-2-65f5b086")
_RUN_AVAILABLE = os.path.isdir(_RUN_DIR)

_CFG = replan_config.ReplanConfig()

# The live run's own reported OLD-algorithm result (summary.json /
# revised_mission_r6.json's validation_result.checks) -- the authenticity
# target this fixture's own recomputation must match.
_LIVE_OLD_WAYPOINT_COUNT = 26
_LIVE_OLD_DISTANCE_M = 476.8
_LIVE_OLD_PRESERVED = 24
_LIVE_OLD_REMOVED = 9
_LIVE_ORIGINAL_WAYPOINT_COUNT = 33
_LIVE_CURRENT_WAYPOINT = 24
# Runtime-verified Home (post Start-sync -- see module docstring), read off
# revised_mission_r6.json's validation_result.geometry_validation.runtime_home
# / feasibility.rtl_home, NOT the pre-sync planning_package.json home field.
_LIVE_RUNTIME_HOME = {"latitude": 56.6503745, "longitude": 12.8709873}
# Trigger position, decision_snapshots.jsonl entry
# (snapshot_id == revised_mission_r6.json's decision_snapshot_id
# "25b09b2a668546039ee7a258082b333a") .position.
_LIVE_TRIGGER_POSITION = (56.6507062, 12.8705259)


def _load_real_package():
    with open(os.path.join(_RUN_DIR, "planning_package.json")) as f:
        raw = json.load(f)
    return pp.build_package(
        raw["mission_id"], raw["route"], _LIVE_RUNTIME_HOME,
        navigable_boundary=raw["navigable_boundary"],
        home_corridor=raw["home_corridor"],
        no_go_zones=raw["no_go_zones"],
        no_go_clearance_m=raw["no_go_clearance_m"],
    )


def _real_snapshot():
    return types.SimpleNamespace(latitude=_LIVE_TRIGGER_POSITION[0],
                                 longitude=_LIVE_TRIGGER_POSITION[1],
                                 current_sequence=_LIVE_CURRENT_WAYPOINT)


@unittest.skipUnless(_RUN_AVAILABLE,
                     f"real wet-run evidence not present in this checkout ({_RUN_DIR})")
class TestShortestSafeReturnRegressionRealRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = _load_real_package()
        cls.snapshot = _real_snapshot()
        cls.old = srp._build_retrace_approved_route(cls.snapshot, cls.package, _CFG)
        cls.old_validation = srp.validate_route(cls.old["route"], cls.package, cls.snapshot, _CFG)
        cls.old_distance_m = round(geo.path_length_m(
            [(wp["latitude"], wp["longitude"]) for wp in cls.old["route"]]), 1)
        cls.new = srp.build_safe_return_route(cls.snapshot, cls.package, _CFG)
        cls.new_validation = srp.validate_route(cls.new["route"], cls.package, cls.snapshot, _CFG)
        cls.new_distance_m = round(geo.path_length_m(
            [(wp["latitude"], wp["longitude"]) for wp in cls.new["route"]]), 1)

    # ── Authenticity: this fixture reproduces the live run's OWN result ─────
    def test_authenticity_reproduces_live_old_route_exactly(self):
        self.assertEqual(len(self.package["route"]), _LIVE_ORIGINAL_WAYPOINT_COUNT)
        self.assertTrue(self.old["ok"], self.old.get("reason"))
        self.assertEqual(self.old["preserved_waypoint_count"], _LIVE_OLD_PRESERVED)
        self.assertEqual(self.old["removed_waypoint_count"], _LIVE_OLD_REMOVED)
        self.assertEqual(len(self.old["route"]), _LIVE_OLD_WAYPOINT_COUNT)
        self.assertEqual(self.old_distance_m, _LIVE_OLD_DISTANCE_M)
        self.assertTrue(self.old_validation["valid"], self.old_validation.get("reason"))

    # ── The new route wins, is proven valid, terminates at the SAME verified
    #    Home the live run used ──────────────────────────────────────────────
    def test_new_route_valid_and_terminates_at_verified_home(self):
        self.assertTrue(self.new["ok"], self.new.get("reason"))
        self.assertTrue(self.new_validation["valid"], self.new_validation.get("reason"))
        self.assertAlmostEqual(self.new["route"][-1]["latitude"],
                               _LIVE_RUNTIME_HOME["latitude"], places=6)
        self.assertAlmostEqual(self.new["route"][-1]["longitude"],
                               _LIVE_RUNTIME_HOME["longitude"], places=6)
        self.assertAlmostEqual(self.new["route"][0]["latitude"],
                               _LIVE_TRIGGER_POSITION[0], places=6)
        self.assertAlmostEqual(self.new["route"][0]["longitude"],
                               _LIVE_TRIGGER_POSITION[1], places=6)

    # ── Every optimized segment independently passes authoritative geometry ──
    def test_every_new_route_segment_passes_authoritative_geometry(self):
        latlon = [(wp["latitude"], wp["longitude"]) for wp in self.new["route"]]
        boundary = srp._boundary_latlon(self.package)
        corridor = srp._home_corridor_latlon(self.package)
        zones = srp._no_go_zones_latlon(self.package)
        clearance = srp._no_go_clearance_m(self.package)
        for i in range(len(latlon) - 1):
            self.assertTrue(
                srp._segment_geometrically_valid(latlon[i], latlon[i + 1], boundary, corridor, zones, clearance),
                f"segment {i} ({latlon[i]} -> {latlon[i+1]}) must pass the SAME authoritative check "
                "validate_route uses")
        self.assertIsNone(geo.route_crosses_no_go(latlon, zones))

    # ── The actual, measured improvement over the real geometry ─────────────
    def test_new_route_is_substantially_shorter(self):
        old_count, new_count = len(self.old["route"]), len(self.new["route"])
        old_m, new_m = self.old_distance_m, self.new_distance_m
        reduction_m = round(old_m - new_m, 1)
        reduction_pct = round(reduction_m / old_m * 100, 1)
        msg = (f"old: {old_count} waypoints / {old_m} m; new: {new_count} waypoints / {new_m} m; "
              f"reduction: {reduction_m} m ({reduction_pct}%)")
        self.assertLess(new_m, old_m, msg)
        self.assertLessEqual(new_count, old_count, msg)
        self.assertGreater(reduction_pct, 25.0, msg)

    # ── Energy result improves using the SAME existing model/reserves, at the
    #    SAME battery percent the live run actually triggered at (12%) ───────
    def test_energy_feasibility_improves_at_the_live_trigger_battery(self):
        battery_percent = 12.0
        old_energy = mf.evaluate_route_return_energy(
            distance_m=self.old_distance_m, physical_battery_percent=battery_percent,
            injected_battery_percent=battery_percent,
            nominal_capacity_Ah=_CFG.nominal_capacity_Ah, conservative_current_A=_CFG.conservative_current_A,
            design_speed_mps=_CFG.design_speed_mps, usable_capacity_factor=_CFG.usable_capacity_factor,
            reserve_fraction=_CFG.rtl_reserve_fraction)
        new_energy = mf.evaluate_route_return_energy(
            distance_m=self.new_distance_m, physical_battery_percent=battery_percent,
            injected_battery_percent=battery_percent,
            nominal_capacity_Ah=_CFG.nominal_capacity_Ah, conservative_current_A=_CFG.conservative_current_A,
            design_speed_mps=_CFG.design_speed_mps, usable_capacity_factor=_CFG.usable_capacity_factor,
            reserve_fraction=_CFG.rtl_reserve_fraction)
        # Reproduces the live run's own reported OLD energy result exactly
        # (checks.revised_route_required_ah=1.192, margin_percent=1.62).
        self.assertEqual(old_energy.status, "FEASIBLE")
        self.assertAlmostEqual(old_energy.required_capacity_Ah, 1.192, places=2)
        self.assertAlmostEqual(old_energy.margin_percent, 1.62, places=1)
        self.assertEqual(new_energy.status, "FEASIBLE")
        self.assertLess(new_energy.required_capacity_Ah, old_energy.required_capacity_Ah)
        self.assertGreater(new_energy.margin_percent, old_energy.margin_percent)

    # ── Bounded, deterministic computation (task: PERFORMANCE) ───────────────
    def test_planner_runtime_bounded(self):
        # The real corridor carries 68 vertices -- the largest polygon in any
        # fixture in this test suite -- still must stay a small fraction of
        # the live run's own trigger-to-revised-AUTO budget (9.46 s).
        self.assertLess(self.new["planner_runtime_s"], 2.0)

    def test_planner_evidence_labels_the_winning_strategy(self):
        self.assertIn(self.new["method"], (srp.METHOD_SHORTEST, srp.METHOD_RETRACE_FALLBACK))
        self.assertIn(self.new["direct_path_valid"], (True, False))
        self.assertIsInstance(self.new["candidate_node_count"], int)
        self.assertGreater(self.new["candidate_node_count"], 0)
        self.assertIsInstance(self.new["fallback_used"], bool)

    def test_original_mission_route_remains_immutable(self):
        before = [dict(wp) for wp in self.package["route"]]
        srp.build_safe_return_route(self.snapshot, self.package, _CFG)
        self.assertEqual(self.package["route"], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
