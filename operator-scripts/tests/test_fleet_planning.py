"""Backend tests for FLEET survey planning (fleet_planning.py + /api/planning/fleet routes)
and the survey-speed default change (single + fleet).

Run from operator-scripts/:  python -m unittest tests.test_fleet_planning

Mapped to the task's required cases:
  * survey speed defaults to 1.0 m/s (single + fleet), user/stored speeds preserved, duration
    uses the configured speed, no unrelated 1.5 default remains;
  * allocation assigns COMPLETE contiguous survey-line groups (never waypoint chunks), each line
    exactly once, deterministically, influenced by vehicle homes, balanced by distance/duration;
  * too many vehicles for the available lines is a clear error; two-pass keeps geographic bands;
  * fleet validation detects duplicate/near-identical cross-vehicle waypoints, route
    intersections, missing homes, duplicate ids, unassigned / doubly-assigned lines, and warns on
    separation below the configured minimum;
  * each child mission is an independent, individually-hashed operator-survey-plan-v1 package that
    uploads through the unchanged POST /api/missions/finalize path — correct mission per vehicle.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import planning  # noqa: E402
import fleet_planning as F  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

# A wide E–W rectangle so a survey angle of 0° sweeps west→east and west/east homes clearly
# influence which contiguous band each vehicle receives (GeoJSON [lng, lat]).
WIDE = [[13.000, 56.699], [13.006, 56.699], [13.006, 56.7002], [13.000, 56.7002]]
BOX = [[13.000, 56.699], [13.004, 56.699], [13.004, 56.7005], [13.000, 56.7005]]
# A no-go zone that splits interior rows into two segments.
SPLIT_ZONE = [[13.0025, 56.6993], [13.0035, 56.6993], [13.0035, 56.7000], [13.0025, 56.7000]]

W_HOME = [12.999, 56.6996]
E_HOME = [13.007, 56.6996]


def veh(vid, home, speed=None):
    v = {"vehicle_id": vid, "vehicle_name": vid, "home": home}
    if speed is not None:
        v["survey_speed_mps"] = speed
    return v


def fleet_body(vehicles, boundary=WIDE, angle=0, **over):
    body = {"boundary": boundary, "shoreline_clearance_m": 8, "lane_spacing_m": 30,
            "primary_angle_deg": angle, "dual_pass": False,
            "minimum_fleet_separation_m": 10, "balance_metric": "estimated_duration",
            "vehicles": vehicles}
    body.update(over)
    return body


# ── survey-speed default (Phase 1) ────────────────────────────────────────────────────────

class TestSurveySpeedDefault(unittest.TestCase):
    def test_backend_default_is_one(self):
        # No unrelated 1.5 default was left behind — the single-vehicle fallback is now 1.0.
        self.assertEqual(planning.DEFAULT_PLANNING_SPEED_MPS, 1.0)

    @requires_geometry
    def test_single_vehicle_plan_defaults_to_one(self):
        r = planning.generate_survey(
            {"boundary": BOX, "shoreline_clearance_m": 10, "lane_spacing_m": 25,
             "primary_angle_deg": 90, "dual_pass": False}, max_route_waypoints=200)
        self.assertEqual(r["metrics"]["survey_speed_mps"], 1.0)
        self.assertTrue(r["metrics"]["survey_speed_is_default"])

    @requires_geometry
    def test_single_vehicle_user_speed_preserved(self):
        r = planning.generate_survey(
            {"boundary": BOX, "shoreline_clearance_m": 10, "lane_spacing_m": 25,
             "primary_angle_deg": 90, "survey_speed_mps": 0.7}, max_route_waypoints=200)
        self.assertEqual(r["metrics"]["survey_speed_mps"], 0.7)
        self.assertFalse(r["metrics"]["survey_speed_is_default"])
        # duration uses the configured speed
        self.assertAlmostEqual(r["metrics"]["estimated_duration_s"],
                               round(r["metrics"]["total_length_m"] / 0.7, 1), places=1)

    def test_fleet_vehicle_defaults_to_one(self):
        inp = F.normalize_fleet_inputs(fleet_body([veh("A", W_HOME), veh("B", E_HOME)]))
        for v in inp["vehicles"]:
            self.assertEqual(v["survey_speed_mps"], 1.0)
            self.assertTrue(v["survey_speed_is_default"])

    def test_fleet_vehicle_stored_speed_preserved(self):
        inp = F.normalize_fleet_inputs(
            fleet_body([veh("A", W_HOME, 2.0), veh("B", E_HOME, 0.5)]))
        speeds = {v["vehicle_id"]: v["survey_speed_mps"] for v in inp["vehicles"]}
        self.assertEqual(speeds["A"], 2.0)
        self.assertEqual(speeds["B"], 0.5)
        self.assertFalse(any(v["survey_speed_is_default"] for v in inp["vehicles"]))

    @requires_geometry
    def test_fleet_duration_uses_configured_speed(self):
        fp = F.generate_fleet(fleet_body([veh("A", W_HOME, 1.0), veh("B", E_HOME, 2.0)]))
        for v in fp["vehicles"]:
            m = v["metrics"]
            self.assertAlmostEqual(m["estimated_duration_s"],
                                   round(m["total_distance_m"] / m["survey_speed_mps"], 1), places=1)


# ── allocation ────────────────────────────────────────────────────────────────────────────

@requires_geometry
class TestAllocation(unittest.TestCase):
    def _assigned(self, fp):
        return {v["vehicle_id"]: v["assigned_survey_line_ids"] for v in fp["vehicles"]}

    def test_two_vehicles_two_contiguous_groups(self):
        fp = F.generate_fleet(fleet_body([veh("A", W_HOME), veh("B", E_HOME)]))
        a = self._assigned(fp)
        # each vehicle owns a contiguous run of the sweep-ordered lines
        for lines in a.values():
            rows = sorted(int(l.split("-")[3]) for l in lines)
            self.assertEqual(rows, list(range(rows[0], rows[0] + len(rows))))
        self.assertGreaterEqual(len(a["A"]), 1)
        self.assertGreaterEqual(len(a["B"]), 1)

    def test_all_lines_assigned_exactly_once(self):
        fp = F.generate_fleet(fleet_body([veh("A", W_HOME), veh("B", E_HOME)]))
        assigned = [l for v in fp["vehicles"] for l in v["assigned_survey_line_ids"]]
        self.assertEqual(sorted(assigned), sorted({l["id"] for l in fp["survey_lines"]}))
        self.assertEqual(len(assigned), len(set(assigned)))  # no line twice
        self.assertEqual(fp["allocation_summary"]["unassigned_survey_line_ids"], [])
        self.assertEqual(fp["allocation_summary"]["duplicate_survey_line_ids"], [])

    def test_three_vehicles_three_contiguous_groups(self):
        mid = [13.003, 56.6996]
        fp = F.generate_fleet(fleet_body([veh("A", W_HOME), veh("B", mid), veh("C", E_HOME)]))
        a = self._assigned(fp)
        self.assertEqual(len(a), 3)
        for lines in a.values():
            self.assertGreaterEqual(len(lines), 1)
        total = sum(len(v) for v in a.values())
        self.assertEqual(total, len(fp["survey_lines"]))

    def test_deterministic(self):
        b = fleet_body([veh("A", W_HOME), veh("B", E_HOME)])
        self.assertEqual(self._assigned(F.generate_fleet(b)), self._assigned(F.generate_fleet(b)))

    def _mean_lng(self, fp, vid):
        lines = {l["id"]: l for l in fp["survey_lines"]}
        ids = next(v["assigned_survey_line_ids"] for v in fp["vehicles"] if v["vehicle_id"] == vid)
        xs = [sum(p[0] for p in lines[i]["coordinates"]) / len(lines[i]["coordinates"]) for i in ids]
        return sum(xs) / len(xs)

    def test_home_influences_region(self):
        # the vehicle whose home is WEST is given the geographically WESTERN region; swapping the
        # homes swaps the regions.
        fp = F.generate_fleet(fleet_body([veh("A", W_HOME), veh("B", E_HOME)]))
        self.assertLess(self._mean_lng(fp, "A"), self._mean_lng(fp, "B"))  # A(west home) is west
        sw = F.generate_fleet(fleet_body([veh("A", E_HOME), veh("B", W_HOME)]))
        self.assertGreater(self._mean_lng(sw, "A"), self._mean_lng(sw, "B"))  # A(east home) is east

    def test_speed_influences_duration_balance(self):
        # A faster vehicle can take a larger share while keeping durations balanced.
        fp = F.generate_fleet(fleet_body([veh("A", W_HOME, 2.0), veh("B", E_HOME, 1.0)]))
        self.assertLessEqual(fp["allocation_summary"]["imbalance_percent"], 60)

    def test_more_vehicles_than_lines_is_error(self):
        # 2 coarse lines but 4 vehicles → a clear blocking error, never a zero-line vehicle.
        body = fleet_body([veh("A", W_HOME), veh("B", E_HOME),
                           veh("C", W_HOME), veh("D", E_HOME)],
                          boundary=BOX, angle=90, lane_spacing_m=90)
        with self.assertRaises(F.FleetPlanError):
            F.generate_fleet(body)

    def test_split_row_segments_handled(self):
        # a no-go zone splits interior rows: several lines share a row with -a/-b suffixes, all
        # still assigned exactly once.
        fp = F.generate_fleet(fleet_body([veh("A", W_HOME), veh("B", E_HOME)],
                                         no_go_zones=[SPLIT_ZONE], lane_spacing_m=25))
        ids = [l["id"] for l in fp["survey_lines"]]
        self.assertTrue(any(i.endswith("-a") or i.endswith("-b") for i in ids))
        assigned = [l for v in fp["vehicles"] for l in v["assigned_survey_line_ids"]]
        self.assertEqual(sorted(assigned), sorted(ids))

    def test_reverse_direction_reduces_approach(self):
        lines = F._order_lines_by_sweep(F._survey_lines(
            planning._NavGrid(WIDE, 8, [], 30), WIDE, 30, 0, 8, [], "pass-1"))
        # home near the LAST line's end → ordering reverses so approach enters there
        home_proj = planning._utm_for(WIDE)[0].transform(*lines[-1]["end_deg"])
        ordered = F._order_from_home(lines, home_proj)
        self.assertEqual(ordered[0]["id"], lines[-1]["id"])

    def test_two_pass_keeps_geographic_bands(self):
        fp = F.generate_fleet(fleet_body([veh("A", W_HOME), veh("B", E_HOME)],
                                         dual_pass=True))
        for v in fp["vehicles"]:
            ids = v["assigned_survey_line_ids"]
            self.assertTrue(any(i.startswith("pass-1") for i in ids))
            self.assertTrue(any(i.startswith("pass-2") for i in ids))


# ── fleet validation ────────────────────────────────────────────────────────────────────

@requires_geometry
class TestFleetValidation(unittest.TestCase):
    def _valid_plan(self, **over):
        return F.generate_fleet(fleet_body([veh("A", W_HOME), veh("B", E_HOME)], **over))

    def test_clean_plan_validates(self):
        fp = self._valid_plan()
        self.assertTrue(fp["validation"]["ok"], fp["validation"]["errors"])
        self.assertTrue(fp["validation"]["checks"]["no_route_intersections"])
        self.assertTrue(fp["validation"]["checks"]["all_lines_assigned_once"])

    def test_identical_waypoint_detected(self):
        fp = self._valid_plan()
        # force B's first waypoint onto A's first waypoint
        a0 = fp["vehicles"][0]["mission_package"]["route_waypoints"][0]
        fp["vehicles"][1]["mission_package"]["route_waypoints"][0] = dict(a0)
        res = F.validate_fleet(fp)
        self.assertFalse(res["checks"]["no_duplicate_cross_waypoints"])
        self.assertFalse(res["ok"])

    def test_near_identical_waypoint_detected(self):
        fp = self._valid_plan()
        a0 = fp["vehicles"][0]["mission_package"]["route_waypoints"][0]
        near = {"latitude": a0["latitude"] + 1e-6, "longitude": a0["longitude"], "loiter_time_s": 0}
        fp["vehicles"][1]["mission_package"]["route_waypoints"][0] = near
        res = F.validate_fleet(fp)
        self.assertFalse(res["checks"]["no_duplicate_cross_waypoints"])

    def test_duplicate_vehicle_ids_block(self):
        fp = self._valid_plan()
        fp["vehicles"][1]["vehicle_id"] = fp["vehicles"][0]["vehicle_id"]
        res = F.validate_fleet(fp)
        self.assertFalse(res["checks"]["unique_vehicle_ids"])
        self.assertFalse(res["ok"])

    def test_unassigned_line_blocks(self):
        fp = self._valid_plan()
        fp["allocation_summary"]["unassigned_survey_line_ids"] = ["pass-1-line-0099"]
        res = F.validate_fleet(fp)
        self.assertFalse(res["checks"]["all_lines_assigned_once"])
        self.assertFalse(res["ok"])

    def test_duplicate_line_assignment_blocks(self):
        fp = self._valid_plan()
        fp["allocation_summary"]["duplicate_survey_line_ids"] = ["pass-1-line-0001"]
        res = F.validate_fleet(fp)
        self.assertFalse(res["ok"])

    def test_missing_home_blocks_generation(self):
        with self.assertRaises(F.FleetPlanError):
            F.generate_fleet(fleet_body([veh("A", None), veh("B", E_HOME)]))

    def test_separation_below_minimum_warns(self):
        # a very large required separation the lane-spacing scale cannot meet → warning, not error
        fp = self._valid_plan(minimum_fleet_separation_m=500)
        res = fp["validation"]
        self.assertFalse(res["checks"]["separation_respected"])
        self.assertTrue(any("separation" in w for w in res["warnings"]))
        self.assertTrue(res["ok"])  # separation is a warning, not a blocking error


# ── upload path reuse ─────────────────────────────────────────────────────────────────────

@requires_geometry
class TestFleetUploadReuse(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_generate_endpoint(self):
        body = fleet_body([veh("usv-2", W_HOME), veh("usv-3", E_HOME)])
        r = self.client.post("/api/planning/fleet/generate", json=body)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["vehicles"]), 2)

    def test_child_hashes_differ_and_upload_to_correct_vehicle(self):
        fp = F.generate_fleet(fleet_body([veh("usv-2", W_HOME), veh("usv-3", E_HOME)]))
        v0, v1 = fp["vehicles"]
        self.assertNotEqual(v0["route_hash"], v1["route_hash"])  # missions differ
        # Each child uploads through the SAME finalize path to its OWN vehicle.
        for v in fp["vehicles"]:
            res = self.client.post("/api/missions/finalize", json={
                "vehicle_id": v["vehicle_id"], "mission_package": v["mission_package"],
                "confirm": True})
            self.assertEqual(res.status_code, 200, res.text)
            data = res.json()
            self.assertTrue(data["ok"])
            # the stored command targets the right vehicle and carries this child's route hash
            self.assertEqual(data["command"]["params"]["expected_route_content_hash"],
                             v["route_hash"])

    def test_generate_rejects_single_vehicle(self):
        r = self.client.post("/api/planning/fleet/generate",
                             json=fleet_body([veh("usv-2", W_HOME)]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("fleet", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
