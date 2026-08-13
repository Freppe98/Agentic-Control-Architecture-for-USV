"""Operator/Scout geometry parity: the route the operator approves is the route Scout accepts.

Run from operator-scripts/:  python -m unittest tests.test_scout_geometry_parity   (no pytest)

THE LIVE FAILURE THIS FILE PINS
-------------------------------
A 42-waypoint survey (1 no-go zone, 1 m shoreline clearance, 1 m no-go clearance, 5 m lanes,
42 deg survey angle) validated VALID on the Plan page, uploaded to the Pixhawk and verified —
and was then rejected by Scout with HTTP 400 ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY:

    route segment 7 lies outside the approved geometry (navigable_geometry / home_corridor)

Because the package was rejected Scout kept the PREVIOUS one, which is what surfaced as
PLANNING_PACKAGE_STALE / ROUTE_HASH_STALE / NOT_READY / ENERGY UNKNOWN / RISK UNKNOWN.

The route and the geometry were the same on both sides. The TOLERANCE was not. Coverage lanes
were clipped FLUSH against the navigable polygon, so a lane end landed exactly ON the approved
boundary and the cross-lane transition joining two such ends ran ALONG that boundary. Serialized
to the 7-decimal wire representation, four of those transitions lay 0.4-3.2 mm OUTSIDE the
polygon they were clipped from. The operator's containment proof buffered the approved region
outward by COVER_TOL_M (0.5 m) and additionally forgave CONNECTOR_EPS_M (1.0 m) of a leg lying
outside it, so it saw nothing. Scout proves containment exactly, so it saw all of it.

WHAT IS PINNED HERE
-------------------
  * the exact live class: a leg that rides the approved edge passes the tolerant checks and is
    REFUSED anyway, before Finish & Upload, by the exact check (fail closed);
  * a generated mission survives the wire round trip — the SERIALIZED route is exactly covered
    by the SERIALIZED geometry, which is the property Scout actually tests;
  * routes A/B/C (validated / uploaded to the Pixhawk / put in the planning package) are one
    identical canonical route, and the geometry validated is the geometry serialized;
  * boundary-touch is INSIDE, consistently, so the two sides cannot disagree on the edge itself;
  * the Home corridor approves transit that the navigable polygon does not, and nothing else.

`scout_equivalent_outside_legs` below is the Scout rule re-implemented on OPERATOR geometry —
`covers`, no buffers, no epsilon — applied to the exact serialized artifact. It is deliberately
independent of check_mission_geometry so the two can be compared rather than assumed equal.
"""
import math
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import mission_contract  # noqa: E402
import planning  # noqa: E402
import replan_package  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

main.MISSION_STORE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="operator-scout-parity-"))
main.MISSION_STORE_PATH = main.MISSION_STORE_DIR / "mission_store.json"

SCOUT_VID = 2

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

# ── a metric fixture frame ────────────────────────────────────────────────────────────────
LAT0, LNG0 = 56.6790, 12.8100
_M_PER_DEG_LAT = 111320.0
_M_PER_DEG_LNG = 111320.0 * math.cos(math.radians(LAT0))


def P(east_m, north_m):
    """[lng, lat] for a point `east_m`/`north_m` metres from the fixture origin, at the SAME
    7-decimal precision the wire uses — a fixture must not be more precise than the contract."""
    return [round(LNG0 + east_m / _M_PER_DEG_LNG, 7), round(LAT0 + north_m / _M_PER_DEG_LAT, 7)]


BOUNDARY = [P(0, 0), P(240, 0), P(240, 180), P(0, 180)]
# L-shaped: the straight line between the two arms leaves the navigable geometry although both
# endpoints are well inside it.
L_BOUNDARY = [P(0, 0), P(240, 0), P(240, 80), P(100, 80), P(100, 180), P(0, 180)]
# A dumbbell whose 4 m waist is narrower than twice the shoreline clearance, so the inset
# SPLITS into two disconnected navigable components.
DUMBBELL = [P(0, 0), P(100, 0), P(100, 88), P(140, 88), P(140, 0), P(240, 0),
            P(240, 180), P(140, 180), P(140, 92), P(100, 92), P(100, 180), P(0, 180)]
ZONE = [P(110, 80), P(130, 80), P(130, 100), P(110, 100)]

HOME_INSIDE = P(120, 90)
# A planning Home for the GENERATED missions: inside the navigable area and clear of ZONE's
# buffered exclusion, which swallows HOME_INSIDE once a no-go clearance is applied.
HOME_SURVEY = P(40, 40)
HOME_OUTSIDE = P(120, -30)


def wps(*points):
    return [{"latitude": p[1], "longitude": p[0], "loiter_time_s": 0} for p in points]


def navigable_of(boundary=None, clearance=None):
    return planning._navigable_rings_deg(
        boundary or BOUNDARY,
        planning.DEFAULT_SHORELINE_CLEARANCE_M if clearance is None else clearance)


def check(*, segments=None, route=None, navigable=None, zones=None, clearance=None,
          home=None, corridor=None):
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


# ── the Scout rule, on operator geometry ──────────────────────────────────────────────────
def scout_equivalent_outside_legs(route_waypoints, navigable_geometry, home_corridor=None):
    """1-based indices of route legs NOT fully contained by `navigable_geometry ∪ home_corridor`.

    Scout's semantics, deliberately re-derived rather than copied: EXACT containment of every
    full leg (`covers`), no outward buffer, no forgiven length, on the artifact exactly as it is
    serialized. A leg lying ON the boundary is covered — touching is inside on both sides.
    """
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    rings = [r for r in (navigable_geometry or []) if r and len(r) >= 3]
    to_proj, _ = planning._utm_for(rings[0])

    def poly(ring):
        return Polygon([to_proj.transform(c[0], c[1]) for c in ring]).buffer(0)

    approved = unary_union([poly(r) for r in rings])
    if home_corridor:
        approved = unary_union([approved, poly(home_corridor)])

    pts = [(w["longitude"], w["latitude"]) for w in route_waypoints]
    bad = []
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        leg = LineString([to_proj.transform(*a), to_proj.transform(*b)])
        if not approved.covers(leg):
            bad.append(i + 1)
    return bad


def metres_outside(route_waypoints, navigable_geometry, home_corridor=None):
    """The deepest excursion (metres) of any leg beyond the approved region — how far outside,
    not merely whether. Lets a test state that an excursion was millimetric AND fatal."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union

    rings = [r for r in (navigable_geometry or []) if r and len(r) >= 3]
    to_proj, _ = planning._utm_for(rings[0])

    def poly(ring):
        return Polygon([to_proj.transform(c[0], c[1]) for c in ring]).buffer(0)

    approved = unary_union([poly(r) for r in rings])
    if home_corridor:
        approved = unary_union([approved, poly(home_corridor)])

    pts = [(w["longitude"], w["latitude"]) for w in route_waypoints]
    worst = 0.0
    for a, b in zip(pts, pts[1:]):
        rest = LineString([to_proj.transform(*a), to_proj.transform(*b)]).difference(approved)
        if rest.is_empty:
            continue
        coords = (rest.coords if rest.geom_type == "LineString"
                  else [c for g in rest.geoms for c in g.coords])
        worst = max([worst] + [approved.distance(Point(c)) for c in coords])
    return worst


def plan_inputs(**over):
    body = {
        "boundary": BOUNDARY,
        "shoreline_clearance_m": planning.DEFAULT_SHORELINE_CLEARANCE_M,
        "no_go_clearance_m": planning.DEFAULT_NO_GO_CLEARANCE_M,
        "lane_spacing_m": planning.DEFAULT_LANE_SPACING_M,
        "primary_angle_deg": 42,
        "dual_pass": False,
        "home": HOME_SURVEY,
    }
    body.update(over)
    return body


# ══════════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTheLiveFailureClass(unittest.TestCase):
    """A route leg that rides the approved edge — the exact geometry Scout rejected."""

    def setUp(self):
        self.navigable = navigable_of()
        ring = self.navigable[0]
        # The northern edge of the inset rectangle, nudged 5 cm OUTWARD: the same shape as a
        # cross-lane transition joining two lane ends clipped flush to the boundary, only
        # exaggerated from millimetres to centimetres so it is expressible at 7 decimals.
        north = max(pt[1] for pt in ring)
        self.offset_deg = round(0.05 / _M_PER_DEG_LAT, 9)
        self.route = wps(P(60, 90),
                         [P(60, 90)[0], round(north + self.offset_deg, 7)],
                         [P(100, 90)[0], round(north + self.offset_deg, 7)],
                         P(100, 90))

    def test_the_excursion_is_far_smaller_than_the_operator_tolerance(self):
        # This is WHY the operator saw nothing: the leg is outside by centimetres and the
        # tolerant proof buffers the region outward by half a metre.
        depth = metres_outside(self.route, self.navigable)
        self.assertGreater(depth, 0.0, "the fixture does not actually leave the region")
        self.assertLess(depth, planning.COVER_TOL_M,
                        "the fixture is a gross excursion, not the live edge-riding class")

    def test_the_tolerant_leg_check_still_passes_it(self):
        report = check(route=self.route, navigable=self.navigable, home=HOME_INSIDE)
        self.assertTrue(report["checks"]["route_legs_within_approved"],
                        "the tolerant check is supposed to be the one that misses this")

    def test_the_exact_check_refuses_it_and_names_the_leg(self):
        report = check(route=self.route, navigable=self.navigable, home=HOME_INSIDE)
        self.assertFalse(report["checks"]["route_legs_exactly_within_approved"])
        self.assertFalse(report["ok"], "an edge-riding route must not validate")
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", codes(report))
        failure = next(f for f in report["failures"]
                       if f["code"] == "ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY" and f.get("legs"))
        # Leg 2 is waypoint 2 -> 3, the one running along the edge.
        self.assertIn(2, failure["legs"])
        self.assertIn("route segment", failure["message"])

    def test_operator_and_scout_equivalent_now_agree_leg_for_leg(self):
        report = check(route=self.route, navigable=self.navigable, home=HOME_INSIDE)
        operator_legs = sorted({leg
                                for f in report["failures"] if f.get("legs")
                                for leg in f["legs"]})
        self.assertEqual(operator_legs,
                         scout_equivalent_outside_legs(self.route, self.navigable),
                         "the operator and Scout still disagree about which legs are outside")

    def test_the_same_route_pulled_inside_the_margin_is_accepted(self):
        # The refusal is about the geometry, not about being near the edge: the identical route
        # one wire margin INSIDE the boundary validates.
        ring = self.navigable[0]
        north = max(pt[1] for pt in ring)
        inside = round(north - self.offset_deg, 7)
        route = wps(P(60, 90), [P(60, 90)[0], inside], [P(100, 90)[0], inside], P(100, 90))
        report = check(route=route, navigable=self.navigable, home=HOME_INSIDE)
        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(scout_equivalent_outside_legs(route, self.navigable), [])


# ══════════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestEndpointsInsideSegmentOutside(unittest.TestCase):
    """A straight leg between two approved points is not itself approved."""

    def test_leg_across_the_concave_notch_is_rejected(self):
        navigable = navigable_of(L_BOUNDARY)
        route = wps(P(200, 40), P(40, 140))   # both arms of the L, the chord cuts the notch
        report = check(route=route, navigable=navigable, home=P(200, 40))
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", codes(report))
        self.assertFalse(report["checks"]["route_legs_exactly_within_approved"])
        # Both ENDPOINTS are individually fine — the leg is the fault.
        self.assertTrue(report["checks"]["waypoints_within_approved"])
        self.assertEqual(scout_equivalent_outside_legs(route, navigable), [1])

    def test_the_same_pair_routed_through_the_corner_is_accepted(self):
        navigable = navigable_of(L_BOUNDARY)
        route = wps(P(200, 40), P(60, 40), P(40, 140))
        report = check(route=route, navigable=navigable, home=P(200, 40))
        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(scout_equivalent_outside_legs(route, navigable), [])


# ══════════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestNoGoHole(unittest.TestCase):
    """The buffered exclusion punches a hole in the surveyable region; a leg may not cross it."""

    def test_leg_crossing_the_buffered_exclusion_is_rejected(self):
        route = wps(P(120, 40), P(120, 140))   # straight through the zone at (110..130, 80..100)
        report = check(route=route, zones=[ZONE], home=P(120, 40))
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_NO_GO_VIOLATION", codes(report))
        self.assertFalse(report["checks"]["route_legs_clear_no_go"])

    def test_a_leg_routed_around_the_exclusion_is_accepted(self):
        route = wps(P(120, 40), P(60, 40), P(60, 140), P(120, 140))
        report = check(route=route, zones=[ZONE], home=P(120, 40))
        self.assertTrue(report["ok"], report["failures"])

    def test_the_exclusion_is_not_part_of_the_serialized_navigable_geometry(self):
        # Operator and Scout must subtract the exclusion the SAME way: the package ships the
        # shoreline inset as `navigable_geometry` and the drawn zones as `no_go_zones`, and both
        # sides derive the exclusion from the zones plus the clearance. If the operator instead
        # validated against a pre-punched polygon the two topologies would differ.
        rings = navigable_of()
        report = check(route=wps(P(60, 40), P(60, 140)), zones=[ZONE], home=P(60, 40))
        self.assertEqual(report["checks"]["navigable_ring_count"], len(rings))
        self.assertEqual(report["checks"]["no_go_zone_count"], 1)


# ══════════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestDisconnectedNavigableTopology(unittest.TestCase):
    """A shoreline inset that SPLITS keeps every component, into validation and onto the wire."""

    def setUp(self):
        self.rings = navigable_of(DUMBBELL)

    def test_the_inset_really_is_more_than_one_component(self):
        self.assertGreater(len(self.rings), 1,
                           "the dumbbell fixture no longer splits — the test proves nothing")

    def test_validation_sees_every_component(self):
        report = check(route=wps(P(40, 40), P(40, 140)), navigable=self.rings, home=P(40, 40))
        self.assertEqual(report["checks"]["navigable_ring_count"], len(self.rings))

    def test_a_leg_crossing_the_gap_between_components_is_rejected(self):
        route = wps(P(40, 90), P(200, 90))   # across the severed waist
        report = check(route=route, navigable=self.rings, home=P(40, 90))
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", codes(report))
        self.assertEqual(scout_equivalent_outside_legs(route, self.rings), [1])

    def test_serialization_preserves_the_ring_count_and_the_vertices(self):
        serialized = replan_package._positional_rings(self.rings, "navigable_geometry")
        self.assertEqual(len(serialized), len(self.rings))
        for validated_ring, wire_ring in zip(self.rings, serialized):
            self.assertEqual(len(wire_ring), len(validated_ring))
            for a, b in zip(validated_ring, wire_ring):
                self.assertAlmostEqual(a[0], b[0], places=7)
                self.assertAlmostEqual(a[1], b[1], places=7)


# ══════════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestBoundaryTouch(unittest.TestCase):
    """Touching the approved boundary is INSIDE — stated once, so both sides can rely on it."""

    def setUp(self):
        self.navigable = navigable_of()
        ring = self.navigable[0]
        self.north = max(pt[1] for pt in ring)

    def test_a_leg_lying_exactly_on_the_boundary_is_accepted(self):
        route = wps([P(60, 90)[0], self.north], [P(100, 90)[0], self.north])
        report = check(route=route, navigable=self.navigable, home=HOME_INSIDE)
        self.assertTrue(report["checks"]["route_legs_exactly_within_approved"],
                        "a leg on the boundary must count as inside")
        self.assertTrue(report["ok"], report["failures"])

    def test_scout_equivalent_agrees_that_touching_is_inside(self):
        route = wps([P(60, 90)[0], self.north], [P(100, 90)[0], self.north])
        self.assertEqual(scout_equivalent_outside_legs(route, self.navigable), [])

    def test_one_wire_quantum_outside_the_boundary_is_not_inside(self):
        # 1e-7 deg is the wire quantum itself: the smallest step the contract can express, and
        # the reason coverage is not built flush against the edge.
        out = round(self.north + 1e-7, 7)
        route = wps([P(60, 90)[0], out], [P(100, 90)[0], out])
        self.assertEqual(scout_equivalent_outside_legs(route, self.navigable), [1])
        report = check(route=route, navigable=self.navigable, home=HOME_INSIDE)
        self.assertFalse(report["checks"]["route_legs_exactly_within_approved"])


# ══════════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestHomeCorridorApprovesTransit(unittest.TestCase):
    """Transit outside the survey polygon is approved by the Home corridor, and by nothing else."""

    def setUp(self):
        self.navigable = navigable_of()
        # A corridor down the middle from the survey area to a Home 30 m offshore.
        self.corridor = [P(110, -35), P(130, -35), P(130, 100), P(110, 100)]

    def test_a_return_leg_outside_navigable_but_inside_the_corridor_is_accepted(self):
        route = wps(P(120, 60), P(120, 20), HOME_OUTSIDE)
        report = check(route=route, navigable=self.navigable, home=HOME_OUTSIDE,
                       corridor=self.corridor)
        self.assertTrue(report["checks"]["route_legs_exactly_within_approved"], report["failures"])
        self.assertEqual(
            scout_equivalent_outside_legs(route, self.navigable, self.corridor), [])
        self.assertTrue(report["checks"]["home_within_approved"])

    def test_the_same_leg_without_a_corridor_is_rejected(self):
        route = wps(P(120, 60), P(120, 20), HOME_OUTSIDE)
        report = check(route=route, navigable=self.navigable, home=HOME_OUTSIDE)
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", codes(report))
        self.assertFalse(report["checks"]["route_legs_exactly_within_approved"])

    def test_a_leg_in_neither_navigable_nor_corridor_is_rejected(self):
        # Offshore, but well to the east of the approved corridor.
        route = wps(P(200, 20), P(200, -30))
        report = check(route=route, navigable=self.navigable, home=HOME_OUTSIDE,
                       corridor=self.corridor)
        self.assertFalse(report["ok"])
        self.assertIn("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", codes(report))
        self.assertEqual(
            scout_equivalent_outside_legs(route, self.navigable, self.corridor), [1])


# ══════════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestCleanupNeverShortcutsOutside(unittest.TestCase):
    """Compression re-proves every hop it keeps, so it cannot straighten a path out of the region."""

    def test_los_compression_keeps_the_corner_of_an_L(self):
        grid = planning._NavGrid(L_BOUNDARY, planning.DEFAULT_SHORELINE_CLEARANCE_M, [],
                                 step_m=10, no_go_clearance=0)
        path = [P(200, 40), P(60, 40), P(40, 140)]
        # The direct P(200,40) -> P(40,140) shortcut cuts the notch, so it must not be taken.
        self.assertFalse(grid.segment_is_safe(path[0], path[2], require_inside=True))
        kept = grid._compress_los(path, require_inside=True)
        self.assertEqual(len(kept), 3, "compression shortcut across the notch")

    def test_los_compression_does_collapse_a_genuinely_straight_run(self):
        grid = planning._NavGrid(BOUNDARY, planning.DEFAULT_SHORELINE_CLEARANCE_M, [],
                                 step_m=10, no_go_clearance=0)
        kept = grid._compress_los([P(40, 90), P(80, 90), P(120, 90)], require_inside=True)
        self.assertEqual(len(kept), 2, "compression is not doing its job at all")

    def test_compression_keeps_the_middle_point_of_a_detour_around_a_no_go(self):
        # The requested class exactly: P1 -> P2 -> P3 is safe leg by leg, the direct P1 -> P3
        # shortcut crosses the buffered exclusion, so P2 must survive cleanup.
        grid = planning._NavGrid(BOUNDARY, planning.DEFAULT_SHORELINE_CLEARANCE_M, [ZONE],
                                 step_m=10, no_go_clearance=planning.DEFAULT_NO_GO_CLEARANCE_M)
        path = [P(120, 40), P(60, 90), P(120, 140)]
        self.assertTrue(grid.segment_is_safe(path[0], path[1], require_inside=True))
        self.assertTrue(grid.segment_is_safe(path[1], path[2], require_inside=True))
        self.assertFalse(grid.segment_is_safe(path[0], path[2], require_inside=True))
        self.assertEqual(grid._compress_los(path, require_inside=True),
                         [list(p) for p in path])


# ══════════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestGeneratedMissionSurvivesTheWire(unittest.TestCase):
    """A full lawnmower around a buffered no-go passes BOTH the operator's proof and Scout's."""

    CASES = {
        "live failure inputs (42 deg, 5 m lanes, 1 m clearances)":
            dict(primary_angle_deg=42, lane_spacing_m=5, shoreline_clearance_m=1,
                 no_go_clearance_m=1, no_go_zones=[ZONE]),
        "axis aligned (0 deg)":
            dict(primary_angle_deg=0, no_go_zones=[ZONE]),
        "axis aligned (90 deg)":
            dict(primary_angle_deg=90, no_go_zones=[ZONE]),
        "no no-go zone":
            dict(primary_angle_deg=42, no_go_zones=[]),
        "offshore Home, corridor-approved transit":
            dict(primary_angle_deg=42, no_go_zones=[ZONE], home=HOME_OUTSIDE,
                 approach_waypoints=[P(120, 20), P(120, 60)],
                 return_waypoints=[P(120, 60), P(120, 20)]),
    }

    def test_every_generated_route_is_exactly_covered_by_its_own_geometry(self):
        for name, over in self.CASES.items():
            with self.subTest(case=name):
                pkg = planning.generate_survey(plan_inputs(**over), max_route_waypoints=5000)
                outside = scout_equivalent_outside_legs(
                    pkg["route_waypoints"],
                    pkg["planning_inputs"]["navigable_boundary"],
                    pkg.get("home_corridor"))
                self.assertEqual(outside, [],
                                 f"Scout would reject {len(outside)} leg(s) of this route")

    def test_the_operator_proof_agrees_with_the_scout_rule(self):
        for name, over in self.CASES.items():
            with self.subTest(case=name):
                pkg = planning.generate_survey(plan_inputs(**over), max_route_waypoints=5000)
                report = planning.check_mission_geometry(
                    **planning.mission_geometry_arguments(pkg))
                self.assertTrue(report["ok"], report["failures"])
                self.assertTrue(report["checks"]["route_legs_exactly_within_approved"])

    def test_coverage_still_stays_clear_of_the_exclusion_and_the_shore(self):
        # The wire margin must not have been paid for out of a clearance: coverage is measured
        # against the region the operator configured, not against the margin.
        inputs = plan_inputs(primary_angle_deg=42, lane_spacing_m=5, shoreline_clearance_m=1,
                             no_go_clearance_m=1, no_go_zones=[ZONE])
        pkg = planning.generate_survey(inputs, max_route_waypoints=5000)
        grid = planning._NavGrid(BOUNDARY, 1, [ZONE], step_m=5, no_go_clearance=1)
        for seg in pkg["segments"]:
            if seg["kind"] not in planning.GEOMETRY_SURVEY_KINDS:
                continue
            for a, b in zip(seg["coordinates"], seg["coordinates"][1:]):
                self.assertTrue(grid.segment_is_safe(a, b, require_inside=True),
                                f"{seg['segment_id']} leaves the navigable region")

    def test_a_generated_route_is_at_least_the_wire_margin_inside_its_geometry(self):
        # The positive statement behind the fix: not merely "not outside", but far enough inside
        # that the 7-decimal round trip cannot push it out.
        from shapely.geometry import LineString, Polygon
        from shapely.ops import unary_union
        pkg = planning.generate_survey(
            plan_inputs(primary_angle_deg=42, lane_spacing_m=5, shoreline_clearance_m=1,
                        no_go_clearance_m=1, no_go_zones=[ZONE]),
            max_route_waypoints=5000)
        rings = pkg["planning_inputs"]["navigable_boundary"]
        to_proj, _ = planning._utm_for(rings[0])
        navigable = unary_union([
            Polygon([to_proj.transform(c[0], c[1]) for c in r]).buffer(0) for r in rings])
        for seg in pkg["segments"]:
            if seg["kind"] not in planning.GEOMETRY_SURVEY_KINDS:
                continue
            line = LineString([to_proj.transform(c[0], c[1]) for c in seg["coordinates"]])
            self.assertTrue(navigable.buffer(-planning.WIRE_MARGIN_M).covers(line),
                            f"{seg['segment_id']} sits within a wire quantum of the edge")


# ══════════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestRouteAndGeometryIdentity(unittest.TestCase):
    """Routes A/B/C are ONE route, and the geometry validated is the geometry serialized."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        # Wider lanes than the live failure only so the route fits mission-contract-v1's 200
        # waypoint ceiling — this class is about identity through finalize/upload/package, and
        # the edge-riding geometry itself is pinned by TestGeneratedMissionSurvivesTheWire.
        cls.package = planning.generate_survey(
            plan_inputs(primary_angle_deg=42, lane_spacing_m=20, shoreline_clearance_m=1,
                        no_go_clearance_m=1, no_go_zones=[ZONE]),
            max_route_waypoints=5000)
        resp = cls.client.post("/api/missions/finalize", json={
            "vehicle_id": SCOUT_VID, "mission_package": cls.package, "confirm": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        cls.record = body["mission"]
        cls.command = body["command"]

    def _routes(self):
        """(A validated, B uploaded to the Pixhawk, C in the planning package)."""
        a = self.package["route_waypoints"]
        b = (self.command.get("params") or {}).get("waypoints")
        pkg, _meta = replan_package.build_v1_package(self.record, vehicle_id=SCOUT_VID)
        return a, b, pkg["route_waypoints"], pkg

    def test_route_counts_match(self):
        a, b, c, _ = self._routes()
        self.assertEqual(len(a), len(b))
        self.assertEqual(len(a), len(c))

    def test_every_waypoint_matches_exactly_and_in_order(self):
        a, b, c, _ = self._routes()
        for i, (wa, wb, wc) in enumerate(zip(a, b, c)):
            self.assertEqual((wa["latitude"], wa["longitude"]),
                             (wb["latitude"], wb["longitude"]),
                             f"validated and uploaded routes differ at waypoint {i}")
            self.assertEqual((wa["latitude"], wa["longitude"]),
                             (wc["latitude"], wc["longitude"]),
                             f"validated and packaged routes differ at waypoint {i}")

    def test_one_content_hash_covers_all_three(self):
        a, b, c, _ = self._routes()
        digest = mission_contract.route_content_hash(a)
        self.assertEqual(digest, mission_contract.route_content_hash(b))
        self.assertEqual(digest, mission_contract.route_content_hash(c))
        self.assertEqual(digest, self.record["route_hash"])

    def test_the_geometry_validated_is_the_geometry_serialized(self):
        _a, _b, _c, pkg = self._routes()
        validated = self.package["planning_inputs"]["navigable_boundary"]
        self.assertEqual(len(pkg["navigable_geometry"]), len(validated))
        for wire_ring, validated_ring in zip(pkg["navigable_geometry"], validated):
            self.assertEqual(len(wire_ring), len(validated_ring),
                             "the serialized ring has a different vertex count")
            for w, v in zip(wire_ring, validated_ring):
                self.assertEqual([round(w[0], 7), round(w[1], 7)],
                                 [round(v[0], 7), round(v[1], 7)])
        self.assertEqual(pkg["no_go_zones"], self.package["planning_inputs"]["no_go_zones"])
        self.assertEqual(pkg["home_corridor"], self.package["home_corridor"])

    def test_the_serialized_package_passes_the_scout_rule(self):
        # The end of the chain: the bytes Scout receives, checked by Scout's own rule.
        _a, _b, _c, pkg = self._routes()
        self.assertEqual(
            scout_equivalent_outside_legs(pkg["route_waypoints"], pkg["navigable_geometry"],
                                          pkg.get("home_corridor")),
            [], "the package the operator would send is one Scout would reject")

    def test_the_stored_record_re_validates_against_its_own_geometry(self):
        report = planning.check_mission_geometry(
            **planning.mission_geometry_arguments(self.record))
        self.assertTrue(report["ok"], report["failures"])
        self.assertTrue(report["checks"]["route_legs_exactly_within_approved"])


if __name__ == "__main__":
    unittest.main()
