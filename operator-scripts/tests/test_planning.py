"""Backend tests for operator-side survey planning (planning.py + the /api/planning routes).

Run from operator-scripts/:  python -m unittest tests.test_planning

What is pinned here, mapped to the task's required cases:
  * a valid survey polygon generates a segmented route (TestGeneration);
  * the shoreline clearance shrinks the navigable area and keeps coverage off the shore;
  * one or more no-go zones are excluded from the coverage route;
  * lane spacing changes route density; the primary angle changes orientation;
  * dual pass creates BOTH passes and preserves the transition/return segments;
  * generation is deterministic (same inputs -> identical route), which is what lets the
    frontend detect an "outdated" route by input-revision change;
  * invalid geometry is rejected, the waypoint limit warns, and validation catches a route
    that crosses a no-go interior (TestValidation);
  * a generated route hashes with mission_contract and uploads through the EXISTING
    POST /api/commands MISSION_UPLOAD path — no second mission framework (TestUploadPath).

The heavy geometry stack (shapely/pyproj/numpy) is optional; every geometry test is skipped
with a clear reason when it is not installed, exactly as the endpoints degrade at runtime.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import mission_contract  # noqa: E402
import planning  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2

# A ~265 m x 165 m rectangular lake near the project home (GeoJSON [lng, lat]).
BOUNDARY = [[13.000, 56.699], [13.004, 56.699], [13.004, 56.7005], [13.000, 56.7005]]
# A small no-go zone inside it.
ZONE = [[13.0018, 56.6996], [13.0022, 56.6996], [13.0022, 56.6999], [13.0018, 56.6999]]

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")


def base_inputs(**over):
    inp = {"boundary": BOUNDARY, "shoreline_clearance_m": 10, "lane_spacing_m": 25,
           "primary_angle_deg": 90, "dual_pass": False, "survey_speed_mps": 1.5}
    inp.update(over)
    return inp


@requires_geometry
class TestGeneration(unittest.TestCase):
    def test_valid_polygon_generates_segmented_route(self):
        r = planning.generate_survey(base_inputs(), max_route_waypoints=200)
        self.assertTrue(r["ok"])
        self.assertGreater(r["metrics"]["waypoint_count"], 2)
        kinds = [s["kind"] for s in r["segments"]]
        self.assertIn("primary", kinds)
        # every route waypoint is route-only (latitude/longitude/loiter_time_s)
        for wp in r["route_waypoints"]:
            self.assertEqual(set(wp), {"latitude", "longitude", "loiter_time_s"})

    def test_shoreline_clearance_shrinks_navigable_area(self):
        small = planning.generate_survey(base_inputs(shoreline_clearance_m=5), max_route_waypoints=200)
        big = planning.generate_survey(base_inputs(shoreline_clearance_m=25), max_route_waypoints=200)
        self.assertLess(big["metrics"]["navigable_area_m2"], small["metrics"]["navigable_area_m2"])
        self.assertLess(small["metrics"]["navigable_area_m2"], small["metrics"]["boundary_area_m2"])

    def test_no_go_zone_excluded_from_coverage(self):
        # The ported avoidance routes ALONG a no-go boundary (correct), so a waypoint may sit
        # on the edge; what must never happen is a waypoint in the STRICT interior. shapely's
        # Polygon.contains excludes the boundary, so it is the right interior test.
        from shapely.geometry import Polygon, Point
        r = planning.generate_survey(base_inputs(no_go_zones=[ZONE]), max_route_waypoints=200)
        zpoly = Polygon([(c[0], c[1]) for c in ZONE])
        for wp in r["route_waypoints"]:
            self.assertFalse(zpoly.contains(Point(wp["longitude"], wp["latitude"])),
                             "a coverage waypoint fell in the no-go zone interior")

    def test_lane_spacing_changes_density(self):
        sparse = planning.generate_survey(base_inputs(lane_spacing_m=50), max_route_waypoints=500)
        dense = planning.generate_survey(base_inputs(lane_spacing_m=10), max_route_waypoints=500)
        self.assertGreater(dense["metrics"]["waypoint_count"], sparse["metrics"]["waypoint_count"])

    def test_primary_angle_changes_route(self):
        a0 = planning.generate_survey(base_inputs(primary_angle_deg=0), max_route_waypoints=500)
        a90 = planning.generate_survey(base_inputs(primary_angle_deg=90), max_route_waypoints=500)
        pts0 = [(w["latitude"], w["longitude"]) for w in a0["route_waypoints"]]
        pts90 = [(w["latitude"], w["longitude"]) for w in a90["route_waypoints"]]
        self.assertNotEqual(pts0, pts90, "changing the survey angle must change the route")

    def test_dual_pass_creates_both_passes_and_segments(self):
        r = planning.generate_survey(
            base_inputs(dual_pass=True, home=[12.9995, 56.6985],
                        approach_waypoints=[[12.9998, 56.6988], [12.99995, 56.69885]],
                        return_waypoints=[[12.9997, 56.6987], [12.9996, 56.6986]]),
            max_route_waypoints=500)
        kinds = [s["kind"] for s in r["segments"]]
        for k in ("start_connector", "approach", "survey_entry_connector", "primary",
                  "pass_transition", "secondary", "return_connector", "return_approach",
                  "final_home_connector"):
            self.assertIn(k, kinds, f"dual-pass route missing the {k} segment")
        self.assertIsNotNone(r["metrics"]["secondary_angle_deg"])

    def test_generation_is_deterministic(self):
        a = planning.generate_survey(base_inputs(), max_route_waypoints=200)["route_waypoints"]
        b = planning.generate_survey(base_inputs(), max_route_waypoints=200)["route_waypoints"]
        self.assertEqual(a, b, "same inputs must produce the identical route (revision stability)")

    def test_waypoint_limit_warns(self):
        r = planning.generate_survey(base_inputs(lane_spacing_m=10), max_route_waypoints=5)
        self.assertTrue(any("limit" in w for w in r["warnings"]),
                        "an over-limit route must carry a waypoint-limit warning")

    def test_empty_after_clearance_is_rejected(self):
        # A clearance larger than the lake's half-width leaves no navigable area.
        with self.assertRaises(ValueError):
            planning.generate_survey(base_inputs(shoreline_clearance_m=500), max_route_waypoints=200)

    def test_invalid_boundary_is_rejected(self):
        with self.assertRaises(ValueError):
            planning.generate_survey(base_inputs(boundary=[[13.0, 56.7]]), max_route_waypoints=200)
        with self.assertRaises(ValueError):
            planning.generate_survey(base_inputs(lane_spacing_m=0), max_route_waypoints=200)


@requires_geometry
class TestValidation(unittest.TestCase):
    def test_generated_route_validates_ok(self):
        r = planning.generate_survey(base_inputs(no_go_zones=[ZONE], home=[12.9995, 56.6985]),
                                     max_route_waypoints=200)
        v = planning.validate_plan(
            {**base_inputs(no_go_zones=[ZONE], home=[12.9995, 56.6985]),
             "route_waypoints": r["route_waypoints"], "segments": r["segments"]},
            max_route_waypoints=200)
        self.assertTrue(v["ok"], v["errors"])
        self.assertTrue(v["checks"]["route_clears_no_go"])
        self.assertTrue(v["checks"]["coverage_within_navigable"])

    def test_route_crossing_no_go_interior_is_an_error(self):
        # A hand-made route straight through the zone must be rejected.
        route = [{"latitude": 56.69975, "longitude": 13.0020, "loiter_time_s": 0},
                 {"latitude": 56.69985, "longitude": 13.0020, "loiter_time_s": 0}]
        v = planning.validate_plan(
            {**base_inputs(no_go_zones=[ZONE]), "route_waypoints": route,
             "segments": [{"kind": "primary", "coordinates": [[13.0020, 56.69975], [13.0020, 56.69985]]}]},
            max_route_waypoints=200)
        self.assertFalse(v["ok"])
        self.assertTrue(any("no-go" in e for e in v["errors"]))

    def test_no_route_is_rejected(self):
        v = planning.validate_plan({**base_inputs(), "route_waypoints": []}, max_route_waypoints=200)
        self.assertFalse(v["ok"])

    def test_over_limit_route_is_an_error(self):
        route = [{"latitude": 56.699, "longitude": 13.0, "loiter_time_s": 0}] * 3
        v = planning.validate_plan({**base_inputs(), "route_waypoints": route}, max_route_waypoints=2)
        self.assertFalse(v["ok"])
        self.assertTrue(any("limit" in e for e in v["errors"]))


@requires_geometry
class TestEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_generate_endpoint_returns_limit_and_route(self):
        res = self.client.post("/api/planning/generate", json=base_inputs())
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["max_route_waypoints"], main.MAX_ROUTE_WAYPOINTS)
        self.assertIn("navigable_boundary", body)

    def test_generate_endpoint_400_on_bad_input(self):
        res = self.client.post("/api/planning/generate", json=base_inputs(lane_spacing_m=-1))
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.json()["ok"])

    def test_draft_crud_roundtrip(self):
        gen = self.client.post("/api/planning/generate", json=base_inputs()).json()
        created = self.client.post("/api/planning/drafts",
                                   json={"name": "Lake T", "vehicle_id": SCOUT_VID,
                                         "state": "ROUTE_GENERATED", "plan": gen})
        self.assertEqual(created.status_code, 200)
        did = created.json()["draft"]["id"]
        try:
            listed = self.client.get("/api/planning/drafts").json()["drafts"]
            self.assertTrue(any(d["id"] == did for d in listed))
            got = self.client.get(f"/api/planning/drafts/{did}")
            self.assertEqual(got.status_code, 200)
            upd = self.client.put(f"/api/planning/drafts/{did}", json={"name": "Lake T v2"})
            self.assertEqual(upd.json()["draft"]["name"], "Lake T v2")
        finally:
            self.assertEqual(self.client.delete(f"/api/planning/drafts/{did}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/planning/drafts/{did}").status_code, 404)

    def test_missing_draft_is_404(self):
        self.assertEqual(self.client.get("/api/planning/drafts/nope").status_code, 404)


@requires_geometry
class TestUploadPath(unittest.TestCase):
    """A generated route uploads through the SAME mission-contract path a pasted mission uses
    — one framework, one hash, one verification."""

    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"

    def test_generated_route_hashes_and_uploads_via_commands(self):
        gen = planning.generate_survey(base_inputs(), max_route_waypoints=200)
        route = gen["route_waypoints"]
        # The route hashes with the SAME calculator the contract uses (no second impl).
        digest = mission_contract.route_content_hash(route)
        self.assertTrue(digest.startswith("sha256:"))

        res = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD", "confirm": True,
            "params": {"contract_version": "mission-contract-v1", "waypoints": route}})
        self.assertEqual(res.status_code, 200)
        cmd = res.json()["command"]
        self.assertEqual(cmd["type"], "MISSION_UPLOAD")
        p = cmd["params"]
        self.assertEqual(p["expected_route_waypoint_count"], len(route))
        self.assertEqual(p["expected_pixhawk_item_count"], len(route) + 1)
        # The upload's hash equals the independently computed one — same bytes, same digest.
        self.assertEqual(p["expected_route_content_hash"], digest)

    def test_over_limit_generated_route_is_refused_by_upload(self):
        # Build a route above the contract limit and confirm the upload endpoint refuses it,
        # exactly as the frontend gate would (blocked before anything reaches the vehicle).
        route = [{"latitude": 56.699 + i * 1e-5, "longitude": 13.0, "loiter_time_s": 0}
                 for i in range(main.MAX_ROUTE_WAYPOINTS + 5)]
        res = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD", "confirm": True,
            "params": {"contract_version": "mission-contract-v1", "waypoints": route}})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "mission_contract_violation")


if __name__ == "__main__":
    unittest.main()
