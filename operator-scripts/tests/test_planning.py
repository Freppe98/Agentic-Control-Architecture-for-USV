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


# A U-shaped (concave) boundary with a notch bitten out of the top edge — the shape that
# reproduced the observed "connector leaves the polygon" failure. Horizontal scan lines get
# split by the notch, so the lane-to-lane connectors must be routed around it.
CONCAVE = [[13.000, 56.699], [13.004, 56.699], [13.004, 56.7005], [13.0025, 56.7005],
           [13.0025, 56.6997], [13.0015, 56.6997], [13.0015, 56.7005], [13.000, 56.7005]]


@requires_geometry
class TestSegmentIntegrity(unittest.TestCase):
    """Ordered-segment invariants (task PART 5 / PART 9 cases 1–4, 11, 13)."""

    def _dual(self):
        inp = base_inputs(dual_pass=True, home=[12.9995, 56.6985],
                          approach_waypoints=[[12.9998, 56.6988], [12.99995, 56.69885]],
                          return_waypoints=[[12.9997, 56.6987], [12.9996, 56.6986]],
                          no_go_zones=[ZONE])
        return inp, planning.generate_survey(inp, max_route_waypoints=500)

    def test_approach_points_in_exact_execution_order(self):
        inp, r = self._dual()
        order = [(round(o["longitude"], 6), round(o["latitude"], 6))
                 for o in r["original_execution_order"]]
        idxs = [order.index((round(p[0], 6), round(p[1], 6))) for p in inp["approach_waypoints"]]
        self.assertEqual(idxs, sorted(idxs), "approach WPs must appear in numbered order")

    def test_return_points_in_exact_execution_order(self):
        inp, r = self._dual()
        order = [(round(o["longitude"], 6), round(o["latitude"], 6))
                 for o in r["original_execution_order"]]
        idxs = [order.index((round(p[0], 6), round(p[1], 6))) for p in inp["return_waypoints"]]
        self.assertEqual(idxs, sorted(idxs), "return WPs must appear in numbered order")

    def test_segment_flattening_equals_route_waypoints(self):
        _, r = self._dual()
        flat, _order = planning._flatten_segments(
            [dict(s) for s in r["segments"]])  # re-flatten a copy
        flat_wp = planning._route_waypoints(planning._dedup(flat))
        self.assertEqual([(w["latitude"], w["longitude"]) for w in flat_wp],
                         [(w["latitude"], w["longitude"]) for w in r["route_waypoints"]])

    def test_no_invisible_jump_between_adjacent_segments(self):
        _, r = self._dual()
        segs = r["segments"]
        for a, b in zip(segs, segs[1:]):
            end, start = a["coordinates"][-1], b["coordinates"][0]
            self.assertTrue(planning._close(end, start),
                            f"invisible jump between {a['kind']} and {b['kind']}")

    def test_pass_transition_is_safe(self):
        from shapely.geometry import LineString
        from shapely.ops import transform
        inp, r = self._dual()
        grid = planning._NavGrid(inp["boundary"], inp["shoreline_clearance_m"],
                                 inp["no_go_zones"], step_m=inp["lane_spacing_m"])
        for s in r["segments"]:
            if s["kind"] == "pass_transition":
                lp = transform(grid.to_proj.transform,
                               LineString([(p[0], p[1]) for p in s["coordinates"]]))
                self.assertTrue(grid._seg_covered(lp), "pass transition left the navigable area")

    def test_no_go_zones_remain_in_finalized_package(self):
        inp, r = self._dual()
        pkg_zones = r["planning_inputs"]["no_go_zones"]
        self.assertEqual(len(pkg_zones), 1, "no-go zones dropped from package")
        # The package retains the zone (stored as a closed ring); every input vertex is kept.
        for pt in ZONE:
            self.assertIn(pt, pkg_zones[0])


@requires_geometry
class TestSafeConnector(unittest.TestCase):
    """The shared safe-connector (task PART 4 / PART 9 cases 5–9)."""

    def _grid(self, boundary=BOUNDARY, clearance=10, zones=None, step=15):
        return planning._NavGrid(boundary, clearance, zones or [], step_m=step)

    def test_direct_connector_accepted_when_fully_safe(self):
        g = self._grid()
        cx, cy = 13.002, 56.69975   # near the lake centre — a short interior segment
        path = g.safe_connector([cx, cy], [cx + 0.0002, cy], require_inside=True)
        self.assertEqual(len(path), 2, "a fully-safe segment is accepted as the direct line")

    def test_direct_connector_rejected_outside_concave_polygon(self):
        g = self._grid(boundary=CONCAVE, clearance=3, step=8)
        # A chord across the mouth of the U passes through the notch (outside navigable).
        self.assertFalse(g.segment_is_safe([13.0012, 56.7003], [13.0028, 56.7003],
                                           require_inside=True))

    def test_astar_connector_stays_inside_navigable(self):
        from shapely.geometry import Point
        from shapely.ops import transform
        g = self._grid(boundary=CONCAVE, clearance=3, step=8)
        path = g.safe_connector([13.0012, 56.7003], [13.0028, 56.7003], require_inside=True)
        self.assertGreater(len(path), 2, "an unsafe direct segment must be re-routed")
        # Every INTERIOR vertex of the routed path lies inside the navigable region.
        for lng, lat in path[1:-1]:
            p = transform(g.to_proj.transform, Point(lng, lat))
            self.assertTrue(g.navigable.buffer(planning.COVER_TOL_M).contains(p))

    def test_connector_avoids_no_go_zone(self):
        from shapely.geometry import LineString
        from shapely.ops import transform
        g = self._grid(zones=[ZONE], step=8)
        # Straight line from just west to just east of the zone would cross it.
        a, b = [13.0016, 56.69975], [13.0024, 56.69975]
        path = g.safe_connector(a, b, require_inside=True)
        lp = transform(g.to_proj.transform, LineString([(p[0], p[1]) for p in path]))
        self.assertTrue(g._seg_clears_nogo(lp), "connector crossed the no-go interior")

    def test_no_safe_connector_raises(self):
        # A no-go wall splits the lake; a connector across the two halves cannot be routed.
        WALL = [[13.0019, 56.6980], [13.0021, 56.6980], [13.0021, 56.7010], [13.0019, 56.7010]]
        g = self._grid(clearance=2, zones=[WALL], step=6)
        with self.assertRaises(planning.ConnectorError):
            g.safe_connector([13.0010, 56.69975], [13.0030, 56.69975], require_inside=True)


@requires_geometry
class TestConcaveRegression(unittest.TestCase):
    """Acceptance fixture (task PART 9): for a concave polygon EVERY generated coverage /
    inside segment is contained in the navigable geometry and outside no-go interiors."""

    def test_concave_polygon_produces_no_outside_connectors(self):
        from shapely.geometry import LineString
        from shapely.ops import transform
        inp = {"boundary": CONCAVE, "shoreline_clearance_m": 3, "lane_spacing_m": 12,
               "primary_angle_deg": 0, "home": [12.9995, 56.6985], "no_go_zones": [ZONE]}
        r = planning.generate_survey(inp, max_route_waypoints=800)
        grid = planning._NavGrid(CONCAVE, 3, [ZONE], step_m=12)
        for s in r["segments"]:
            if s["kind"] in planning._INSIDE_KINDS:
                lp = transform(grid.to_proj.transform,
                               LineString([(p[0], p[1]) for p in s["coordinates"]]))
                out = lp.difference(grid.navigable.buffer(planning.COVER_TOL_M))
                self.assertTrue(out.is_empty or out.length < 1.0,
                                f"{s['kind']} leaves the navigable region by {out.length:.1f} m")
                self.assertTrue(grid._seg_clears_nogo(lp),
                                f"{s['kind']} crosses a no-go interior")
        v = planning.validate_plan({**inp, "segments": r["segments"],
                                    "route_waypoints": r["route_waypoints"],
                                    "route_hash": r["route_hash"]}, max_route_waypoints=800)
        self.assertTrue(v["ok"], v["errors"])

    def test_disconnected_navigable_geometry_is_rejected(self):
        WALL = [[13.0019, 56.6980], [13.0021, 56.6980], [13.0021, 56.7010], [13.0019, 56.7010]]
        with self.assertRaises(planning.DisconnectedNavigableError):
            planning.generate_survey(
                {"boundary": BOUNDARY, "shoreline_clearance_m": 2, "lane_spacing_m": 15,
                 "primary_angle_deg": 0, "no_go_zones": [WALL]}, max_route_waypoints=500)


@requires_geometry
class TestMissionRecord(unittest.TestCase):
    """Immutable original mission record + finalize/verify lifecycle (task PART 6 / PART 9
    cases 14–17)."""

    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear(); main.commands_by_id.clear()
        main.original_missions.clear(); main.mission_id_by_command.clear()
        main.active_original_by_vehicle.clear()
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"

    def _finalize(self):
        pkg = planning.generate_survey(
            base_inputs(home=[12.9995, 56.6985], approach_waypoints=[[12.9998, 56.6988]]),
            max_route_waypoints=200)
        res = self.client.post("/api/missions/finalize",
                               json={"vehicle_id": SCOUT_VID, "mission_package": pkg, "confirm": True})
        return pkg, res

    def test_finalize_creates_immutable_original_revision_0(self):
        pkg, res = self._finalize()
        self.assertEqual(res.status_code, 200)
        rec = res.json()["mission"]
        self.assertEqual(rec["mission_revision"], 0)
        self.assertIsNone(rec["parent_revision_id"])
        self.assertTrue(rec["immutable"])
        self.assertEqual(rec["upload_status"], "QUEUED")
        self.assertTrue(rec["segments"] and rec["original_execution_order"])
        # read-only GETs
        mid = rec["mission_id"]
        self.assertEqual(self.client.get(f"/api/missions/original/{mid}").status_code, 200)
        active = self.client.get(f"/api/vehicles/{SCOUT_VID}/missions/active-original").json()
        self.assertEqual(active["mission"]["mission_id"], mid)

    def test_route_hash_matches_mission_contract_canonicalization(self):
        pkg, res = self._finalize()
        rec = res.json()["mission"]
        cmd = res.json()["command"]
        independent = mission_contract.route_content_hash(pkg["route_waypoints"])
        self.assertEqual(rec["route_hash"], independent)
        self.assertEqual(rec["route_hash"], cmd["params"]["expected_route_content_hash"])

    def test_verified_command_updates_stored_record(self):
        pkg, res = self._finalize()
        mid, cid = res.json()["mission"]["mission_id"], res.json()["command"]["id"]
        exp = res.json()["command"]["params"]
        self.client.post("/agent/command_result", json={
            "command_id": cid, "status": "EXECUTED", "result": {
                "accepted": True, "uploaded": True, "verified": True,
                "observed_route_waypoint_count": exp["expected_route_waypoint_count"],
                "observed_pixhawk_item_count": exp["expected_pixhawk_item_count"],
                "observed_route_content_hash": exp["expected_route_content_hash"]}})
        rec = main.original_missions[mid]
        self.assertEqual(rec["upload_status"], "VERIFIED")
        self.assertIsNotNone(rec["verified_at"])

    def test_failed_upload_preserves_record_with_failure_status(self):
        pkg, res = self._finalize()
        mid, cid = res.json()["mission"]["mission_id"], res.json()["command"]["id"]
        self.client.post("/agent/command_result", json={
            "command_id": cid, "status": "FAILED", "reason": "link lost mid-transfer"})
        rec = main.original_missions[mid]
        self.assertIn(mid, main.original_missions, "record preserved on failure")
        self.assertEqual(rec["upload_status"], "FAILED")
        self.assertTrue(rec["segments"], "plan geometry preserved on failure")


if __name__ == "__main__":
    unittest.main()
