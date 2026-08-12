"""The mission-geometry contract: one self-consistent geometry a Scout can safely replan from.

Run from operator-scripts/:  python -m unittest tests.test_mission_geometry   (no pytest)

WHY THIS FILE EXISTS — TWO LIVE E2 REPLANNING FAILURES
------------------------------------------------------
Run 1: the verified runtime Home ended up outside the mission's navigable boundary AND outside
the supplied `home_corridor`; constrained safe return could not be validated and the vehicle
fell back to native Pixhawk RTL.

Run 2: the mission PACKAGE was internally inconsistent — several approved route waypoints lay
outside the `navigable_boundary` shipped in the same package. RETRACE_APPROVED tried to reuse
those approved waypoints, safe-return validation correctly refused them, and RTL took over
again.

Both are the same root cause on the OPERATOR side: only the coverage passes were ever required
to stay inside the shoreline inset. Every transit leg was checked for no-go clearance alone and
was free to run anywhere, so a package could ship a route its own geometry did not contain.

WHAT IS PINNED HERE
-------------------
  • the invariant: every route leg ⊂ (navigable_geometry ∪ home_corridor) − no-go exclusion;
  • segments, not just waypoints — a straight leg between two approved points is not approved;
  • the corridor is DERIVED from the approved transit path, never invented around a Home;
  • THE RAW OPERATOR BOUNDARY IS NEVER A FALLBACK: a route that fits the boundary but not the
    navigable geometry is REFUSED, because accepting it would silently discard the shoreline
    clearance keeping the hull off the shore (this is the Run-2 class, pinned explicitly);
  • the effective no-go exclusion (drawn zones ⊕ no_go_clearance_m) binds the corridor too, so
    a corridor can never open a legal tunnel through geometry the operator excluded;
  • the finalized record, the Scout package and a fleet child mission all carry — and are all
    held to — the same contract.

The geometry stack (shapely/pyproj/numpy) is optional; every geometry test skips with a clear
reason when it is missing, exactly as the endpoints degrade at runtime.
"""
import math
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import planning  # noqa: E402
import replan_package  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Finalizing writes the durable mission snapshot — point it at a throwaway directory for this
# whole module so a test run can never touch the station's real store.
main.MISSION_STORE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="operator-mission-geometry-"))
main.MISSION_STORE_PATH = main.MISSION_STORE_DIR / "mission_store.json"

SCOUT_VID = 2

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

# ── a metric fixture frame ────────────────────────────────────────────────────────────────
# Every fixture below is written in METRES from one origin and converted to [lng, lat] here, so
# a test can say "30 m south of the shore" and mean it. The survey sits by the project site.
LAT0, LNG0 = 56.6790, 12.8100
_M_PER_DEG_LAT = 111320.0
_M_PER_DEG_LNG = 111320.0 * math.cos(math.radians(LAT0))


def P(east_m, north_m):
    """[lng, lat] for a point `east_m`/`north_m` metres from the fixture origin."""
    return [round(LNG0 + east_m / _M_PER_DEG_LNG, 7), round(LAT0 + north_m / _M_PER_DEG_LAT, 7)]


# A 240 m x 180 m rectangular survey area. With the default 5 m shoreline clearance the
# navigable geometry is the 230 m x 170 m rectangle inset from it — the band between the two is
# exactly where the Run-2 route waypoints lived.
BOUNDARY = [P(0, 0), P(240, 0), P(240, 180), P(0, 180)]
# An L-shaped variant: the straight line between the two arms leaves the navigable geometry even
# though both endpoints are comfortably inside it.
L_BOUNDARY = [P(0, 0), P(240, 0), P(240, 80), P(100, 80), P(100, 180), P(0, 180)]
# A no-go zone in the middle of the rectangle. With a 5 m no-go clearance its EFFECTIVE
# exclusion is the (105..135, 75..105) region.
ZONE = [P(110, 80), P(130, 80), P(130, 100), P(110, 100)]

HOME_INSIDE = P(120, 90)        # in the middle of the survey — no corridor is needed
HOME_OUTSIDE = P(120, -30)      # 30 m south of the shore, as a launch ramp actually sits


def inputs(**over):
    """Planning inputs at the stated defaults (shoreline 5 m, no-go 5 m, lanes 10 m)."""
    body = {
        "boundary": BOUNDARY,
        "shoreline_clearance_m": planning.DEFAULT_SHORELINE_CLEARANCE_M,
        "no_go_clearance_m": planning.DEFAULT_NO_GO_CLEARANCE_M,
        "lane_spacing_m": planning.DEFAULT_LANE_SPACING_M,
        "primary_angle_deg": 90,
        "dual_pass": False,
        "home": HOME_INSIDE,
    }
    body.update(over)
    return body


def navigable_of(boundary=None, clearance=None):
    return planning._navigable_rings_deg(
        boundary or BOUNDARY,
        planning.DEFAULT_SHORELINE_CLEARANCE_M if clearance is None else clearance)


def wps(*points):
    return [{"latitude": p[1], "longitude": p[0], "loiter_time_s": 0} for p in points]


def check(*, segments=None, route=None, navigable=None, zones=None, clearance=None,
          home=None, corridor=None):
    """`check_mission_geometry` with the fixture's defaults filled in."""
    return planning.check_mission_geometry(
        segments=segments or [],
        route_waypoints=route or [],
        navigable_geometry=navigable if navigable is not None else navigable_of(),
        no_go_zones=zones or [],
        no_go_clearance_m=(planning.DEFAULT_NO_GO_CLEARANCE_M if clearance is None else clearance),
        planning_home=home,
        home_corridor=corridor)


def codes(report):
    return sorted({f["code"] for f in report["failures"]})


# ══════════════════════════════════════════════════════════════════════════════════════════
# 1–2. THE CORRIDOR: when it is not needed, and how it is derived when it is
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestHomeInsideNavigableGeometry(unittest.TestCase):
    """Case 1 — Home inside the navigable geometry. The mission is valid, and it is valid
    WITHOUT any corridor: the corridor is not a field a mission has to carry, it is an answer to
    a question this mission does not ask."""

    def setUp(self):
        self.package = planning.generate_survey(inputs(home=HOME_INSIDE))

    def test_the_mission_generates_and_proves_its_own_geometry(self):
        self.assertTrue(self.package["geometry_check"]["ok"],
                        self.package["geometry_check"]["failures"])

    def test_the_mission_is_valid_with_no_corridor_at_all(self):
        report = check(segments=self.package["segments"],
                       route=self.package["route_waypoints"],
                       navigable=self.package["navigable_boundary"],
                       home=HOME_INSIDE, corridor=None)
        self.assertTrue(report["ok"], report["failures"])
        self.assertTrue(report["checks"]["home_within_approved"])


@requires_geometry
class TestHomeOutsideWithValidTransit(unittest.TestCase):
    """Case 2 — Home outside the navigable geometry with an approved transit path. A corridor is
    derived DETERMINISTICALLY from that path; it contains Home, overlaps the navigable geometry,
    and the whole mission validates against navigable ∪ corridor."""

    def setUp(self):
        self.package = planning.generate_survey(inputs(home=HOME_OUTSIDE))
        self.corridor = self.package["home_corridor"]
        self.meta = self.package["home_corridor_meta"]

    def test_a_corridor_is_derived(self):
        self.assertIsNotNone(self.corridor, self.meta.get("reason"))
        self.assertTrue(self.meta["available"])

    def test_the_corridor_contains_home_and_overlaps_the_navigable_geometry(self):
        self.assertTrue(self.meta["contains_planning_home"])
        self.assertTrue(self.meta["overlaps_navigable"])
        self.assertTrue(self.meta["covers_transit_path"])

    def test_the_corridor_uses_the_one_stated_half_width(self):
        # There is exactly ONE corridor width in planning semantics and this is it. A second,
        # unrelated width would make the shipped geometry unreproducible from the record.
        self.assertEqual(self.meta["half_width_m"], planning.HOME_CORRIDOR_HALF_WIDTH_M)
        self.assertEqual(planning.HOME_CORRIDOR_HALF_WIDTH_M, 6.0)

    def test_derivation_is_deterministic(self):
        again = planning.generate_survey(inputs(home=HOME_OUTSIDE))
        self.assertEqual(self.corridor, again["home_corridor"])

    def test_the_whole_mission_validates_against_navigable_plus_corridor(self):
        self.assertTrue(self.package["geometry_check"]["ok"],
                        self.package["geometry_check"]["failures"])
        checks = self.package["geometry_check"]["checks"]
        self.assertTrue(checks["transit_segments_within_approved"])
        self.assertTrue(checks["survey_segments_within_navigable"])
        self.assertTrue(checks["route_legs_within_approved"])
        self.assertTrue(checks["home_within_approved"])
        self.assertTrue(checks["corridor_overlaps_navigable"])
        self.assertTrue(checks["corridor_covers_transit"])

    def test_the_same_mission_FAILS_once_the_corridor_is_taken_away(self):
        # The corridor is doing real work here — this is what proves it is not decorative.
        report = check(segments=self.package["segments"],
                       route=self.package["route_waypoints"],
                       navigable=self.package["navigable_boundary"],
                       home=HOME_OUTSIDE, corridor=None)
        self.assertFalse(report["ok"])
        self.assertIn("HOME_OUTSIDE_APPROVED_GEOMETRY", codes(report))


@requires_geometry
class TestHomeOutsideWithoutValidTransit(unittest.TestCase):
    """Case 3 — Home outside and no approved transit path. No corridor is invented, and the
    mission is refused rather than approved against a polygon drawn around the Home."""

    def test_no_transit_segments_yields_no_corridor_and_a_named_reason(self):
        package = planning.generate_survey(inputs(home=HOME_OUTSIDE))
        coverage_only = [s for s in package["segments"]
                         if s["kind"] in planning.GEOMETRY_SURVEY_KINDS]
        ring, meta = planning.home_corridor_ring(
            segments=coverage_only, navigable_geometry=package["navigable_boundary"],
            no_go_zones=[], planning_home=HOME_OUTSIDE,
            no_go_clearance_m=planning.DEFAULT_NO_GO_CLEARANCE_M)
        self.assertIsNone(ring)
        self.assertIn("transit", meta["reason"])

    def test_the_mission_is_refused_rather_than_approved_around_the_home(self):
        report = check(
            segments=[{"kind": "start_connector", "coordinates": [HOME_OUTSIDE, P(120, 10)]},
                      {"kind": "primary", "coordinates": [P(120, 10), P(120, 170)]}],
            route=wps(HOME_OUTSIDE, P(120, 10), P(120, 170)),
            home=HOME_OUTSIDE, corridor=None)
        self.assertFalse(report["ok"])
        self.assertIn("HOME_OUTSIDE_APPROVED_GEOMETRY", codes(report))
        self.assertIn("TRANSIT_OUTSIDE_APPROVED_GEOMETRY", codes(report))


# ══════════════════════════════════════════════════════════════════════════════════════════
# 4–5. THE NO-GO EXCLUSION BINDS THE TRANSIT PATH AND THE CORRIDOR
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestNoGoBindsTransitAndCorridor(unittest.TestCase):

    def test_transit_crossing_the_no_go_buffer_is_rejected(self):
        """Case 4 — Home outside, transit straight through the buffered zone."""
        report = check(
            segments=[{"kind": "start_connector", "coordinates": [HOME_OUTSIDE, P(120, 120)]},
                      {"kind": "primary", "coordinates": [P(120, 120), P(120, 170)]}],
            route=wps(HOME_OUTSIDE, P(120, 120), P(120, 170)),
            zones=[ZONE], home=HOME_OUTSIDE, corridor=None)
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_NO_GO_VIOLATION", codes(report))

    def test_a_transit_path_through_a_zone_yields_no_corridor(self):
        ring, meta = planning.home_corridor_ring(
            segments=[{"kind": "start_connector", "coordinates": [HOME_OUTSIDE, P(120, 120)]}],
            navigable_geometry=navigable_of(), no_go_zones=[ZONE],
            planning_home=HOME_OUTSIDE,
            no_go_clearance_m=planning.DEFAULT_NO_GO_CLEARANCE_M)
        self.assertIsNone(ring)
        self.assertFalse(meta["clears_no_go_zones"])
        self.assertIn("no-go", meta["reason"])

    def test_a_corridor_overlapping_the_buffered_exclusion_is_rejected(self):
        """Case 5 — a corridor that would tunnel through the exclusion. Supplied by hand, since
        the derivation refuses to build one; the check must refuse it independently."""
        tunnel = [P(114, -35), P(126, -35), P(126, 120), P(114, 120)]
        report = check(
            segments=[{"kind": "start_connector", "coordinates": [HOME_OUTSIDE, P(120, 120)]}],
            route=wps(HOME_OUTSIDE, P(120, 120)),
            zones=[ZONE], home=HOME_OUTSIDE, corridor=tunnel)
        self.assertFalse(report["ok"])
        self.assertIn("HOME_CORRIDOR_NO_GO_VIOLATION", codes(report))
        self.assertFalse(report["checks"]["corridor_clears_no_go"])

    def test_a_leg_between_two_clear_endpoints_that_crosses_the_buffer_is_rejected(self):
        """Case 9 — both waypoints clear the exclusion; the straight leg between them does not.
        Waypoint-only validation would pass this route. That is the whole point."""
        a, b = P(80, 90), P(160, 90)
        report = check(segments=[{"kind": "primary", "coordinates": [a, b]}],
                       route=wps(a, b), zones=[ZONE], home=HOME_INSIDE)
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_NO_GO_VIOLATION", codes(report))
        self.assertTrue(report["checks"]["waypoints_clear_no_go"])   # the endpoints ARE clear
        self.assertFalse(report["checks"]["route_legs_clear_no_go"])  # the leg is not


# ══════════════════════════════════════════════════════════════════════════════════════════
# 6–8. CONTAINMENT — AND THE RUN-2 REGRESSION
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestRouteContainment(unittest.TestCase):

    def test_a_survey_waypoint_outside_the_navigable_geometry_is_rejected(self):
        """Case 6."""
        stray = P(2, 90)         # inside the raw boundary, outside the 5 m inset
        report = check(segments=[{"kind": "primary", "coordinates": [P(20, 90), stray]}],
                       route=wps(P(20, 90), stray), home=HOME_INSIDE)
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", codes(report))
        self.assertFalse(report["checks"]["waypoints_within_approved"])

    def test_a_survey_segment_that_leaves_the_navigable_geometry_is_rejected(self):
        """Case 7 — both endpoints are well inside; the leg cuts across the L's missing corner."""
        a, b = P(200, 40), P(50, 140)
        report = check(segments=[{"kind": "primary", "coordinates": [a, b]}],
                       route=wps(a, b), navigable=navigable_of(L_BOUNDARY), home=None)
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", codes(report))
        self.assertFalse(report["checks"]["survey_segments_within_navigable"])
        self.assertTrue(report["checks"]["waypoints_within_approved"])   # the ENDS are fine


@requires_geometry
class TestRawBoundaryIsNotAFallback(unittest.TestCase):
    """THE LIVE E2 RUN-2 REGRESSION. A route every waypoint of which sits inside the raw operator
    boundary, and several of which sit OUTSIDE the shoreline-inset navigable geometry — exactly
    the package Scout was handed, tried to retrace, and correctly refused.

    The Operator must fail this with a specific geometry-consistency error and must NOT reach for
    the raw boundary to make it pass. Widening to the boundary would discard the shoreline
    clearance, which is the one thing keeping the hull off the shore."""

    # Every point lies in the 5 m band between the boundary and the inset.
    RUN2_ROUTE = [P(2, 20), P(2, 160), P(238, 160), P(238, 20)]

    def setUp(self):
        self.segments = [{"kind": "primary", "coordinates": self.RUN2_ROUTE}]
        self.route = wps(*self.RUN2_ROUTE)

    def test_the_route_IS_contained_by_the_raw_boundary(self):
        # Stated explicitly so the next assertion cannot be mistaken for a broken fixture: the
        # route really would pass if the boundary were accepted as the safety region.
        against_boundary = check(segments=self.segments, route=self.route,
                                 navigable=[BOUNDARY], home=None)
        self.assertTrue(against_boundary["ok"], against_boundary["failures"])

    def test_but_the_navigable_geometry_refuses_it_with_a_specific_code(self):
        report = check(segments=self.segments, route=self.route, home=None)
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", codes(report))

    def test_finalization_refuses_the_package_and_stores_no_mission(self):
        client = TestClient(main.app)
        before = set(main.original_missions)
        package = {
            "mission_package_version": planning.MISSION_PACKAGE_VERSION,
            "contract_version": planning.ROUTE_CONTRACT_VERSION,
            "planning_inputs": {
                "boundary": BOUNDARY, "shoreline_clearance_m": 5.0,
                "navigable_boundary": navigable_of(), "no_go_zones": [],
                "no_go_clearance_m": 5.0, "lane_spacing_m": 10.0,
                "planning_home": None,
            },
            "segments": self.segments,
            "route_waypoints": self.route,
            "navigable_boundary": navigable_of(),
        }
        resp = client.post("/api/missions/finalize", json={
            "vehicle_id": SCOUT_VID, "mission_package": package, "confirm": True})
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertEqual(body["error"], "mission_geometry_inconsistent")
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", body["codes"])
        self.assertEqual(set(main.original_missions), before,
                         "an inconsistent package must not create a mission record")

    def test_the_validate_endpoint_refuses_it_before_the_operator_can_upload(self):
        client = TestClient(main.app)
        resp = client.post("/api/planning/validate", json={
            "boundary": BOUNDARY, "shoreline_clearance_m": 5.0, "no_go_clearance_m": 5.0,
            "lane_spacing_m": 10.0, "no_go_zones": [],
            "segments": self.segments, "route_waypoints": self.route})
        self.assertEqual(resp.status_code, 200)      # validation always answers; it just says no
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertFalse(body["checks"]["geometry_consistent"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", body["checks"]["geometry_codes"])

    def test_the_corrected_route_generated_inside_the_navigable_geometry_finalizes(self):
        client = TestClient(main.app)
        package = planning.generate_survey(inputs(home=HOME_OUTSIDE))
        resp = client.post("/api/missions/finalize", json={
            "vehicle_id": SCOUT_VID, "mission_package": package, "confirm": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["mission"]["mission_id"])


# ══════════════════════════════════════════════════════════════════════════════════════════
# 10–11. PARAMETERS
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestDefaultsAndClearanceSweep(unittest.TestCase):

    def test_the_stated_defaults(self):
        """Case 10 — shoreline 5 m, no-go 5 m, lanes 10 m, applied when a caller omits them."""
        self.assertEqual(planning.DEFAULT_SHORELINE_CLEARANCE_M, 5.0)
        self.assertEqual(planning.DEFAULT_NO_GO_CLEARANCE_M, 5.0)
        self.assertEqual(planning.DEFAULT_LANE_SPACING_M, 10.0)
        norm = planning.normalize_generate_inputs({"boundary": BOUNDARY,
                                                   "shoreline_clearance_m": 5.0})
        self.assertEqual(norm["no_go_clearance_m"], 5.0)
        self.assertEqual(norm["lane_spacing_m"], 10.0)

    def test_no_go_clearance_0_5_10_changes_the_geometry_and_stays_consistent(self):
        """Case 11 — the exclusion grows with the clearance, and every resulting mission still
        proves its own geometry."""
        areas = []
        for clearance in (0.0, 5.0, 10.0):
            package = planning.generate_survey(
                inputs(no_go_zones=[ZONE], no_go_clearance_m=clearance, home=HOME_OUTSIDE))
            self.assertTrue(package["geometry_check"]["ok"],
                            (clearance, package["geometry_check"]["failures"]))
            self.assertEqual(package["planning_inputs"]["no_go_clearance_m"], clearance)
            # PROVENANCE: the drawn ring is preserved exactly, never replaced by the buffer.
            self.assertEqual(package["planning_inputs"]["no_go_zones"],
                             [planning._ring(ZONE)])
            rings = package["no_go_exclusion_rings"]
            areas.append(0.0 if not rings else planning._polygon_area_m2(rings[0]))
        self.assertEqual(areas[0], 0.0, "a 0 m clearance ships no derived exclusion overlay")
        self.assertGreater(areas[2], areas[1], "10 m must exclude more than 5 m")


# ══════════════════════════════════════════════════════════════════════════════════════════
# 13–14. THE FINALIZED RECORD AND THE SCOUT PACKAGE
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestFinalizedRecordAndReplanPackage(unittest.TestCase):
    """Cases 13 and 14 — the immutable record keeps the authoritative geometry contract, and the
    Scout package is built from it without re-deriving or substituting anything."""

    def setUp(self):
        self.client = TestClient(main.app)
        self.package = planning.generate_survey(
            inputs(no_go_zones=[ZONE], home=HOME_OUTSIDE))
        resp = self.client.post("/api/missions/finalize", json={
            "vehicle_id": SCOUT_VID, "mission_package": self.package, "confirm": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.record = main.original_missions[resp.json()["mission"]["mission_id"]]

    def test_the_record_carries_the_authoritative_geometry(self):
        inputs_ = self.record["planning_inputs"]
        self.assertEqual(self.record["navigable_geometry"], self.package["navigable_boundary"])
        self.assertEqual(inputs_["boundary"], planning._ring(BOUNDARY))
        # ORIGINAL rings, not buffered — provenance survives finalization untouched.
        self.assertEqual(inputs_["no_go_zones"], [planning._ring(ZONE)])
        self.assertEqual(inputs_["no_go_clearance_m"], 5.0)
        self.assertEqual(inputs_["shoreline_clearance_m"], 5.0)
        self.assertEqual(inputs_["lane_spacing_m"], 10.0)
        self.assertEqual(inputs_["planning_home"], HOME_OUTSIDE)
        self.assertIn("approach_waypoints", inputs_)
        self.assertIn("return_waypoints", inputs_)

    def test_the_record_carries_the_home_corridor_it_was_finalized_with(self):
        self.assertEqual(self.record["home_corridor"], self.package["home_corridor"])
        self.assertIsNotNone(self.record["home_corridor"])

    def test_the_record_still_proves_its_own_geometry_when_re_checked(self):
        report = planning.check_package_geometry(self.record)
        self.assertTrue(report["ok"], report["failures"])

    def test_the_scout_package_carries_the_exact_approved_contract(self):
        pkg, meta = replan_package.build_v1_package(self.record, vehicle_id=SCOUT_VID)
        self.assertEqual(pkg["navigable_geometry"], self.record["navigable_geometry"])
        self.assertEqual(pkg["no_go_zones"],
                         [[[float(c[0]), float(c[1])] for c in planning._ring(ZONE)]])
        self.assertEqual(pkg["home_corridor"], self.record["home_corridor"])
        self.assertEqual(pkg["route_waypoints"], self.record["route_waypoints"])
        self.assertEqual(pkg["route_hash"], self.record["route_hash"])
        self.assertEqual(meta["no_go_clearance_m"], 5.0)

    def test_the_package_corridor_is_the_stored_one_not_a_fresh_derivation(self):
        ring, corridor_meta = replan_package.derive_home_corridor(self.record)
        self.assertEqual(ring, self.record["home_corridor"])
        self.assertEqual(corridor_meta.get("source"), "finalized_record")

    def test_the_package_geometry_is_itself_self_consistent(self):
        pkg, _ = replan_package.build_v1_package(self.record, vehicle_id=SCOUT_VID)
        report = planning.check_mission_geometry(
            segments=pkg["segments"], route_waypoints=pkg["route_waypoints"],
            navigable_geometry=pkg["navigable_geometry"], no_go_zones=pkg["no_go_zones"],
            no_go_clearance_m=5.0, planning_home=pkg["planning_home"],
            home_corridor=pkg.get("home_corridor"))
        self.assertTrue(report["ok"], report["failures"])


# ══════════════════════════════════════════════════════════════════════════════════════════
# 15. FLEET
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestFleetChildMissions(unittest.TestCase):
    """Case 15 — a fleet child mission is an ordinary operator-survey-plan-v1 package and gets
    no exemption from the contract."""

    def setUp(self):
        import fleet_planning
        self.plan = fleet_planning.generate_fleet({
            "boundary": BOUNDARY,
            "shoreline_clearance_m": 5.0,
            "no_go_clearance_m": 5.0,
            "lane_spacing_m": 10.0,
            "primary_angle_deg": 90,
            "vehicles": [{"vehicle_id": "usv-2", "home": P(60, -30)},
                         {"vehicle_id": "usv-3", "home": P(180, -30)}],
        })

    def test_every_child_mission_proves_its_own_geometry(self):
        for vehicle in self.plan["vehicles"]:
            package = vehicle["mission_package"]
            self.assertTrue(package["geometry_check"]["ok"],
                            (vehicle["vehicle_id"], package["geometry_check"]["failures"]))
            report = planning.check_package_geometry(package)
            self.assertTrue(report["ok"], (vehicle["vehicle_id"], report["failures"]))

    def test_every_child_mission_carries_its_own_home_corridor(self):
        for vehicle in self.plan["vehicles"]:
            package = vehicle["mission_package"]
            self.assertIsNotNone(package["home_corridor"],
                                 package["home_corridor_meta"].get("reason"))
            self.assertEqual(package["home_corridor_meta"]["half_width_m"],
                             planning.HOME_CORRIDOR_HALF_WIDTH_M)

    def test_the_fleet_validation_agrees_the_children_are_valid(self):
        self.assertTrue(self.plan["validation"]["checks"]["child_missions_valid"],
                        self.plan["validation"]["errors"])


# ══════════════════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestBackwardCompatibility(unittest.TestCase):
    """A historical record lacks `no_go_clearance_m` and `home_corridor`. It keeps loading under
    the existing migration semantics — absent clearance reads as the drawn geometry itself (0),
    absent corridor is re-derived — and is never silently 'repaired' by broadening its geometry."""

    def test_a_record_without_a_stored_corridor_is_derived_as_before(self):
        package = planning.generate_survey(inputs(home=HOME_OUTSIDE))
        legacy = {
            "planning_inputs": {k: v for k, v in package["planning_inputs"].items()
                                if k != "no_go_clearance_m"},
            "navigable_geometry": package["navigable_boundary"],
            "segments": package["segments"],
        }
        ring, meta = replan_package.derive_home_corridor(legacy)
        self.assertIsNotNone(ring, meta.get("reason"))
        self.assertEqual(meta["no_go_clearance_m"], 0.0)
        self.assertNotEqual(meta.get("source"), "finalized_record")

    def test_a_historical_record_is_never_validated_against_the_raw_boundary(self):
        legacy = {
            "planning_inputs": {"boundary": BOUNDARY, "planning_home": None},
            "navigable_geometry": navigable_of(),
            "segments": [{"kind": "primary", "coordinates": [P(2, 20), P(2, 160)]}],
            "route_waypoints": wps(P(2, 20), P(2, 160)),
        }
        report = planning.check_package_geometry(legacy)
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", codes(report))


@requires_geometry
class TestInvalidNavigableGeometry(unittest.TestCase):
    """There is no route that passes when there is no navigable geometry to pass against — and
    the raw boundary is not offered as a stand-in."""

    def test_absent_navigable_geometry_is_a_named_failure(self):
        report = check(segments=[{"kind": "primary", "coordinates": [P(20, 20), P(20, 160)]}],
                       route=wps(P(20, 20), P(20, 160)), navigable=[], home=None)
        self.assertFalse(report["ok"])
        self.assertEqual(codes(report), ["INVALID_NAVIGABLE_GEOMETRY"])

    def test_an_empty_route_is_a_named_failure(self):
        report = check(route=[], home=None)
        self.assertEqual(codes(report), ["ROUTE_EMPTY"])

    def test_generation_raises_rather_than_returning_an_inconsistent_package(self):
        # The exception type the generate endpoint maps to a 400 `mission_geometry_inconsistent`.
        # Generation cannot normally produce one (that is the point), so this pins the contract
        # of the failure path itself: a code list, not a prose blob.
        exc = planning.GeometryConsistencyError(
            [{"code": "ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", "message": "…"}])
        self.assertEqual(exc.codes, ["ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", str(exc))
        for code in planning.GEOMETRY_ERROR_CODES:
            self.assertRegex(code, r"^[A-Z_]+$")


if __name__ == "__main__":
    unittest.main()
