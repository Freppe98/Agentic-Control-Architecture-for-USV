"""Boustrophedon cellular decomposition of the coverage sweep (planning._bcd_cells).

Run from operator-scripts/:  python -m unittest tests.test_bcd_coverage

THE REGRESSION THIS FILE PINS
-----------------------------
Coverage fragments used to be flown in a strict global (row, along_index) order: every fragment
of lane N before lane N+1. Where an exclusion splits a lane, that order forces the route to leave
one half, travel around the exclusion, fly the other half, and come back around on the next lane —
one long bypass staircase PER SPLIT LANE, its cross-lane legs sweeping repeatedly up and down
beside the obstacle and crossing water already surveyed.

Fragments are now grouped into CELLS at the sweep's critical points (1 fragment -> 2+, or 2+ -> 1,
or a break in the sweep rows) and each cell is covered completely before the next. The obstacle is
then crossed ONCE, at the hand-over between the two columns beside it, instead of once per lane.

WHAT IS ASSERTED
----------------
  * same_lane_obstacle_bridge_count == 0 — the oscillation is gone (the headline guarantee);
  * cells are covered completely and contiguously, and are sweep-monotonic inside;
  * coverage is COMPLETE and the clipped lane content is unchanged by the reordering — the same
    lane fragments, the same total survey line, only a different visiting order;
  * shoreline clearance, no-go clearance and the buffered exclusion are untouched;
  * every emitted leg is still re-proven safe with planning's own predicates;
  * generation is deterministic (identical route, identical hash across repeats).

Geometries cover the matrix the investigation used: no obstacle, rotated lanes, a central no-go
splitting many lanes, a narrow corridor, two separated zones, a concave boundary, a no-go tilted
in the survey frame, and an irregular boundary with two zones.
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import planning  # noqa: E402

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

# ── Geometry helpers: build the fixtures in METRES about a local origin ──────────────────
LAT0, LNG0 = 56.6990, 13.0000
M_PER_DEG_LAT = 111320.0
M_PER_DEG_LNG = 111320.0 * math.cos(math.radians(LAT0))


def _m2ll(x, y):
    return [LNG0 + x / M_PER_DEG_LNG, LAT0 + y / M_PER_DEG_LAT]


def _rect(x0, y0, x1, y1):
    return [_m2ll(x0, y0), _m2ll(x1, y0), _m2ll(x1, y1), _m2ll(x0, y1)]


def _rot_rect(x0, y0, x1, y1, deg):
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    a = math.radians(deg)
    out = []
    for (x, y) in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        dx, dy = x - cx, y - cy
        out.append(_m2ll(cx + dx * math.cos(a) - dy * math.sin(a),
                         cy + dx * math.sin(a) + dy * math.cos(a)))
    return out


BOX = _rect(0, 0, 200, 120)
CONCAVE_L = [_m2ll(0, 0), _m2ll(200, 0), _m2ll(200, 60), _m2ll(90, 60),
             _m2ll(90, 120), _m2ll(0, 120)]
IRREGULAR = [_m2ll(0, 10), _m2ll(70, 0), _m2ll(160, 15), _m2ll(210, 70),
             _m2ll(180, 125), _m2ll(95, 140), _m2ll(20, 110)]

# 1 no obstacle · 2 rotated lanes · 3 central no-go across many lanes · 4 narrow corridor
# 5 two separated zones · 6 concave boundary · 7 tilted no-go · 8 irregular boundary + 2 zones
CASES = {
    "rectangle, no obstacle":       dict(boundary=BOX, zones=[], angle=0.0),
    "rotated lanes, no obstacle":   dict(boundary=BOX, zones=[], angle=35.0),
    "central no-go, many lanes":    dict(boundary=BOX, zones=[_rect(90, 20, 115, 100)],
                                         angle=0.0),
    "narrow corridor":              dict(boundary=BOX, zones=[_rect(60, 18, 140, 102)],
                                         angle=0.0),
    "two separated no-gos":         dict(boundary=BOX, zones=[_rect(50, 30, 75, 90),
                                                              _rect(130, 30, 155, 90)],
                                         angle=0.0),
    "concave boundary":             dict(boundary=CONCAVE_L, zones=[], angle=0.0),
    "tilted no-go in survey frame": dict(boundary=BOX, zones=[_rot_rect(75, 35, 125, 85, 30.0)],
                                         angle=0.0),
    "irregular boundary + 2 no-gos": dict(boundary=IRREGULAR,
                                          zones=[_rot_rect(60, 45, 105, 90, 25.0),
                                                 _rect(140, 60, 170, 95)],
                                          angle=0.0),
}
# The cases where an exclusion actually splits lanes, so a decomposition must occur.
SPLIT_CASES = ("central no-go, many lanes", "narrow corridor", "two separated no-gos",
               "tilted no-go in survey frame", "irregular boundary + 2 no-gos")

SHORE_M = 5.0
NO_GO_M = 5.0
SPACING_M = 10.0


def _inputs(name):
    c = CASES[name]
    return {"boundary": c["boundary"], "no_go_zones": c["zones"],
            "shoreline_clearance_m": SHORE_M, "no_go_clearance_m": NO_GO_M,
            "lane_spacing_m": SPACING_M, "primary_angle_deg": c["angle"]}


def _grid(name):
    c = CASES[name]
    return planning._NavGrid(c["boundary"], SHORE_M, c["zones"],
                             step_m=SPACING_M, no_go_clearance=NO_GO_M)


def _gen(name):
    return planning.generate_survey(_inputs(name), max_route_waypoints=5000)


def _coverage_legs(pkg, grid):
    out = []
    for s in pkg["segments"]:
        if s["kind"] not in ("primary", "secondary"):
            continue
        pp = [grid.to_proj.transform(c[0], c[1]) for c in s["coordinates"]]
        out.extend((s["kind"], pp[i], pp[i + 1]) for i in range(len(pp) - 1))
    return out


@requires_geometry
class TestDecompositionStructure(unittest.TestCase):
    """The cells themselves: how many, and that they partition the fragments."""

    def test_a_region_with_nothing_splitting_a_lane_is_one_cell(self):
        for name in ("rectangle, no obstacle", "rotated lanes, no obstacle",
                     "concave boundary"):
            with self.subTest(name):
                rq = _gen(name)["route_quality"]
                self.assertEqual(rq["coverage_cell_count"], 1,
                                 "nothing splits a lane here, so the sweep is one cell and the "
                                 "decomposition must not invent structure")
                self.assertEqual(rq["coverage_cell_handover_count"], 0)

    def test_an_exclusion_that_splits_lanes_produces_several_cells(self):
        for name in SPLIT_CASES:
            with self.subTest(name):
                rq = _gen(name)["route_quality"]
                self.assertGreater(rq["coverage_cell_count"], 1,
                                   "an exclusion splitting lanes must decompose the sweep")

    def test_every_fragment_belongs_to_exactly_one_cell_and_cells_are_contiguous(self):
        for name in CASES:
            with self.subTest(name):
                frs = _gen(name)["route_quality"]["coverage_fragments"]
                self.assertTrue(frs)
                by_cell = {}
                for f in frs:
                    by_cell.setdefault((f["pass_kind"], f["cell_index"]), []).append(f)
                seen = 0
                for key, members in by_cell.items():
                    idxs = [f["fragment_index"] for f in members]
                    self.assertEqual(idxs, list(range(idxs[0], idxs[0] + len(idxs))),
                                     f"{key} is interleaved with another cell instead of being "
                                     f"covered completely before the route moves on")
                    seen += len(members)
                self.assertEqual(seen, len(frs), "a fragment was in no cell, or in two")

    def test_each_cell_is_sweep_monotonic_in_its_own_direction(self):
        for name in CASES:
            with self.subTest(name):
                rq = _gen(name)["route_quality"]
                self.assertEqual(rq["fragment_reorders"], 0)
                by_cell = {}
                for f in rq["coverage_fragments"]:
                    by_cell.setdefault((f["pass_kind"], f["cell_index"]), []).append(f)
                for key, members in by_cell.items():
                    sweeps = [f["sweep_coordinate"] for f in members]
                    asc = members[0]["cell_ascending"]
                    self.assertEqual(sweeps, sorted(sweeps, reverse=not asc),
                                     f"{key} is not monotonic through the sweep")


@requires_geometry
class TestNoSameLaneOscillation(unittest.TestCase):
    """The headline guarantee: the route stops bridging around the exclusion on every lane."""

    def test_no_same_lane_obstacle_bridge_remains(self):
        for name in CASES:
            with self.subTest(name):
                rq = _gen(name)["route_quality"]
                self.assertEqual(
                    rq["same_lane_obstacle_bridge_count"], 0,
                    f"[{name}] the route still leaves one half of a split lane, goes around the "
                    f"exclusion and returns to the other half")

    def test_the_obstacle_is_crossed_at_most_once_per_split_region(self):
        # A hand-over IS allowed — the sweep has to get from one column beside an obstacle to the
        # other exactly once — but it must not scale with the number of split lanes.
        for name in SPLIT_CASES:
            with self.subTest(name):
                rq = _gen(name)["route_quality"]
                split_lanes = sum(1 for f in rq["coverage_fragments"]) - rq["coverage_cell_count"]
                self.assertLessEqual(
                    rq["coverage_cell_handover_count"], rq["coverage_cell_count"],
                    f"[{name}] hand-overs are growing with the lanes, not with the cells")
                self.assertGreater(split_lanes, rq["coverage_cell_handover_count"],
                                   f"[{name}] expected far fewer hand-overs than fragments")


@requires_geometry
class TestCoverageIsUnchanged(unittest.TestCase):
    """Reordering must not change WHAT is covered — only the order it is covered in."""

    def test_the_clipped_lane_content_is_identical_to_the_generator_s_fragments(self):
        for name in CASES:
            with self.subTest(name):
                grid = _grid(name)
                frame = planning._SurveyFrame(grid, CASES[name]["angle"])
                frags, _ = planning._lane_fragments(frame, SPACING_M)
                rq = _gen(name)["route_quality"]
                self.assertEqual(rq["coverage_fragment_count"], len(frags),
                                 "the decomposition changed how many lane pieces exist")
                # Reported fragment lengths are rounded to 2 dp, so a sum over tens of fragments
                # carries a few centimetres of rounding — compare with a tolerance, not exactly.
                self.assertAlmostEqual(
                    sum(f["length_m"] for f in rq["coverage_fragments"]),
                    sum(f["length_m"] for f in frags), delta=0.5,
                    msg="the decomposition changed how much survey line is flown")

    def test_every_fragment_is_still_straight_and_parallel_to_the_survey_angle(self):
        for name in CASES:
            with self.subTest(name):
                grid = _grid(name)
                ang = CASES[name]["angle"]
                for f in _gen(name)["route_quality"]["coverage_fragments"]:
                    a = grid.to_proj.transform(*f["start"])
                    b = grid.to_proj.transform(*f["end"])
                    self.assertEqual(f["point_count"], 2)
                    self.assertIn(planning._survey_align_class(a, b, ang), ("U", "short"),
                                  f"[{name}] a coverage fragment is no longer lane-parallel")

    def test_no_fragment_is_visited_twice(self):
        for name in CASES:
            with self.subTest(name):
                frs = _gen(name)["route_quality"]["coverage_fragments"]
                keys = [(f["pass_kind"], tuple(f["start"]), tuple(f["end"])) for f in frs]
                self.assertEqual(len(keys), len(set(keys)),
                                 f"[{name}] a lane fragment is flown more than once")


@requires_geometry
class TestSafetyGeometryIsUntouched(unittest.TestCase):
    """Clearances and the safety predicates are exactly what they were."""

    def test_shoreline_and_no_go_clearance_are_echoed_unchanged(self):
        for name in CASES:
            with self.subTest(name):
                inp = _gen(name)["planning_inputs"]
                self.assertEqual(inp["shoreline_clearance_m"], SHORE_M)
                self.assertEqual(inp["no_go_clearance_m"], NO_GO_M)
                self.assertEqual(inp["no_go_zones"],
                                 [planning._ring(z) for z in CASES[name]["zones"]],
                                 "the operator's drawn zones were modified")

    def test_every_coverage_leg_is_inside_the_navigable_region(self):
        for name in CASES:
            with self.subTest(name):
                pkg = _gen(name)
                grid = _grid(name)
                for kind, a, b in _coverage_legs(pkg, grid):
                    a_deg = list(grid.to_deg.transform(*a))
                    b_deg = list(grid.to_deg.transform(*b))
                    self.assertTrue(
                        grid.segment_is_safe(a_deg, b_deg, require_inside=True),
                        f"[{name}] a {kind} leg leaves the navigable region or cuts the "
                        f"buffered no-go exclusion")

    def test_the_geometry_contract_still_holds(self):
        for name in CASES:
            with self.subTest(name):
                self.assertTrue(_gen(name)["geometry_check"]["ok"],
                                f"[{name}] the mission geometry contract failed")


@requires_geometry
class TestDeterminism(unittest.TestCase):
    """Identical inputs must produce an identical route, ordering and hash."""

    def test_repeated_generation_is_byte_identical(self):
        for name in CASES:
            with self.subTest(name):
                a, b, c = _gen(name), _gen(name), _gen(name)
                self.assertEqual(a["route_waypoints"], b["route_waypoints"])
                self.assertEqual(b["route_waypoints"], c["route_waypoints"])
                self.assertEqual(a["route_hash"], b["route_hash"])
                self.assertEqual(b["route_hash"], c["route_hash"])
                self.assertEqual(a["route_quality"], b["route_quality"])

    def test_the_cell_decomposition_itself_is_deterministic(self):
        for name in CASES:
            with self.subTest(name):
                grid = _grid(name)
                frame = planning._SurveyFrame(grid, CASES[name]["angle"])
                frags, _ = planning._lane_fragments(frame, SPACING_M)
                runs = []
                for _ in range(3):
                    plan = planning._bcd_traversal_order(planning._bcd_cells(frags))
                    runs.append([(sorted(f["row"] for f in cell),
                                  sorted(round(f["rot"][0][0], 6) for f in cell), asc)
                                 for cell, asc in plan])
                self.assertEqual(runs[0], runs[1])
                self.assertEqual(runs[1], runs[2])


if __name__ == "__main__":
    unittest.main()


@requires_geometry
class TestAlignmentGateAgreesWithTheClassifier(unittest.TestCase):
    """_aligned_transition tier 1 must use the SAME alignment semantics as the classifier.

    The gate used to accept any direct segment whose along-lane (du) or cross-lane (dv) offset
    was under ALIGN_MIN_LEG_M. Over a 5 m lane gap that admits legs up to 11.3° off the V axis,
    while `_survey_align_class` (SURVEY_ALIGN_TOL_DEG = 5°) — the predicate the route-quality
    contract counts violations with — calls anything past 5° "other". A transition could
    therefore be emitted as "direct" and simultaneously counted as an arbitrary-angle coverage
    leg. These tests pin the two sides together.

    They probe the ALIGNED TIERS specifically, so they call _aligned_transition with
    `optimize_transit=False` - the same switch the BCD cell-entry probe uses. The tier-0
    direct-safe candidate would otherwise answer first for every one of these clear-water
    offsets and the tier-1 gate below it would never be reached. Tier 0 is covered by
    tests/test_transition_policy.py.
    """

    def _frame_and_grid(self):
        grid = _grid("rectangle, no obstacle")
        return planning._SurveyFrame(grid, 0.0), grid

    def _transition_for(self, du, dv, optimize_transit=False):
        """Build a transition across a clear region with the given survey-frame offsets."""
        frame, grid = self._frame_and_grid()
        centre = frame.deg_to_rot(list(grid.to_deg.transform(*grid.coverage.centroid.coords[0])))
        a_rot = (centre[0], centre[1])
        b_rot = (centre[0] + du, centre[1] + dv)
        a_deg, b_deg = frame.rot_to_deg(a_rot), frame.rot_to_deg(b_rot)
        cls = planning._survey_align_class(frame.rot_to_proj(a_rot), frame.rot_to_proj(b_rot),
                                           frame.angle_deg)
        path, category = planning._aligned_transition(
            frame, a_deg, b_deg, optimize_transit=optimize_transit)
        return cls, category, path

    def test_a_small_offset_the_classifier_still_calls_aligned_is_accepted_direct(self):
        # du = 0.4 m over dv = 5 m is 4.57° off V — inside the 5° tolerance.
        cls, category, path = self._transition_for(0.4, 5.0)
        self.assertEqual(cls, "V", "precondition: the classifier considers this aligned")
        self.assertEqual(category, "direct",
                         "a segment the classifier calls aligned must still be taken directly")
        self.assertEqual(len(path), 2, "an accepted direct transition is a single segment")

    def test_offsets_the_classifier_rejects_are_no_longer_taken_direct(self):
        # du = 0.7 and 1.0 m over dv = 5 m are 7.97° and 11.31° off V — outside the tolerance,
        # yet both were admitted by the old `abs(du) <= ALIGN_MIN_LEG_M` shortcut.
        for du in (0.7, 1.0):
            with self.subTest(du=du):
                cls, category, path = self._transition_for(du, 5.0)
                self.assertEqual(cls, "other",
                                 "precondition: the classifier rejects this as arbitrary-angle")
                self.assertNotEqual(category, "direct",
                                    f"du={du} m over 5 m is {math.degrees(math.atan2(du, 5)):.1f}° "
                                    f"off axis and must not be taken as a direct aligned leg")

    def test_the_rejected_transition_falls_through_to_the_orthogonal_tier(self):
        for du in (0.7, 1.0):
            with self.subTest(du=du):
                _, category, path = self._transition_for(du, 5.0)
                self.assertEqual(category, "orthogonal",
                                 "it must fall through to the existing L, not to the A* fallback")
                self.assertEqual(len(path), 3, "an orthogonal L has one bend")

    def test_every_leg_of_the_fallen_through_transition_is_aligned_or_short(self):
        frame, _ = self._frame_and_grid()
        for du in (0.7, 1.0):
            with self.subTest(du=du):
                _, _, path = self._transition_for(du, 5.0)
                for p, q in zip(path, path[1:]):
                    pr = frame.rot_to_proj(frame.deg_to_rot(p))
                    qr = frame.rot_to_proj(frame.deg_to_rot(q))
                    self.assertIn(planning._survey_align_class(pr, qr, frame.angle_deg),
                                  ("U", "V", "short"),
                                  "the replacement L introduced an arbitrary-angle leg")

    def test_the_gate_never_admits_what_the_classifier_rejects(self):
        # Sweep the whole band the old shortcut covered: the two predicates must now agree.
        for du in (0.1, 0.2, 0.3, 0.4, 0.44, 0.5, 0.7, 0.9, 1.0, 1.5):
            with self.subTest(du=du):
                cls, category, _ = self._transition_for(du, 5.0)
                if category == "direct":
                    self.assertIn(cls, ("U", "V", "short"),
                                  f"du={du} m was taken direct but the classifier calls it {cls}")


@requires_geometry
class TestCellEntryOrientation(unittest.TestCase):
    """The one free bit per cell: which way its first row is flown.

    That bit fixes both which side the cell is entered from and — through the row-by-row
    alternation — which side it is left on. It is chosen on three deterministic local terms, in
    order: whether the hand-over INTO the cell can be built from survey-frame legs at all,
    whether the heading the cell is LEFT on already points at the next cell, and finally the
    combined entry+exit U distance.
    """

    def test_no_hand_over_falls_through_to_the_astar_fallback(self):
        # The first term exists to keep this at zero: a fallback hand-over emits arbitrary-angle
        # geometry and breaks the survey-frame alignment contract.
        for name in CASES:
            with self.subTest(name):
                rq = _gen(name)["route_quality"]
                self.assertEqual(rq["fallback_connector_count"], 0,
                                 f"[{name}] a transition dropped to the generic A* connector")
                self.assertEqual(rq["non_survey_aligned_segment_count"], 0,
                                 f"[{name}] an arbitrary-angle coverage leg was emitted")

    def test_probing_a_candidate_entry_does_not_colour_the_reported_metrics(self):
        # Choosing the entry side probes BOTH candidates with _aligned_transition, and reaching
        # its fallback tier runs the grid A*, which bumps the grid's connector counters. Only one
        # candidate is ever used, so the probe must leave those counters exactly as it found them.
        for name in SPLIT_CASES:
            with self.subTest(name):
                pkg = _gen(name)
                rq = pkg["route_quality"]
                connector_pts = sum(len(s["coordinates"]) for s in pkg["segments"]
                                    if s["kind"] not in ("primary", "secondary"))
                self.assertLessEqual(
                    rq["final_connector_waypoint_count"], connector_pts,
                    "connector waypoint counts exceed the connector geometry actually emitted — "
                    "a discarded entry probe leaked into the diagnostics")
                self.assertLessEqual(rq["connector_length_after_m"],
                                     rq["connector_length_before_m"] + 1e-6)

    def test_entry_choice_is_deterministic_across_repeats(self):
        for name in CASES:
            with self.subTest(name):
                runs = []
                for _ in range(3):
                    frs = _gen(name)["route_quality"]["coverage_fragments"]
                    runs.append([(f["cell_index"], f["cell_ascending"], tuple(f["start"]))
                                 for f in frs])
                self.assertEqual(runs[0], runs[1])
                self.assertEqual(runs[1], runs[2])
