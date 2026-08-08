"""The approved Home corridor: derivation, refusal, and its place on the v1 wire package.

Run from operator-scripts/:  python -m unittest tests.test_home_corridor

WHAT THIS PINS
--------------
Scout's `replan-planning-package-v1` accepts an OPTIONAL `home_corridor`: one implicitly-closed
`[[longitude, latitude], ...]` ring proving a safe connector between the approved navigable area
and the launch/Home area, for the common case where Home sits outside the survey polygon.

The whole safety of the feature is in what the Operator REFUSES to emit. A corridor is derived
from geometry the operator already approved — the transit/connector segments this station
generated, validated against the navigable area and the no-go zones, and uploaded — and from
nothing else. So these tests pin both directions:

  • a real record with approved transit geometry yields a corridor that contains the planning
    Home, overlaps the navigable area, clears the no-go zones, and is serialized [lon, lat];
  • every failed requirement yields NO corridor and a named reason, the key is OMITTED from the
    package (not null, not an empty ring), and Scout is therefore left to fail closed in LOITER;
  • the corridor is never widened, re-anchored or invented to reach a runtime Home.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import planning  # noqa: E402
import replan_package  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "active-original-msn-329c2faff137.json")


def real_record():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def corridor_of(record):
    return replan_package.derive_home_corridor(record)


@unittest.skipUnless(planning.PLANNING_AVAILABLE,
                     "the geometry stack (shapely/pyproj/numpy) is not installed")
class DerivationTests(unittest.TestCase):
    """The corridor the real, operator-approved record actually produces."""

    def test_a_record_with_approved_transit_geometry_yields_a_corridor(self):
        ring, meta = corridor_of(real_record())
        self.assertIsNotNone(ring, meta.get("reason"))
        self.assertTrue(meta["available"])
        self.assertIsNone(meta["reason"])

    def test_the_ring_is_a_single_implicitly_closed_polygon_of_at_least_three_vertices(self):
        ring, _ = corridor_of(real_record())
        self.assertGreaterEqual(len({tuple(p) for p in ring}), 3)
        # IMPLICITLY closed: the first vertex is not repeated at the end.
        self.assertNotEqual(ring[0], ring[-1])

    def test_wire_coordinates_are_longitude_latitude(self):
        # The survey sits near 56.679 N, 12.811 E — so lon is the ~12 and lat the ~56. Getting
        # this the wrong way round would place the corridor in the Indian Ocean and Scout would
        # reject the package; asserting the ORDER explicitly is what keeps it honest.
        ring, _ = corridor_of(real_record())
        for lon, lat in ring:
            self.assertAlmostEqual(lon, 12.811, delta=0.01)
            self.assertAlmostEqual(lat, 56.679, delta=0.01)

    def test_the_corridor_contains_the_planning_home(self):
        ring, meta = corridor_of(real_record())
        self.assertTrue(meta["contains_planning_home"])
        home = real_record()["planning_inputs"]["planning_home"]
        self.assertTrue(_ring_contains(ring, home), "the planning Home is outside the corridor")

    def test_the_corridor_overlaps_the_navigable_area_and_clears_the_no_go_zones(self):
        _, meta = corridor_of(real_record())
        self.assertTrue(meta["overlaps_navigable"])
        self.assertTrue(meta["clears_no_go_zones"])

    def test_the_corridor_is_built_only_from_transit_segments_never_the_survey(self):
        _, meta = corridor_of(real_record())
        self.assertTrue(set(meta["source_segment_kinds"])
                        <= set(planning.HOME_CORRIDOR_SOURCE_KINDS))
        # Buffering the coverage passes would produce a "corridor" covering the whole site.
        self.assertNotIn("primary", meta["source_segment_kinds"])
        self.assertNotIn("secondary", meta["source_segment_kinds"])

    def test_the_width_is_a_stated_number_not_an_emergent_one(self):
        _, meta = corridor_of(real_record())
        self.assertEqual(meta["half_width_m"], planning.HOME_CORRIDOR_HALF_WIDTH_M)

    def test_derivation_is_deterministic(self):
        a, _ = corridor_of(real_record())
        b, _ = corridor_of(real_record())
        self.assertEqual(a, b)

    def test_derivation_does_not_mutate_the_record(self):
        rec = real_record()
        before = json.dumps(rec, sort_keys=True)
        corridor_of(rec)
        self.assertEqual(json.dumps(rec, sort_keys=True), before)


@unittest.skipUnless(planning.PLANNING_AVAILABLE, "geometry stack not installed")
class RefusalTests(unittest.TestCase):
    """Every way a corridor is NOT proven. Each one must yield None and a named reason — never
    a best-effort ring — so Scout fails closed rather than returning through unapproved water."""

    def test_no_transit_segments_means_no_corridor(self):
        rec = real_record()
        rec["segments"] = [s for s in rec["segments"] if s["kind"] in ("primary", "secondary")]
        ring, meta = corridor_of(rec)
        self.assertIsNone(ring)
        self.assertIn("transit", meta["reason"])

    def test_no_planning_home_means_no_corridor(self):
        rec = real_record()
        rec["planning_inputs"]["planning_home"] = None
        ring, meta = corridor_of(rec)
        self.assertIsNone(ring)
        self.assertIn("planning home", meta["reason"])

    def test_no_navigable_geometry_means_no_corridor(self):
        rec = real_record()
        rec["navigable_geometry"] = []
        rec["planning_inputs"]["navigable_boundary"] = []
        ring, meta = corridor_of(rec)
        self.assertIsNone(ring)
        self.assertIn("navigable", meta["reason"])

    def test_a_home_far_from_the_approved_transit_path_is_not_covered(self):
        # THE case the contract exists to refuse: a Home the approved geometry says nothing
        # about. The corridor is NOT re-anchored or widened to reach it.
        rec = real_record()
        rec["planning_inputs"]["planning_home"] = [12.9000000, 56.7000000]
        ring, meta = corridor_of(rec)
        self.assertIsNone(ring)
        self.assertFalse(meta["contains_planning_home"])
        self.assertIn("does not contain the planning Home", meta["reason"])

    def test_a_no_go_zone_across_the_transit_path_refuses_the_corridor(self):
        rec = real_record()
        # A zone straddling the approach/return legs between Home and the survey entry.
        rec["no_go_zones"] = [[
            [12.81100, 56.67905], [12.81125, 56.67905],
            [12.81125, 56.67915], [12.81100, 56.67915],
        ]]
        ring, meta = corridor_of(rec)
        self.assertIsNone(ring)
        self.assertFalse(meta["clears_no_go_zones"])
        self.assertIn("no-go zone", meta["reason"])


@unittest.skipUnless(planning.PLANNING_AVAILABLE, "geometry stack not installed")
class PackageSerializationTests(unittest.TestCase):
    """How the corridor rides — and does not ride — on the v1 wire package."""

    def test_a_proven_corridor_is_carried_on_the_package(self):
        pkg, meta = replan_package.build_v1_package(real_record())
        self.assertIn("home_corridor", pkg)
        self.assertTrue(meta["home_corridor_supplied"])
        self.assertEqual(len(pkg["home_corridor"]), meta["home_corridor_vertex_count"])
        for pair in pkg["home_corridor"]:
            self.assertEqual(len(pair), 2)
            self.assertIsInstance(pair[0], float)
            self.assertIsInstance(pair[1], float)

    def test_the_corridor_sits_in_the_declared_wire_order(self):
        pkg, _ = replan_package.build_v1_package(real_record())
        self.assertEqual(list(pkg), [k for k in replan_package.V1_FIELD_ORDER if k in pkg])
        keys = list(pkg)
        self.assertLess(keys.index("no_go_zones"), keys.index("home_corridor"))
        self.assertLess(keys.index("home_corridor"), keys.index("route_waypoints"))

    def test_an_unproven_corridor_is_OMITTED_not_null_and_not_empty(self):
        rec = real_record()
        rec["planning_inputs"]["planning_home"] = [12.9000000, 56.7000000]
        pkg, meta = replan_package.build_v1_package(rec)
        self.assertNotIn("home_corridor", pkg)          # absent, so Scout fails closed
        self.assertFalse(meta["home_corridor_supplied"])
        self.assertTrue(any("fail closed" in lim for lim in meta["limitations"]))

    def test_the_corridor_never_changes_the_route_or_its_hash(self):
        # The corridor is additional approved GEOMETRY. It must not move a single byte Scout
        # verifies the route against, or the package would fail PLANNING_PACKAGE_HASH_MISMATCH.
        rec = real_record()
        with_corridor, _ = replan_package.build_v1_package(rec)
        no_home = real_record()
        no_home["planning_inputs"]["planning_home"] = [12.9, 56.7]
        without, _ = replan_package.build_v1_package(no_home)
        self.assertEqual(with_corridor["route_hash"], without["route_hash"])
        self.assertEqual(with_corridor["route_waypoints"], without["route_waypoints"])

    def test_a_caller_supplied_corridor_is_validated_not_trusted(self):
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(real_record(), home_corridor=[[12.81, 56.67]])
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(
                real_record(), home_corridor=[[999.0, 56.67], [12.81, 56.67], [12.82, 56.68]])

    def test_the_package_stays_deterministic_with_the_corridor(self):
        a, _ = replan_package.build_v1_package(real_record())
        b, _ = replan_package.build_v1_package(real_record())
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


class WithoutTheGeometryStackTests(unittest.TestCase):
    """A backend without shapely/pyproj cannot CHECK a corridor, so it must not emit one."""

    def test_no_geometry_stack_means_no_corridor_and_a_named_reason(self):
        real = planning.PLANNING_AVAILABLE
        planning.PLANNING_AVAILABLE = False
        try:
            ring, meta = corridor_of(real_record())
        finally:
            planning.PLANNING_AVAILABLE = real
        self.assertIsNone(ring)
        self.assertFalse(meta["available"])
        self.assertIn("geometry stack", meta["reason"])


def _ring_contains(ring, point):
    """Ray-casting point-in-ring, written here rather than imported so the assertion does not
    lean on the same library the code under test uses."""
    lon, lat = float(point[0]), float(point[1])
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xin = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < xin:
                inside = not inside
    return inside


if __name__ == "__main__":
    unittest.main()
