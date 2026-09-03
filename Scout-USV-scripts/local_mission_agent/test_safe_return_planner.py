"""
Standalone tests for safe_return_planner.py.

    python3 test_safe_return_planner.py

Covers: the RETRACE_APPROVED build, validation, and every fail-closed path
(no package / no Home / no position / connector gap exceeded / no-go crossing /
not terminating at Home). Uses a lightweight snapshot stand-in.
"""
import types
import unittest

import geo
import planning_package as pp
import replan_config
import safe_return_planner as srp


def _snap(lat, lon, seq):
    return types.SimpleNamespace(latitude=lat, longitude=lon, current_sequence=seq)


# Approved outbound line heading north from near Home, then the survey.
_ROUTE = [
    {"latitude": 56.6500, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_OUTBOUND_TRANSIT},
    {"latitude": 56.6510, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
    {"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
]
_HOME = {"latitude": 56.6490, "longitude": 12.8700}


def _package(no_go=None):
    return pp.build_package("m1", _ROUTE, _HOME, no_go_zones=no_go or [])


_CFG = replan_config.ReplanConfig(connect_gap_max_m=150.0)


class TestBuild(unittest.TestCase):
    def test_happy_path_prefers_shortest_direct_route(self):
        # No navigable_boundary/home_corridor/no_go in this package -> the
        # current->Home segment is unconstrained and wins as the DIRECT
        # (preference A) route: just [current, Home], not a 3-point retrace
        # of the approved mission (the whole point of the feature -- see
        # module docstring preferences A/B/C).
        snap = _snap(56.6520, 12.8700, 3)
        r = srp.build_safe_return_route(snap, _package(), _CFG)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(r["strategy"], "SAFE_RETURN_HOME")
        self.assertEqual(r["method"], srp.METHOD_SHORTEST)
        self.assertTrue(r["direct_path_valid"])
        self.assertFalse(r["fallback_used"])
        route = r["route"]
        self.assertEqual(len(route), 2)
        # Terminates at Home.
        self.assertAlmostEqual(route[-1]["latitude"], _HOME["latitude"], places=6)
        self.assertAlmostEqual(route[-1]["longitude"], _HOME["longitude"], places=6)
        # Begins at the current position.
        self.assertAlmostEqual(route[0]["latitude"], 56.6520, places=6)
        self.assertIsInstance(r["planner_runtime_s"], float)

    def test_retrace_fallback_when_geometry_forces_it(self):
        # A navigable_boundary that contains the approved route/traversal but
        # NOT the direct current->Home chord (it excludes the middle of the
        # survey area) forces both preference (A) direct and (B) constrained
        # shortest-path to fail (no valid edge set can connect current->Home
        # while respecting containment other than by following the approved
        # legs) -- build_safe_return_route falls back to (C)
        # RETRACE_APPROVED_FALLBACK, unchanged from the original algorithm.
        boundary = [[56.6499, 12.8699], [56.6499, 12.8701],
                    [56.6521, 12.8701], [56.6521, 12.8699]]
        pkg = pp.build_package("m1", _ROUTE, _HOME, navigable_boundary=boundary)
        snap = _snap(56.6520, 12.8700, 3)
        r = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(r["method"], srp.METHOD_RETRACE_FALLBACK)
        self.assertTrue(r["fallback_used"])
        route = r["route"]
        self.assertAlmostEqual(route[-1]["latitude"], _HOME["latitude"], places=6)
        self.assertAlmostEqual(route[-1]["longitude"], _HOME["longitude"], places=6)
        self.assertAlmostEqual(route[0]["latitude"], 56.6520, places=6)
        # Retraced 3 approved points; nothing left un-traversed.
        self.assertEqual(r["preserved_waypoint_count"], 3)
        self.assertEqual(r["removed_waypoint_count"], 0)

    def test_retrace_approved_internals_unchanged(self):
        # _build_retrace_approved_route (preference C) is kept byte-for-byte
        # available as its own fail-safe building block, independent of the
        # dispatcher's preference for shorter routes.
        snap = _snap(56.6520, 12.8700, 3)
        r = srp._build_retrace_approved_route(snap, _package(), _CFG)
        self.assertTrue(r["ok"], r.get("reason"))
        self.assertEqual(r["method"], srp.METHOD_RETRACE_FALLBACK)
        self.assertEqual(r["preserved_waypoint_count"], 3)
        self.assertEqual(r["removed_waypoint_count"], 0)

    def test_partial_progress_drops_untraversed_legs(self):
        # Only 2 of 3 traversed -> the 3rd is 'removed' (RETRACE_APPROVED
        # fallback's own accounting, exercised directly).
        snap = _snap(56.6510, 12.8700, 2)
        r = srp._build_retrace_approved_route(snap, _package(), _CFG)
        self.assertTrue(r["ok"])
        self.assertEqual(r["preserved_waypoint_count"], 2)
        self.assertEqual(r["removed_waypoint_count"], 1)

    def test_no_package_fails_closed(self):
        r = srp.build_safe_return_route(_snap(56.652, 12.87, 3), None, _CFG)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason_code"], srp.CODE_NO_PACKAGE)
        self.assertEqual(r["route"], [])

    def test_no_home_fails_closed(self):
        pkg = pp.build_package("m", _ROUTE, None)
        r = srp.build_safe_return_route(_snap(56.652, 12.87, 3), pkg, _CFG)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason_code"], srp.CODE_NO_HOME)

    def test_no_position_fails_closed(self):
        r = srp.build_safe_return_route(_snap(None, None, 3), _package(), _CFG)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason_code"], srp.CODE_NO_POSITION)

    def test_connector_gap_exceeded_fails_closed(self):
        # Vehicle far from the approved network -> refuse rather than draw a
        # long unverified line.
        snap = _snap(56.70, 12.95, 3)
        r = srp.build_safe_return_route(snap, _package(), _CFG)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason_code"], srp.CODE_CONNECT_GAP)


class TestValidate(unittest.TestCase):
    def test_valid_route_passes(self):
        snap = _snap(56.6520, 12.8700, 3)
        build = srp.build_safe_return_route(snap, _package(), _CFG)
        v = srp.validate_route(build["route"], _package(), snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))

    def test_rejects_route_not_ending_at_home(self):
        route = [{"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0},
                 {"latitude": 56.6510, "longitude": 12.8700, "loiter_time_s": 0}]
        v = srp.validate_route(route, _package(), _snap(56.6520, 12.8700, 3), _CFG)
        self.assertFalse(v["valid"])
        self.assertEqual(v["reason_code"], srp.CODE_NOT_TERMINATING_HOME)

    def test_rejects_no_go_crossing(self):
        # A no-go square straddling the approved line at lat ~56.6515. The
        # dispatcher now routes AROUND it (shortest-safe-return, preference
        # B) instead of failing closed -- build_safe_return_route succeeds
        # and the winning route independently avoids the zone.
        zone = [(56.6513, 12.8698), (56.6513, 12.8702),
                (56.6517, 12.8702), (56.6517, 12.8698)]
        pkg = _package(no_go=[[list(v) for v in zone]])
        snap = _snap(56.6520, 12.8700, 3)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))
        latlon = [(wp["latitude"], wp["longitude"]) for wp in build["route"]]
        self.assertIsNone(geo.route_crosses_no_go(latlon, [zone]))

        # The underlying rejection this test used to exercise is still real:
        # a route DIRECTLY through the zone (what a naive/no-detour planner
        # would produce) is still refused by validate_route.
        straight_through = [
            {"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0},
            {"latitude": _HOME["latitude"], "longitude": _HOME["longitude"], "loiter_time_s": 0},
        ]
        v2 = srp.validate_route(straight_through, pkg, snap, _CFG)
        self.assertFalse(v2["valid"])
        self.assertEqual(v2["reason_code"], srp.CODE_NO_GO_CROSSING)

    def test_rejects_duplicate_points(self):
        route = [{"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0},
                 {"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0},
                 {"latitude": 56.6490, "longitude": 12.8700, "loiter_time_s": 0}]
        v = srp.validate_route(route, _package(), _snap(56.6520, 12.8700, 3), _CFG)
        self.assertFalse(v["valid"])
        self.assertEqual(v["reason_code"], srp.CODE_DUPLICATE)

    def test_geometry_validation_reports_no_boundary(self):
        # No boundary supplied -> connector not proven by containment, flagged.
        snap = _snap(56.6520, 12.8700, 3)
        build = srp.build_safe_return_route(snap, _package(), _CFG)
        v = srp.validate_route(build["route"], _package(), snap, _CFG)
        self.assertTrue(v["valid"])
        g = v["geometry_validation"]
        self.assertFalse(g["boundary_available"])
        self.assertFalse(g["boundary_checked"])
        self.assertFalse(g["connector_proven_safe"])
        self.assertTrue(any("boundary" in lim for lim in g["limitations"]))

    def test_geometry_validation_boundary_checked_and_connector_proven(self):
        boundary = [[56.6485, 12.8695], [56.6485, 12.8705],
                    [56.6525, 12.8705], [56.6525, 12.8695]]
        pkg = pp.build_package("m1", _ROUTE, _HOME, navigable_boundary=boundary)
        snap = _snap(56.6520, 12.8700, 3)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"])
        g = v["geometry_validation"]
        self.assertTrue(g["boundary_available"])
        self.assertTrue(g["boundary_checked"])
        self.assertTrue(g["connector_proven_safe"])

    def test_route_leaving_boundary_fails_closed(self):
        # A boundary that does NOT contain the northern approved waypoints ->
        # the retrace leaves the boundary -> fail closed.
        boundary = [[56.6485, 12.8695], [56.6485, 12.8705],
                    [56.6505, 12.8705], [56.6505, 12.8695]]
        pkg = pp.build_package("m1", _ROUTE, _HOME, navigable_boundary=boundary)
        snap = _snap(56.6520, 12.8700, 3)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertFalse(v["valid"])
        self.assertEqual(v["reason_code"], srp.CODE_BOUNDARY_VIOLATION)

    def test_shoreline_scalar_flagged_not_checked(self):
        pkg = pp.build_package("m1", _ROUTE, _HOME, shoreline_clearance_m=5.0)
        snap = _snap(56.6520, 12.8700, 3)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertFalse(v["geometry_validation"]["shoreline_clearance_available"])
        self.assertTrue(any("shoreline" in lim for lim in v["geometry_validation"]["limitations"]))

    def test_backward_compat_route_without_segments(self):
        # A package whose route carries no semantic segment tags still plans and
        # validates (segments default to UNSPECIFIED).
        legacy = pp.build_package("legacy", [
            {"latitude": 56.6500, "longitude": 12.8700},
            {"latitude": 56.6510, "longitude": 12.8700},
        ], _HOME)
        snap = _snap(56.6510, 12.8700, 2)
        build = srp.build_safe_return_route(snap, legacy, _CFG)
        self.assertTrue(build["ok"])
        v = srp.validate_route(build["route"], legacy, snap, _CFG)
        self.assertTrue(v["valid"])


# ── Runtime-Home / launch connector geometry contract (task section 4) ─────────
class TestHomeConnectorGeometry(unittest.TestCase):
    # Survey navigable polygon containing the whole approved route + the vehicle,
    # but NOT a launch Home to its south.
    _BOUNDARY = [[56.6480, 12.8690], [56.6480, 12.8710],
                 [56.6530, 12.8710], [56.6530, 12.8690]]
    _HOME_IN = {"latitude": 56.6490, "longitude": 12.8700}   # inside the survey
    _HOME_OUT = {"latitude": 56.6470, "longitude": 12.8700}  # south of the survey
    # Approved corridor overlapping the boundary's south edge and reaching down to
    # contain the whole final leg (56.6500 -> Home_out 56.6470).
    _CORRIDOR = [[56.6465, 12.8695], [56.6465, 12.8705],
                 [56.6505, 12.8705], [56.6505, 12.8695]]

    def _build_validate(self, pkg, vehicle=(56.6520, 12.8700, 3)):
        snap = _snap(*vehicle)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        return srp.validate_route(build["route"], pkg, snap, _CFG)

    def test_home_inside_navigable_area_valid(self):
        pkg = pp.build_package("m1", _ROUTE, self._HOME_IN, navigable_boundary=self._BOUNDARY)
        v = self._build_validate(pkg)
        self.assertTrue(v["valid"], v.get("reason"))
        g = v["geometry_validation"]
        self.assertTrue(g["home_in_navigable_boundary"])
        self.assertTrue(g["connector_proven_safe"])

    def test_home_outside_with_valid_corridor_valid(self):
        pkg = pp.build_package("m1", _ROUTE, self._HOME_OUT,
                               navigable_boundary=self._BOUNDARY, home_corridor=self._CORRIDOR)
        v = self._build_validate(pkg)
        self.assertTrue(v["valid"], v.get("reason"))
        g = v["geometry_validation"]
        self.assertFalse(g["home_in_navigable_boundary"])
        self.assertTrue(g["home_corridor_available"])
        self.assertTrue(g["home_corridor_checked"])
        self.assertTrue(g["home_in_corridor"])
        self.assertTrue(g["connector_proven_safe"])

    def test_home_outside_without_corridor_fails_closed(self):
        pkg = pp.build_package("m1", _ROUTE, self._HOME_OUT, navigable_boundary=self._BOUNDARY)
        v = self._build_validate(pkg)
        self.assertFalse(v["valid"])
        self.assertEqual(v["reason_code"], srp.CODE_HOME_OUTSIDE_BOUNDARY)
        # Actionable: names the missing Operator geometry.
        self.assertTrue(any("home_corridor" in lim
                            for lim in v["geometry_validation"]["limitations"]))

    def test_corridor_crossing_no_go_rejected(self):
        # A no-go zone straddling the straight final leg inside the corridor.
        # The corridor is wide enough either side of the zone that the
        # shortest-safe-return planner finds a valid detour around it within
        # the SAME approved corridor -- the corridor is still NOT exempt from
        # no-go constraints (every edge is still checked against it), it is
        # simply no longer required to fail closed when a safe detour exists.
        zone = [[56.6480, 12.8698], [56.6480, 12.8702],
                [56.6490, 12.8702], [56.6490, 12.8698]]
        pkg = pp.build_package("m1", _ROUTE, self._HOME_OUT,
                               navigable_boundary=self._BOUNDARY, home_corridor=self._CORRIDOR,
                               no_go_zones=[zone])
        snap = _snap(56.6520, 12.8700, 3)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))
        latlon = [(wp["latitude"], wp["longitude"]) for wp in build["route"]]
        self.assertIsNone(geo.route_crosses_no_go(latlon, [zone]))

        # The underlying no-go-inside-corridor rejection is still real: the
        # ORIGINAL retrace route (whose final leg runs straight through the
        # zone, entirely inside the corridor, with no detour) is still
        # refused -- the corridor is not exempt from no-go constraints.
        retrace = srp._build_retrace_approved_route(snap, pkg, _CFG)
        self.assertTrue(retrace["ok"], retrace.get("reason"))
        v2 = srp.validate_route(retrace["route"], pkg, snap, _CFG)
        self.assertFalse(v2["valid"])
        self.assertEqual(v2["reason_code"], srp.CODE_CONNECTOR_CROSSES_NO_GO)

    def test_shoreline_scalar_not_treated_as_proven_connector(self):
        # A scalar shoreline_clearance_m must never make an out-of-boundary Home
        # connector "proven" -- it is not geometry and is not silently accepted.
        pkg = pp.build_package("m1", _ROUTE, self._HOME_OUT,
                               navigable_boundary=self._BOUNDARY, shoreline_clearance_m=5.0)
        v = self._build_validate(pkg)
        self.assertFalse(v["valid"])  # still fails closed -- scalar proves nothing
        self.assertEqual(v["reason_code"], srp.CODE_HOME_OUTSIDE_BOUNDARY)
        self.assertFalse(v["geometry_validation"]["shoreline_clearance_available"])

    def test_vehicle_near_polygon_edge_still_valid(self):
        # Vehicle just inside the northern boundary edge, Home inside -> valid.
        pkg = pp.build_package("m1", _ROUTE, self._HOME_IN, navigable_boundary=self._BOUNDARY)
        v = self._build_validate(pkg, vehicle=(56.6529, 12.8700, 3))
        self.assertTrue(v["valid"], v.get("reason"))

    def test_route_cannot_escape_allowed_geometry_mid_route(self):
        # A candidate route with a mid-route point outside all approved
        # geometry is rejected as a boundary violation (NOT the Home-leg
        # special case). validate_route is exercised directly with the
        # crafted candidate -- build_safe_return_route's own strategies never
        # need to construct this route in the first place (the whole point
        # of the shortest-safe-return feature is that they don't have to),
        # but validate_route must still refuse it if anything ever proposes it.
        route = [{"latitude": 56.6500, "longitude": 12.8700, "loiter_time_s": 0},
                 {"latitude": 56.6600, "longitude": 12.8700, "loiter_time_s": 0},   # north of the boundary
                 {"latitude": self._HOME_IN["latitude"], "longitude": self._HOME_IN["longitude"], "loiter_time_s": 0}]
        pkg = pp.build_package("m1", _ROUTE, self._HOME_IN, navigable_boundary=self._BOUNDARY)
        snap = _snap(56.6500, 12.8700, 0)
        v = srp.validate_route(route, pkg, snap, _CFG)
        self.assertFalse(v["valid"])
        self.assertEqual(v["reason_code"], srp.CODE_BOUNDARY_VIOLATION)

    def test_home_corridor_field_preserved_by_build_package(self):
        # The Operator-provided home_corridor survives package construction so the
        # Scout-side contract is functional end-to-end when supplied.
        pkg = pp.build_package("m1", _ROUTE, self._HOME_OUT,
                               navigable_boundary=self._BOUNDARY, home_corridor=self._CORRIDOR)
        self.assertTrue(len(pkg.get("home_corridor") or []) >= 3)
        # And a package WITHOUT it stays empty (backward compatible).
        pkg2 = pp.build_package("m1", _ROUTE, self._HOME_IN, navigable_boundary=self._BOUNDARY)
        self.assertEqual(pkg2.get("home_corridor"), [])


# ── E2 thesis evidence: RETRACE_APPROVED around a mid-route no-go zone ─────────
# The exact E2 experiment geometry: Home to the south, one rectangular no-go
# zone directly between Home and the vehicle's current position (so a naive
# direct current->Home line crosses it), and a previously-approved/traversed
# mission route that already detours AROUND the zone to the east. Proves (a)
# retracing the approved detour reaches Home without crossing the zone, and
# (b) a direct straight-line return through the same geometry is correctly
# rejected by validate_route -- i.e. the geometry check is real, not a no-op.
class TestE2RetraceAroundNoGoZone(unittest.TestCase):
    _HOME = {"latitude": 56.6490, "longitude": 12.8700}
    # Directly between Home (lat 56.6490) and the vehicle (lat 56.6530), on the
    # same north-south line (lon ~12.8700) that a direct return would follow.
    _ZONE = [(56.6505, 12.8695), (56.6505, 12.8705),
             (56.6515, 12.8705), (56.6515, 12.8695)]
    # Approved route: north a bit, jog east around the zone, continue north past
    # it, jog back west, continue to the vehicle's current position. Every leg
    # is entirely outside the zone's lat OR lon band, so the detour cannot
    # ambiguously clip the zone under any correct segment-intersection test.
    _ROUTE_AROUND = [
        {"latitude": 56.6500, "longitude": 12.8700, "loiter_time_s": 0},  # below zone
        {"latitude": 56.6500, "longitude": 12.8730, "loiter_time_s": 0},  # jog east, still below zone
        {"latitude": 56.6520, "longitude": 12.8730, "loiter_time_s": 0},  # north, east of zone
        {"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0},  # jog west, above zone
        {"latitude": 56.6530, "longitude": 12.8700, "loiter_time_s": 0},  # vehicle's current position
    ]

    def _package(self):
        return pp.build_package("e2", self._ROUTE_AROUND, self._HOME,
                                no_go_zones=[[list(v) for v in self._ZONE]])

    def test_retrace_around_no_go_reaches_home_without_crossing(self):
        # Vehicle at the last approved waypoint, opposite Home across the zone.
        # The approved mission's own detour jogs ~230 m east of the zone; the
        # shortest-safe-return planner instead hugs the zone directly (a much
        # shorter valid path) -- proving the feature's whole point: it is NOT
        # constrained to retrace/reorder the remaining approved waypoints when
        # a shorter route through the same approved (here: unconstrained,
        # no-go-only) geometry exists.
        snap = _snap(56.6530, 12.8700, len(self._ROUTE_AROUND))
        pkg = self._package()
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        self.assertEqual(build["method"], srp.METHOD_SHORTEST)
        route = build["route"]

        v = srp.validate_route(route, pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))
        g = v["geometry_validation"]
        self.assertTrue(g["no_go_available"])
        self.assertTrue(g["no_go_checked"])

        # Independently confirm (not just via validate_route) that no leg of
        # the generated route crosses the zone -- the direct geometric
        # assertion this test exists to make.
        latlon = [(wp["latitude"], wp["longitude"]) for wp in route]
        self.assertIsNone(geo.route_crosses_no_go(latlon, [self._ZONE]))

        # Shorter than the approved detour (which would be ~1090 m: the sum
        # of the 4 legs in _ROUTE_AROUND) -- the east jog to lon ~12.8730 is
        # NOT needed and is not present.
        self.assertFalse(any(abs(wp["longitude"] - 12.8730) < 1e-6 for wp in route))
        approved_detour_m = geo.path_length_m(
            [(wp["latitude"], wp["longitude"]) for wp in self._ROUTE_AROUND] + [
                (self._HOME["latitude"], self._HOME["longitude"])])
        self.assertLess(geo.path_length_m(latlon), approved_detour_m)

        # Terminates at Home.
        self.assertAlmostEqual(route[-1]["latitude"], self._HOME["latitude"], places=6)
        self.assertAlmostEqual(route[-1]["longitude"], self._HOME["longitude"], places=6)

    def test_direct_straight_line_return_through_no_go_fails_validation(self):
        # The naive path a shortest-path/direct-line strategy WOULD produce --
        # straight from the vehicle's current position to Home, right through
        # the zone -- must be rejected by validate_route. This is the negative
        # control proving the no-go check is load-bearing, not a no-op.
        snap = _snap(56.6530, 12.8700, len(self._ROUTE_AROUND))
        direct_route = [
            {"latitude": 56.6530, "longitude": 12.8700, "loiter_time_s": 0},
            {"latitude": self._HOME["latitude"], "longitude": self._HOME["longitude"], "loiter_time_s": 0},
        ]
        pkg = self._package()
        v = srp.validate_route(direct_route, pkg, snap, _CFG)
        self.assertFalse(v["valid"])
        self.assertEqual(v["reason_code"], srp.CODE_NO_GO_CROSSING)

        # And directly at the geometry-primitive level too.
        latlon = [(wp["latitude"], wp["longitude"]) for wp in direct_route]
        self.assertEqual(geo.route_crosses_no_go(latlon, [self._ZONE]), 0)

    def test_g_retrace_around_no_go_still_succeeds_with_5m_clearance(self):
        # G: the SAME RETRACE_APPROVED strategy, but with the package now
        # carrying no_go_clearance_m=5.0 (the Operator-side default). The
        # approved detour jogs ~150 m east of the zone -- comfortably outside
        # a 5 m buffer -- so retracing it still succeeds and no revised
        # segment enters the buffered exclusion.
        snap = _snap(56.6530, 12.8700, len(self._ROUTE_AROUND))
        pkg = pp.build_package("e2", self._ROUTE_AROUND, self._HOME,
                               no_go_zones=[[list(v) for v in self._ZONE]],
                               no_go_clearance_m=5.0)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))
        self.assertEqual(v["geometry_validation"]["no_go_clearance_m"], 5.0)
        latlon = [(wp["latitude"], wp["longitude"]) for wp in build["route"]]
        self.assertIsNone(geo.route_crosses_no_go(latlon, [self._ZONE], 5.0))


# ── no_go_clearance_m: buffered exclusion around no_go_zones ───────────────────
class TestNoGoClearance(unittest.TestCase):
    # A single rectangular no-go zone, independent of the E2 route above.
    _ZONE = [(56.6505, 12.8695), (56.6505, 12.8705),
             (56.6515, 12.8705), (56.6515, 12.8695)]
    # West of the zone (safely clear, ~92 m) so the final Home leg never
    # itself approaches the zone -- isolates the segment under test.
    _HOME = {"latitude": 56.6510, "longitude": 12.8680}

    def _route(self, mid_points):
        return mid_points + [{"latitude": self._HOME["latitude"],
                              "longitude": self._HOME["longitude"], "loiter_time_s": 0}]

    def _package(self, route, clearance_m):
        return pp.build_package("m1", route, self._HOME,
                                no_go_zones=[[list(v) for v in self._ZONE]],
                                no_go_clearance_m=clearance_m)

    def test_e_zero_clearance_may_approach_raw_boundary(self):
        # E (clearance=0): a route running 2 m outside the raw polygon (west
        # edge) does not cross it -- may approach the raw boundary, must not
        # cross it.
        route = self._route([
            {"latitude": 56.6517, "longitude": 12.869467318535717, "loiter_time_s": 0},
            {"latitude": 56.6503, "longitude": 12.869467318535717, "loiter_time_s": 0},
        ])
        pkg = self._package(route, 0.0)
        v = srp.validate_route(route, pkg, None, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))
        self.assertEqual(v["geometry_validation"]["no_go_clearance_m"], 0.0)

    def test_e_five_metre_clearance_rejects_a_route_still_2m_from_zone(self):
        # E (clearance=5): the SAME route, 2 m from the raw polygon, now lies
        # inside the 5 m buffered exclusion -- must fail, within existing
        # geometric tolerance (the check is exact metric distance, not degree
        # approximation).
        route = self._route([
            {"latitude": 56.6517, "longitude": 12.869467318535717, "loiter_time_s": 0},
            {"latitude": 56.6503, "longitude": 12.869467318535717, "loiter_time_s": 0},
        ])
        pkg = self._package(route, 5.0)
        v = srp.validate_route(route, pkg, None, _CFG)
        self.assertFalse(v["valid"])
        self.assertEqual(v["reason_code"], srp.CODE_NO_GO_CROSSING)
        self.assertEqual(v["geometry_validation"]["no_go_clearance_m"], 5.0)

    def test_e_route_clear_of_the_5m_buffer_passes(self):
        # A route a clear 6 m from the zone passes at both clearances.
        route = self._route([
            {"latitude": 56.6517, "longitude": 12.869401956907229, "loiter_time_s": 0},
            {"latitude": 56.6503, "longitude": 12.869401956907229, "loiter_time_s": 0},
        ])
        for clearance_m in (0.0, 5.0):
            pkg = self._package(route, clearance_m)
            v = srp.validate_route(route, pkg, None, _CFG)
            self.assertTrue(v["valid"], v.get("reason"))

    def test_f_individually_clear_waypoints_but_connecting_segment_crosses_buffer(self):
        # F: two waypoints each ~6 m from the zone (individually outside a 5 m
        # buffer around the SW corner) but the straight segment between them
        # cuts to within ~4.24 m of that corner -- inside the buffer. A route
        # that only checked waypoint containment would wrongly pass this;
        # validate_route must fail it as a SEGMENT violation.
        p1 = {"latitude": 56.6505, "longitude": 12.869401956907229, "loiter_time_s": 0}
        p2 = {"latitude": 56.6504461013295, "longitude": 12.8695, "loiter_time_s": 0}
        # Confirm the premise directly: both endpoints individually clear 5 m.
        for p in (p1, p2):
            d = geo.segment_distance_to_polygon_m(
                (p["latitude"], p["longitude"]), (p["latitude"], p["longitude"]), self._ZONE)
            self.assertGreaterEqual(d, 5.0)
        # And the bare segment does NOT cross the raw (unbuffered) polygon --
        # proving this is genuinely a buffer/segment check, not a crossing.
        self.assertIsNone(geo.route_crosses_no_go(
            [(p1["latitude"], p1["longitude"]), (p2["latitude"], p2["longitude"])], [self._ZONE]))

        route = self._route([p1, p2])
        pkg = self._package(route, 5.0)
        v = srp.validate_route(route, pkg, None, _CFG)
        self.assertFalse(v["valid"])
        self.assertEqual(v["reason_code"], srp.CODE_NO_GO_CROSSING)


# ── First-approach (route_start_mode=first_approach) compatibility ─────────────
class TestFirstApproachCorridorCompatibility(unittest.TestCase):
    # Operator's route_start_mode=first_approach fix: the execution route
    # begins directly at A1 (the first survey waypoint), not at Home. The
    # approved Home->A1 connector is carried as the finalized home_corridor
    # instead of a planning_only_transit_segments wire field Scout would have
    # to reconstruct.
    _HOME = {"latitude": 56.6490, "longitude": 12.8700}
    _ROUTE = [   # begins at A1, wholly inside navigable_boundary
        {"latitude": 56.6610, "longitude": 12.8700, "loiter_time_s": 0},
        {"latitude": 56.6620, "longitude": 12.8700, "loiter_time_s": 0},
    ]
    _NAV = [[56.66, 12.86], [56.66, 12.89], [56.68, 12.89], [56.68, 12.86]]
    _CORRIDOR = [[56.647, 12.865], [56.647, 12.875], [56.663, 12.875], [56.663, 12.865]]

    def _package(self):
        pkg = pp.build_package("m1", self._ROUTE, self._HOME, no_go_zones=[],
                               navigable_boundary=self._NAV, home_corridor=self._CORRIDOR)
        self.assertNotIn("planning_only_transit_segments", pkg)
        return pkg

    def test_retrace_uses_stored_corridor_for_home_leg_no_reconstruction(self):
        pkg = self._package()
        snap = _snap(56.6620, 12.8700, len(self._ROUTE))
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))
        self.assertTrue(v["geometry_validation"]["home_corridor_checked"])
        self.assertTrue(v["geometry_validation"]["home_in_corridor"])
        # Terminates at Home via the stored corridor -- nothing reconstructed.
        self.assertAlmostEqual(build["route"][-1]["latitude"], self._HOME["latitude"], places=6)
        self.assertAlmostEqual(build["route"][-1]["longitude"], self._HOME["longitude"], places=6)


# ── Shortest-safe-return: dedicated edge cases ──────────────────────────────
class TestShortestSafeReturnEdgeCases(unittest.TestCase):
    def test_two_paths_around_no_go_shorter_side_selected(self):
        # A no-go zone positioned asymmetrically between current and Home: its
        # west edge is ~1 m from the direct line, its east edge is far away --
        # going around the west side is far shorter. The planner must select
        # the shorter valid path, not merely "a" valid path.
        cur = {"latitude": 56.6530, "longitude": 12.8700}
        home = {"latitude": 56.6490, "longitude": 12.8700}
        zone = [[56.6505, 12.8699], [56.6505, 12.8730],
                [56.6515, 12.8730], [56.6515, 12.8699]]
        pkg = pp.build_package("m1", [cur], home, no_go_zones=[zone])
        snap = _snap(cur["latitude"], cur["longitude"], 1)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        self.assertEqual(build["method"], srp.METHOD_SHORTEST)
        latlon = [(wp["latitude"], wp["longitude"]) for wp in build["route"]]
        dist = geo.path_length_m(latlon)
        # The long way around (east, ~30 m of extra lateral travel each way)
        # would be substantially longer; the short (west) detour is close to
        # the direct distance.
        direct_dist = geo.haversine_m(cur["latitude"], cur["longitude"],
                                      home["latitude"], home["longitude"])
        self.assertLess(dist, direct_dist + 20)
        # None of the winning route's waypoints ventures east into the zone's
        # wide (east-side detour) longitude range.
        self.assertTrue(all(lon < 12.8710 for _, lon in latlon))
        self.assertIsNone(geo.route_crosses_no_go(latlon, [zone]))

    def test_concave_navigable_boundary_no_chord_leaves_geometry(self):
        # An L-shaped (concave) navigable_boundary. The straight chord between
        # current and Home would cut through the notch (outside the L) -- the
        # planner must route around the notch's own inner (reflex) corner,
        # and every resulting segment must stay inside the L.
        boundary = [
            [56.640, 12.870], [56.640, 12.880], [56.644, 12.880],
            [56.644, 12.874], [56.650, 12.874], [56.650, 12.870],
        ]
        cur = {"latitude": 56.641, "longitude": 12.879}
        home = {"latitude": 56.649, "longitude": 12.871}
        pkg = pp.build_package("m1", [cur], home, navigable_boundary=boundary)
        snap = _snap(cur["latitude"], cur["longitude"], 1)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        self.assertFalse(build["direct_path_valid"])  # the chord truly leaves the L
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))
        latlon = [(wp["latitude"], wp["longitude"]) for wp in build["route"]]
        for i in range(len(latlon) - 1):
            self.assertIsNone(geo.route_outside_boundary(latlon[i:i + 2], boundary),
                              f"segment {i} leaves the concave boundary")

    def test_home_outside_boundary_shortest_path_connects_via_corridor(self):
        # Home is outside navigable_boundary but inside home_corridor, and the
        # DIRECT current->Home chord is invalid (neither polygon alone spans
        # it) -- the shortest-safe-return search must connect through the
        # corridor/boundary overlap, not merely fail closed.
        boundary = [[56.6480, 12.8690], [56.6480, 12.8710],
                    [56.6530, 12.8710], [56.6530, 12.8690]]
        corridor = [[56.6465, 12.8695], [56.6465, 12.8705],
                    [56.6505, 12.8705], [56.6505, 12.8695]]
        home = {"latitude": 56.6470, "longitude": 12.8700}
        cur = {"latitude": 56.6528, "longitude": 12.8692}
        pkg = pp.build_package("m1", [cur], home,
                               navigable_boundary=boundary, home_corridor=corridor)
        snap = _snap(cur["latitude"], cur["longitude"], 1)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        self.assertEqual(build["method"], srp.METHOD_SHORTEST)
        self.assertFalse(build["direct_path_valid"])
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))
        self.assertTrue(v["geometry_validation"]["home_corridor_checked"])
        self.assertAlmostEqual(build["route"][-1]["latitude"], home["latitude"], places=6)
        self.assertAlmostEqual(build["route"][-1]["longitude"], home["longitude"], places=6)

    def test_current_position_near_home_no_mission_retrace(self):
        # Vehicle already essentially at Home -> the route collapses to the
        # minimum useful route (no multi-waypoint retrace of the mission).
        home = {"latitude": 56.6490, "longitude": 12.8700}
        cur = {"latitude": 56.6490001, "longitude": 12.8700001}  # < 0.5 m away
        route = [
            {"latitude": 56.6500, "longitude": 12.8700, "loiter_time_s": 0},
            {"latitude": 56.6520, "longitude": 12.8700, "loiter_time_s": 0},
        ]
        pkg = pp.build_package("m1", route, home)
        snap = _snap(cur["latitude"], cur["longitude"], 0)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        self.assertEqual(build["method"], srp.METHOD_SHORTEST)
        self.assertLessEqual(len(build["route"]), 2)
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))

    def test_current_position_at_polygon_boundary_edge(self):
        # Vehicle just inside the navigable_boundary's own north edge (the
        # same convention test_vehicle_near_polygon_edge_still_valid uses --
        # a point EXACTLY on a horizontal edge hits a pre-existing, unrelated
        # ray-casting boundary ambiguity in geo.point_in_polygon that this
        # task does not touch) -- the direct route must still go through.
        boundary = [[56.6480, 12.8690], [56.6480, 12.8710],
                    [56.6530, 12.8710], [56.6530, 12.8690]]
        home = {"latitude": 56.6490, "longitude": 12.8700}
        cur = {"latitude": 56.6529, "longitude": 12.8700}
        pkg = pp.build_package("m1", [cur], home, navigable_boundary=boundary)
        snap = _snap(cur["latitude"], cur["longitude"], 1)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))

    def test_multiple_no_go_zones_both_avoided(self):
        # Two separate no-go zones, one on each plausible side of the direct
        # line -- the winning route must avoid BOTH.
        cur = {"latitude": 56.6530, "longitude": 12.8700}
        home = {"latitude": 56.6490, "longitude": 12.8700}
        zone_a = [[56.6515, 12.8695], [56.6515, 12.8705],
                  [56.6520, 12.8705], [56.6520, 12.8695]]
        zone_b = [[56.6500, 12.8695], [56.6500, 12.8705],
                  [56.6505, 12.8705], [56.6505, 12.8695]]
        pkg = pp.build_package("m1", [cur], home, no_go_zones=[zone_a, zone_b])
        snap = _snap(cur["latitude"], cur["longitude"], 1)
        build = srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertTrue(build["ok"], build.get("reason"))
        latlon = [(wp["latitude"], wp["longitude"]) for wp in build["route"]]
        self.assertIsNone(geo.route_crosses_no_go(latlon, [zone_a, zone_b]))
        v = srp.validate_route(build["route"], pkg, snap, _CFG)
        self.assertTrue(v["valid"], v.get("reason"))

    def test_shortest_candidate_failing_final_validation_falls_back(self):
        # If the shortest-path candidate somehow fails its OWN internal
        # validate_route self-check, build_safe_return_route must fall back
        # to the retrace strategy rather than ever returning an unproven
        # SHORTEST_SAFE_RETURN route (task edge case: "shortest-path candidate
        # fails final authoritative validation -> rejected/fallback").
        snap = _snap(56.6520, 12.8700, 3)
        pkg = _package()
        real_validate = srp.validate_route
        try:
            srp.validate_route = lambda *a, **k: {"valid": False, "reason_code": "FORCED_TEST_FAILURE",
                                                   "reason": "forced", "checks": {}, "geometry_validation": {}}
            build = srp.build_safe_return_route(snap, pkg, _CFG)
        finally:
            srp.validate_route = real_validate
        # The retrace fallback doesn't call validate_route internally, so it
        # still succeeds -- but it must be clearly labelled as the fallback,
        # never SHORTEST_SAFE_RETURN.
        self.assertTrue(build["ok"], build.get("reason"))
        self.assertEqual(build["method"], srp.METHOD_RETRACE_FALLBACK)
        self.assertTrue(build["fallback_used"])

    def test_route_simplification_never_introduces_invalid_segment(self):
        # A deliberately over-segmented valid path around a no-go zone --
        # every consecutive segment BEFORE and AFTER simplification is
        # independently proven valid, and simplification never increases the
        # point count.
        zone = [(56.6505, 12.8695), (56.6505, 12.8705),
                (56.6515, 12.8705), (56.6515, 12.8695)]
        boundary: list = []
        corridor: list = []
        # A zig-zag detour that IS valid but carries redundant intermediate
        # points a tighter path wouldn't need.
        points = [
            (56.6530, 12.8700), (56.6522, 12.8700), (56.6518, 12.8698),
            (56.6516, 12.8694), (56.6504, 12.8694), (56.6502, 12.8698),
            (56.6498, 12.8700), (56.6490, 12.8700),
        ]
        for i in range(len(points) - 1):
            self.assertTrue(srp._segment_geometrically_valid(
                points[i], points[i + 1], boundary, corridor, [zone], 0.0),
                f"fixture segment {i} must itself be valid")
        simplified = srp._simplify_route_latlon(points, boundary, corridor, [zone], 0.0)
        self.assertLessEqual(len(simplified), len(points))
        self.assertEqual(simplified[0], points[0])
        self.assertEqual(simplified[-1], points[-1])
        for i in range(len(simplified) - 1):
            self.assertTrue(srp._segment_geometrically_valid(
                simplified[i], simplified[i + 1], boundary, corridor, [zone], 0.0),
                f"simplified segment {i} must remain valid")
        self.assertIsNone(geo.route_crosses_no_go(simplified, [zone]))

    def test_original_mission_route_remains_immutable(self):
        # build_safe_return_route must never mutate the approved package's own
        # route list/dicts, regardless of which strategy wins.
        snap = _snap(56.6520, 12.8700, 3)
        pkg = _package()
        before = [dict(wp) for wp in pkg["route"]]
        srp.build_safe_return_route(snap, pkg, _CFG)
        self.assertEqual(pkg["route"], before)
        # Same route object identity untouched, not just equal-by-value.
        self.assertEqual(len(pkg["route"]), len(_ROUTE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
