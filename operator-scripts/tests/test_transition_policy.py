"""Direct-safe-first inter-fragment transitions (planning._aligned_transition tier 0).

Run from operator-scripts/:  python -m unittest tests.test_transition_policy

WHAT CHANGED, AND WHY THESE TESTS EXIST
---------------------------------------
A COVERAGE FRAGMENT is a sonar pass: it must run parallel to the survey angle, because a stable
heading at a fixed lane spacing is what makes the swaths overlap. A TRANSITION is not a sonar
pass — it is the vessel repositioning from the end of one finished fragment to the start of the
next — and holding it to the same U/V axes bought nothing acoustically while costing real
distance: on the operator's own large planning polygon it made the route fly BACK ALONG a lane it
had just surveyed (154–241 m of exact retrace per survey angle) and emit two 90° corners where a
straight leg needs none.

So the straight segment is now tried FIRST, at whatever heading it has, and accepted only by the
SAME authoritative predicate every other tier is held to (`_seg_safe_cached(..., True)` against
`buildable` = shoreline-inset MINUS the buffered no-go exclusion, minus the wire margin). When it
is not safe, the pre-existing aligned/orthogonal/bypass/A* hierarchy runs unchanged.

Three things must therefore be true at once, and each is pinned below:

  * SAFETY IS UNCHANGED. A direct transit is not a new permission — the same predicate, the same
    region, the same tolerances. It can neither leave the navigable region nor touch the buffered
    no-go geometry (TestDirectTransitIsHeldToTheSamePredicate).
  * COVERAGE IS UNTOUCHED. The clipped fragments, their order and their flown DIRECTION are
    byte-identical to the aligned-only baseline. That is what `optimize_transit=False` in the
    BCD cell-entry probe buys: without it, a probe that suddenly answers "direct_transit" for both
    candidate entries stops discriminating and the cell entry bit — hence some cells' sweep
    direction — moves for reasons unrelated to buildability (TestCoverageIsUnchanged,
    TestTheBcdEntryProbeIgnoresTierZero).
  * THE CONTRACT MEASURES THE RIGHT SET. An arbitrary-angle transit must never be reported as
    arbitrary-angle COVERAGE (TestTheAlignmentContractCountsCoverageOnly).
"""

import copy
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import planning  # noqa: E402
from tests.test_bcd_coverage import CASES, _grid, _inputs, _m2ll, _rect  # noqa: E402
from tests import test_planning_quality as Q  # noqa: E402

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

# The full regression matrix the investigation used: the eight BCD geometries plus the three
# asymmetric route-quality fixtures.
MATRIX = [(f"bcd:{n}", _inputs(n)) for n in CASES] + \
         [(f"quality:{n}", Q._fixture(n))
          for n in ("concave notch", "multi-lobe", "central obstacle")]


def _gen(inp):
    return planning.generate_survey(copy.deepcopy(inp), max_route_waypoints=5000)


def _gen_aligned_only(inp):
    """The same generation with tier 0 suppressed everywhere — i.e. the exact pre-change
    behaviour, reproduced through the public switch rather than from a stored golden file."""
    real = planning._aligned_transition

    def aligned_only(frame, a, b, in_dir=None, out_dir=None, optimize_transit=True):
        return real(frame, a, b, in_dir=in_dir, out_dir=out_dir, optimize_transit=False)

    planning._aligned_transition = aligned_only
    try:
        return _gen(inp)
    finally:
        planning._aligned_transition = real


def _fragment_signature(pkg):
    """Fragment identity AND visiting order AND flown direction, as one comparable value."""
    return [[f["fragment_index"], f["cell_index"], f["cell_ascending"], f["row_index"],
             f["start"], f["end"], f["length_m"]]
            for f in pkg["route_quality"]["coverage_fragments"]]


def _split_legs(pkg, grid):
    """(fragment legs, transition legs) of the coverage segments, as [(kind, a_deg, b_deg)]."""
    pairs = {(tuple(f["start"]), tuple(f["end"]))
             for f in pkg["route_quality"]["coverage_fragments"]}
    frags, trans = [], []
    for s in pkg["segments"]:
        if s["kind"] not in ("primary", "secondary"):
            continue
        cs = s["coordinates"]
        for i in range(len(cs) - 1):
            a, b = cs[i], cs[i + 1]
            key = ((round(a[0], 7), round(a[1], 7)), (round(b[0], 7), round(b[1], 7)))
            (frags if key in pairs else trans).append((s["kind"], a, b))
    return frags, trans


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTierZeroAcceptance(unittest.TestCase):
    """The tier itself: a safe straight leg is taken directly, an unsafe one is not."""

    CASE = "central no-go, many lanes"

    def setUp(self):
        self.grid = _grid(self.CASE)
        self.angle = CASES[self.CASE]["angle"]
        self.frame = planning._SurveyFrame(self.grid, self.angle)
        frags, _skipped = planning._lane_fragments(
            self.frame, _inputs(self.CASE)["lane_spacing_m"])
        self.frags = frags
        self.box = self.frame.exclusion_boxes[0]

    def _side(self, frag):
        """Which side of the exclusion (in U) this fragment lies on."""
        mid = (self.box[0] + self.box[2]) / 2.0
        return "left" if frag["rot"][1][0] <= mid else "right"

    def _clear_water_pair(self):
        """Two fragment ends on ADJACENT rows, the same side of the exclusion, offset on BOTH
        survey axes — the ordinary lane turn whose straight leg tier 1 used to reject."""
        by_row = {}
        for f in self.frags:
            if self._side(f) == "left":
                by_row.setdefault(f["row"], []).append(f)
        for row in sorted(by_row):
            if row + 1 not in by_row:
                continue
            f, g = by_row[row][0], by_row[row + 1][0]
            a = self.frame.rot_to_deg(f["rot"][0])      # left end of the lower lane
            b = self.frame.rot_to_deg(g["rot"][1])      # right end of the upper lane
            du = abs(g["rot"][1][0] - f["rot"][0][0])
            if du < 2.0:
                continue
            cls = planning._survey_align_class(self.grid.to_proj.transform(*a),
                                               self.grid.to_proj.transform(*b), self.angle)
            if cls == "other" and self.grid.segment_is_safe(a, b, require_inside=True):
                return a, b
        self.fail("fixture: no safe arbitrary-angle clear-water pair found")

    def _across_the_exclusion_pair(self):
        """Two fragment ends on the SAME row, opposite sides of the exclusion — the straight leg
        between them necessarily cuts the buffered zone."""
        by_row = {}
        for f in self.frags:
            by_row.setdefault(f["row"], []).append(f)
        for row in sorted(by_row):
            group = sorted(by_row[row], key=lambda f: f["rot"][0][0])
            if len(group) < 2:
                continue
            a = self.frame.rot_to_deg(group[0]["rot"][1])    # right end of the left fragment
            b = self.frame.rot_to_deg(group[-1]["rot"][0])   # left end of the right fragment
            if not self.grid.segment_is_safe(a, b, require_inside=True):
                return a, b
        self.fail("fixture: the exclusion does not split any lane")

    def test_a_safe_arbitrary_angle_leg_is_emitted_as_a_two_point_direct_transit(self):
        a, b = self._clear_water_pair()
        cls = planning._survey_align_class(self.grid.to_proj.transform(*a),
                                           self.grid.to_proj.transform(*b), self.angle)
        self.assertEqual(cls, "other", "precondition: the direct leg is arbitrary-angle")
        self.assertTrue(self.grid.segment_is_safe(a, b, require_inside=True),
                        "precondition: the direct leg is safe")

        path, category = planning._aligned_transition(self.frame, a, b)
        self.assertEqual(category, "direct_transit")
        self.assertEqual(len(path), 2, "a direct transit is a single two-point segment")
        self.assertEqual([[float(c) for c in p] for p in path],
                         [[float(c) for c in a], [float(c) for c in b]])

    def test_an_unsafe_direct_leg_is_never_taken_direct(self):
        a, b = self._across_the_exclusion_pair()
        self.assertFalse(self.grid.segment_is_safe(a, b, require_inside=True),
                         "precondition: the direct leg must cross the exclusion")

        path, category = planning._aligned_transition(self.frame, a, b)
        self.assertNotEqual(category, "direct_transit")
        self.assertIn(category, ("shortest_safe_transit", "direct", "orthogonal", "bypass",
                                 "fallback"))
        self.assertGreater(len(path), 2, "an unsafe direct must be replaced by a routed path")
        for p, q in zip(path, path[1:]):
            self.assertTrue(self.grid.segment_is_safe(p, q, require_inside=True))

    def test_with_optimisation_suppressed_it_falls_through_to_the_aligned_hierarchy(self):
        """The pre-F2/F4 hierarchy is still intact underneath and still answers."""
        a, b = self._across_the_exclusion_pair()
        path, category = planning._aligned_transition(self.frame, a, b, optimize_transit=False)
        self.assertIn(category, ("direct", "orthogonal", "bypass", "fallback"))
        self.assertGreater(len(path), 2)
        for p, q in zip(path, path[1:]):
            self.assertTrue(self.grid.segment_is_safe(p, q, require_inside=True))

    def test_suppressing_the_tier_restores_the_previous_answer(self):
        a, b = self._clear_water_pair()
        _, with_tier0 = planning._aligned_transition(self.frame, a, b)
        _, without = planning._aligned_transition(self.frame, a, b, optimize_transit=False)
        self.assertEqual(with_tier0, "direct_transit")
        self.assertNotEqual(without, "direct_transit",
                            "optimize_transit=False must reach the aligned tiers")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestDirectTransitIsHeldToTheSamePredicate(unittest.TestCase):
    """No new permission: a direct transit satisfies the authoritative predicate, stays inside
    the navigable/buildable region and clears the buffered no-go exclusion."""

    def test_every_emitted_direct_transit_passes_the_authoritative_predicate(self):
        from shapely.geometry import LineString
        from shapely.ops import transform
        seen = 0
        for name, inp in MATRIX:
            with self.subTest(case=name):
                pkg = _gen(inp)
                grid = planning._NavGrid(
                    inp["boundary"], inp.get("shoreline_clearance_m", 0),
                    inp.get("no_go_zones") or [], step_m=inp.get("lane_spacing_m", 10),
                    no_go_clearance=inp.get("no_go_clearance_m", 0))
                _frags, trans = _split_legs(pkg, grid)
                for kind, a, b in trans:
                    seen += 1
                    # 3. the SAME predicate the aligned tiers are held to
                    self.assertTrue(grid.segment_is_safe(a, b, require_inside=True),
                                    f"[{name}] a {kind} transition leg fails segment_is_safe")
                    lp = transform(grid.to_proj.transform, LineString([a, b]))
                    # 5. inside the region generation may build in
                    self.assertTrue(grid._seg_covered(lp),
                                    f"[{name}] a transition leg leaves buildable geometry")
                    # 4. clear of the BUFFERED no-go exclusion, not merely the drawn zone
                    self.assertTrue(grid._seg_clears_nogo(lp),
                                    f"[{name}] a transition leg enters the no-go exclusion")
        self.assertGreater(seen, 100, "expected the matrix to emit many transition legs")

    def test_a_direct_transit_is_never_emitted_across_the_exclusion(self):
        """Every split lane, probed end to end across the buffered zone."""
        case = "central no-go, many lanes"
        grid = _grid(case)
        angle = CASES[case]["angle"]
        frame = planning._SurveyFrame(grid, angle)
        frags, _skipped = planning._lane_fragments(frame, _inputs(case)["lane_spacing_m"])
        by_row = {}
        for f in frags:
            by_row.setdefault(f["row"], []).append(f)
        split_rows = [r for r, g in by_row.items() if len(g) >= 2]
        self.assertTrue(split_rows, "fixture: the exclusion must split lanes")
        for row in sorted(split_rows):
            group = sorted(by_row[row], key=lambda f: f["rot"][0][0])
            a = frame.rot_to_deg(group[0]["rot"][1])
            b = frame.rot_to_deg(group[-1]["rot"][0])
            with self.subTest(row=row):
                self.assertFalse(grid.segment_is_safe(a, b, require_inside=True),
                                 "precondition: the straight leg crosses the exclusion")
                path, category = planning._aligned_transition(frame, a, b)
                self.assertNotEqual(category, "direct_transit")
                for p, q in zip(path, path[1:]):
                    self.assertTrue(grid.segment_is_safe(p, q, require_inside=True))

    def test_the_geometry_contract_still_holds_across_the_matrix(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                pkg = _gen(inp)
                self.assertTrue(pkg["geometry_check"]["ok"],
                                f"[{name}] {pkg['geometry_check']['failures']}")
                v = planning.validate_plan(
                    {**inp, "segments": pkg["segments"],
                     "route_waypoints": pkg["route_waypoints"],
                     "route_hash": pkg["route_hash"],
                     "input_revision": pkg["input_revision"]},
                    max_route_waypoints=5000)
                self.assertTrue(v["ok"], f"[{name}] validation failed: {v['errors']}")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTheAlignmentContractCountsCoverageOnly(unittest.TestCase):
    """F1: an arbitrary-angle TRANSIT must not be charged to the COVERAGE contract."""

    def test_a_direct_transit_does_not_increment_the_non_aligned_coverage_count(self):
        found_arbitrary_transit = False
        for name, inp in MATRIX:
            with self.subTest(case=name):
                pkg = _gen(inp)
                rq = pkg["route_quality"]
                self.assertEqual(rq["non_survey_aligned_coverage_segment_count"], 0,
                                 f"[{name}] arbitrary-angle SURVEY geometry was emitted")
                self.assertEqual(rq["non_survey_aligned_segment_count"], 0,
                                 f"[{name}] the compat key must mean the same thing")
                if rq["non_survey_aligned_transition_count"] > 0:
                    found_arbitrary_transit = True
        self.assertTrue(found_arbitrary_transit,
                        "the matrix must exercise at least one arbitrary-angle transit, or this "
                        "test proves nothing")

    def test_the_transition_diagnostic_reports_what_was_actually_built(self):
        inp = _inputs("rotated lanes, no obstacle")
        pkg = _gen(inp)
        grid = _grid("rotated lanes, no obstacle")
        rq = pkg["route_quality"]
        self.assertGreater(rq["direct_transit_transition_count"], 0,
                           "clear water at a rotated angle must produce direct transits")
        _frags, trans = _split_legs(pkg, grid)
        angle = CASES["rotated lanes, no obstacle"]["angle"]
        counted = 0
        for _kind, a, b in trans:
            cls = planning._survey_align_class(grid.to_proj.transform(*a),
                                               grid.to_proj.transform(*b), angle)
            if cls == "other":
                counted += 1
        self.assertEqual(counted, rq["non_survey_aligned_transition_count"],
                         "the reported transit-alignment tally must be re-derivable from the "
                         "emitted geometry")

    def test_the_two_alignment_buckets_sum_to_the_whole_coverage_polyline(self):
        """The split partitions the segment — nothing is dropped or double-counted."""
        for name, inp in MATRIX:
            with self.subTest(case=name):
                pkg = _gen(inp)
                grid = planning._NavGrid(
                    inp["boundary"], inp.get("shoreline_clearance_m", 0),
                    inp.get("no_go_zones") or [], step_m=inp.get("lane_spacing_m", 10),
                    no_go_clearance=inp.get("no_go_clearance_m", 0))
                rq = pkg["route_quality"]
                total = sum(len(s["coordinates"]) - 1 for s in pkg["segments"]
                            if s["kind"] in ("primary", "secondary"))
                classified = (rq["survey_aligned_coverage_segment_count"]
                              + rq["non_survey_aligned_coverage_segment_count"]
                              + rq["survey_aligned_transition_count"]
                              + rq["non_survey_aligned_transition_count"])
                frags, trans = _split_legs(pkg, grid)
                shorts = 0
                for kind, a, b in frags + trans:
                    ang = pkg["metrics"][("primary_angle_deg" if kind == "primary"
                                          else "secondary_angle_deg")]
                    if planning._survey_align_class(grid.to_proj.transform(*a),
                                                    grid.to_proj.transform(*b), ang) == "short":
                        shorts += 1
                self.assertEqual(classified + shorts, total,
                                 f"[{name}] the alignment split does not partition the segment")

    def test_coverage_and_transit_lengths_are_reported_separately(self):
        """The accounting behind metrics.coverage_length_m is now visible."""
        for name, inp in MATRIX:
            with self.subTest(case=name):
                pkg = _gen(inp)
                rq = pkg["route_quality"]
                # `coverage_fragments[].length_m` is a PROJECTED (UTM) length measured in the
                # rotated survey frame, while every `*_length_m` metric is GEODESIC. They
                # describe the same lines and agree to within UTM scale distortion (~0.02 %),
                # so this is a relative check, not an equality.
                frag_m = sum(f["length_m"] for f in rq["coverage_fragments"])
                self.assertAlmostEqual(rq["coverage_fragment_length_m"] / frag_m, 1.0, places=3)
                # The one that must hold exactly: the split partitions coverage_length_m.
                self.assertAlmostEqual(
                    rq["coverage_fragment_length_m"] + rq["in_coverage_transition_length_m"],
                    pkg["metrics"]["coverage_length_m"], delta=1.0,
                    msg=f"[{name}] coverage_length_m must equal fragments + in-coverage transit")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestCoverageIsUnchanged(unittest.TestCase):
    """The whole point: transits move, survey geometry does not."""

    def test_every_coverage_fragment_is_still_survey_frame_aligned(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                pkg = _gen(inp)
                grid = planning._NavGrid(
                    inp["boundary"], inp.get("shoreline_clearance_m", 0),
                    inp.get("no_go_zones") or [], step_m=inp.get("lane_spacing_m", 10),
                    no_go_clearance=inp.get("no_go_clearance_m", 0))
                frags, _trans = _split_legs(pkg, grid)
                self.assertTrue(frags, f"[{name}] no coverage fragments")
                for kind, a, b in frags:
                    ang = pkg["metrics"][("primary_angle_deg" if kind == "primary"
                                          else "secondary_angle_deg")]
                    cls = planning._survey_align_class(grid.to_proj.transform(*a),
                                                       grid.to_proj.transform(*b), ang)
                    self.assertIn(cls, ("U", "short"),
                                  f"[{name}] a survey fragment is not parallel to U")

    def test_fragment_set_order_and_direction_match_the_aligned_only_baseline(self):
        """9 + 10: identity, visiting ORDER and flown DIRECTION are all byte-identical."""
        for name, inp in MATRIX:
            with self.subTest(case=name):
                self.assertEqual(_fragment_signature(_gen(inp)),
                                 _fragment_signature(_gen_aligned_only(inp)),
                                 f"[{name}] the transition policy moved coverage geometry")

    def test_the_total_survey_line_is_unchanged(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                a = sum(f["length_m"] for f in _gen(inp)["route_quality"]["coverage_fragments"])
                b = sum(f["length_m"]
                        for f in _gen_aligned_only(inp)["route_quality"]["coverage_fragments"])
                self.assertAlmostEqual(a, b, delta=0.01, msg=f"[{name}] survey line length moved")

    def test_cells_bridges_and_fallbacks_are_unchanged(self):
        """11 + 12 + 13: the decomposition and the fallback hierarchy are untouched."""
        for name, inp in MATRIX:
            with self.subTest(case=name):
                new = _gen(inp)["route_quality"]
                old = _gen_aligned_only(inp)["route_quality"]
                self.assertEqual(new["coverage_cell_count"], old["coverage_cell_count"])
                self.assertEqual(new["coverage_fragment_count"], old["coverage_fragment_count"])
                self.assertEqual(new["coverage_cell_handover_count"],
                                 old["coverage_cell_handover_count"])
                self.assertEqual(new["same_lane_obstacle_bridge_count"], 0)
                self.assertLessEqual(new["fallback_connector_count"],
                                     old["fallback_connector_count"],
                                     f"[{name}] direct-safe-first must not push work onto A*")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTheBcdEntryProbeIgnoresTierZero(unittest.TestCase):
    """F3: cell-entry buildability is a property of the GEOMETRY, not of how transits are drawn.

    `bcd:irregular boundary + 2 no-gos` is the case that proved this matters: with the probe
    left on the new tier it flipped an entry bit and reversed the sweep direction of several
    fragments, changing the route for a reason unrelated to buildability."""

    CASE = "irregular boundary + 2 no-gos"

    def test_the_probe_never_sees_a_direct_transit(self):
        seen = []
        real = planning._aligned_transition

        def spy(frame, a, b, in_dir=None, out_dir=None, optimize_transit=True):
            caller = sys._getframe(1).f_code.co_name
            if caller == "_unaligned_entry":
                seen.append(optimize_transit)
            return real(frame, a, b, in_dir=in_dir, out_dir=out_dir,
                        optimize_transit=optimize_transit)

        planning._aligned_transition = spy
        try:
            _gen(_inputs(self.CASE))
        finally:
            planning._aligned_transition = real
        self.assertTrue(seen, "the fixture must actually run the cell-entry probe")
        self.assertTrue(all(flag is False for flag in seen),
                        "the BCD entry probe must suppress the direct-safe tier")

    def test_the_entry_bit_is_identical_with_and_without_the_tier(self):
        pkg = _gen(_inputs(self.CASE))
        base = _gen_aligned_only(_inputs(self.CASE))
        self.assertEqual([f["cell_ascending"] for f in pkg["route_quality"]["coverage_fragments"]],
                         [f["cell_ascending"]
                          for f in base["route_quality"]["coverage_fragments"]])
        self.assertEqual(_fragment_signature(pkg), _fragment_signature(base))


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestDeterminism(unittest.TestCase):
    """14: identical inputs must still produce an identical route and an identical hash."""

    def test_repeated_generation_is_byte_identical(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                a, b = _gen(inp), _gen(inp)
                self.assertEqual(a["route_hash"], b["route_hash"])
                self.assertEqual(a["route_waypoints"], b["route_waypoints"])
                self.assertEqual([s["coordinates"] for s in a["segments"]],
                                 [s["coordinates"] for s in b["segments"]])

    def test_the_route_actually_changed_from_the_aligned_only_baseline(self):
        """A guard on the guards: if nothing moved, every equality above is vacuous."""
        moved = [n for n, inp in MATRIX
                 if _gen(inp)["route_hash"] != _gen_aligned_only(inp)["route_hash"]]
        self.assertGreater(len(moved), len(MATRIX) // 2,
                           "direct-safe-first should change most routes in the matrix")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTheTransitsActuallyImprove(unittest.TestCase):
    """Direction-only assertions. The investigation's percentages are reference observations,
    deliberately NOT hard-coded thresholds — what is pinned is that nothing gets worse."""

    def _transit_metrics(self, pkg, grid):
        _frags, trans = _split_legs(pkg, grid)
        total = 0.0
        for _k, a, b in trans:
            ap, bp = grid.to_proj.transform(*a), grid.to_proj.transform(*b)
            total += math.hypot(bp[0] - ap[0], bp[1] - ap[1])
        return total, len(trans)

    def test_transit_distance_never_increases_and_points_are_only_traded_for_distance(self):
        """Transit DISTANCE is the quantity the optimisation tiers minimise, and it may never
        grow. Waypoint count is a different quantity and is allowed to move in either direction:
        tier 0 removes L corners (large reductions), while tier 0b can spend a corner to cut a
        detour short. What must never happen is paying waypoints for nothing, so an increase is
        only legal alongside a strict distance reduction."""
        for name, inp in MATRIX:
            with self.subTest(case=name):
                grid = planning._NavGrid(
                    inp["boundary"], inp.get("shoreline_clearance_m", 0),
                    inp.get("no_go_zones") or [], step_m=inp.get("lane_spacing_m", 10),
                    no_go_clearance=inp.get("no_go_clearance_m", 0))
                new, old = _gen(inp), _gen_aligned_only(inp)
                n_len, _ = self._transit_metrics(new, grid)
                o_len, _ = self._transit_metrics(old, grid)
                self.assertLessEqual(n_len, o_len + 0.5, f"[{name}] transit distance grew")
                n_wp = new["metrics"]["waypoint_count"]
                o_wp = old["metrics"]["waypoint_count"]
                if n_wp > o_wp:
                    self.assertLess(n_len, o_len - planning.F4_MIN_GAIN_M,
                                    f"[{name}] {n_wp - o_wp} waypoint(s) were added without a "
                                    f"real distance saving")


if __name__ == "__main__":
    unittest.main()
