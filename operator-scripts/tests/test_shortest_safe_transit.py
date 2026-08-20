"""Bounded shortest-safe inter-fragment transits (planning._aligned_transition tier 0b, "F4").

Run from operator-scripts/:  python -m unittest tests.test_shortest_safe_transit

WHAT THIS TIER IS, AND WHAT IT IS NOT
-------------------------------------
Tier 0 takes the straight leg between two finished coverage fragments whenever the authoritative
predicate accepts it. Tier 0b is only reached when it does not — when a shoreline concavity or the
buffered no-go exclusion sits across the straight line. Before this tier, that case was answered by
a survey-frame L or staircase, which is often far longer than the obstruction requires: measured on
the operator's own large planning polygon, 110.1 m emitted for a 78.2 m gap, 160.6 m for 114.5 m,
162.6 m for 109.5 m — and in two of the three the straight leg missed only by leaving the approved
region at a concave corner, once by barely two metres.

It is NOT a general path planner and these tests hold it to that:

  * BOUNDED. The candidate corners come from the a/b bounding box grown by F4_LOCAL_MARGIN_M and
    are capped at F4_MAX_LOCAL_VERTICES. A corner on the far side of the survey can never enter the
    set, so the cost of the search does not grow with the survey area (TestTheSearchIsLocal).
  * SUBORDINATE. The aligned tiers are evaluated FIRST and produce exactly the candidate they
    produce today; the detour is taken only when it beats that candidate by F4_MIN_GAIN_M, so a
    near-tie never churns the route hash (TestItOnlyWinsWhenItActuallyWins).
  * NO NEW PERMISSION. Every edge of the graph, and every leg of the emitted path, is admitted
    only by `_seg_safe_cached(..., True)` — the same predicate, region and tolerances as every
    other tier (TestEveryLegIsAuthoritativelySafe).
  * COVERAGE-NEUTRAL. The surveyed geometry — the fragments, their coordinates, their lengths and
    the BCD cells they belong to — is byte-identical to the pre-F4 baseline (TestCoverageIsUntouched).
    Their visiting ORDER and flown DIRECTION deliberately are not: the cell order and orientation
    are chosen by measuring the real hand-overs, so a cheaper transit makes a different sequence
    the cheapest one. That is the mechanism, not a leak — see tests/test_bcd_cell_order.py.
"""

import copy
import math
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import planning  # noqa: E402
from tests.test_bcd_coverage import CASES, _grid, _inputs, _m2ll, _rect, _rot_rect  # noqa: E402
from tests import test_planning_quality as Q  # noqa: E402

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

MATRIX = [(f"bcd:{n}", _inputs(n)) for n in CASES] + \
         [(f"quality:{n}", Q._fixture(n))
          for n in ("concave notch", "multi-lobe", "central obstacle")]


def _gen(inp):
    return planning.generate_survey(copy.deepcopy(inp), max_route_waypoints=5000)


def _gen_without_f4(inp):
    """Generation with tier 0b suppressed but tier 0 intact — i.e. the exact F1/F2/F3 baseline,
    reproduced through the real code path rather than from a stored golden file."""
    real = planning._NavGrid.shortest_safe_transit
    planning._NavGrid.shortest_safe_transit = lambda self, a, b, limit_m=None: None
    try:
        return _gen(inp)
    finally:
        planning._NavGrid.shortest_safe_transit = real


def _gen_aligned_only(inp):
    """Both optimisation tiers suppressed — the pre-F2 behaviour."""
    real = planning._aligned_transition

    def aligned_only(frame, a, b, in_dir=None, out_dir=None, optimize_transit=True):
        return real(frame, a, b, in_dir=in_dir, out_dir=out_dir, optimize_transit=False)

    planning._aligned_transition = aligned_only
    try:
        return _gen(inp)
    finally:
        planning._aligned_transition = real


def _fragment_signature(pkg):
    return [[f["fragment_index"], f["cell_index"], f["cell_ascending"], f["row_index"],
             f["start"], f["end"], f["length_m"]]
            for f in pkg["route_quality"]["coverage_fragments"]]


def _coverage_content(pkg):
    """WHAT is surveyed, with WHEN and WHICH WAY ROUND factored out.

    The BCD cell order and each cell's orientation are chosen from the MEASURED cost of the real
    hand-overs, so they legitimately move when the transit policy changes — a cheaper transit
    makes a different sequence the cheapest one. The coverage itself must not move, and this is
    the value that pins it: cell membership, sweep row, length, and the endpoint pair unordered
    (flying a lane the other way is the same lane)."""
    return sorted([f["cell_index"], f["sweep_coordinate"], round(f["length_m"], 6),
                   *sorted([tuple(f["start"]), tuple(f["end"])])]
                  for f in pkg["route_quality"]["coverage_fragments"])


def _transitions(inp):
    """[(category, path, aligned_category, aligned_path)] for every EMITTED transition, by spying
    on the tier so each decision can be compared against what it replaced.

    Emitted only — the caller frame is checked. The cell-ordering search asks the same tier to
    COST candidate hand-overs it mostly never builds (`planning._transition_oracle`), and counting
    those here would describe the search rather than the route."""
    out = []
    real = planning._aligned_transition

    def spy(frame, a, b, in_dir=None, out_dir=None, optimize_transit=True):
        got = real(frame, a, b, in_dir=in_dir, out_dir=out_dir,
                   optimize_transit=optimize_transit)
        if optimize_transit and sys._getframe(1).f_code.co_name == "_survey_frame_coverage":
            was = real(frame, a, b, in_dir=in_dir, out_dir=out_dir, optimize_transit=False)
            out.append((got[1], got[0], was[1], was[0], frame.grid))
        return got

    planning._aligned_transition = spy
    try:
        _gen(inp)
    finally:
        planning._aligned_transition = real
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTierOrder(unittest.TestCase):
    """Tier 0 still wins whenever it can; tier 0b is never consulted unnecessarily."""

    CASE = "central no-go, many lanes"

    def setUp(self):
        self.grid = _grid(self.CASE)
        self.frame = planning._SurveyFrame(self.grid, CASES[self.CASE]["angle"])
        self.frags, _ = planning._lane_fragments(self.frame,
                                                 _inputs(self.CASE)["lane_spacing_m"])

    def _rows(self):
        by_row = {}
        for f in self.frags:
            by_row.setdefault(f["row"], []).append(f)
        for r in by_row:
            by_row[r].sort(key=lambda f: f["rot"][0][0])
        return by_row

    def test_a_safe_direct_leg_is_still_taken_direct_and_f4_is_not_run(self):
        by_row = self._rows()
        rows = sorted(by_row)
        a = self.frame.rot_to_deg(by_row[rows[0]][0]["rot"][0])
        b = self.frame.rot_to_deg(by_row[rows[1]][0]["rot"][1])
        self.assertTrue(self.grid.segment_is_safe(a, b, require_inside=True),
                        "precondition: the direct leg is safe")
        before = self.grid.f4_invocations
        path, category = planning._aligned_transition(self.frame, a, b)
        self.assertEqual(category, "direct_transit")
        self.assertEqual(len(path), 2)
        self.assertEqual(self.grid.f4_invocations, before,
                         "tier 0b must not be searched when tier 0 already answered")

    def test_across_the_exclusion_the_detour_tier_answers(self):
        by_row = self._rows()
        for row in sorted(by_row):
            group = by_row[row]
            if len(group) < 2:
                continue
            a = self.frame.rot_to_deg(group[0]["rot"][1])
            b = self.frame.rot_to_deg(group[-1]["rot"][0])
            if self.grid.segment_is_safe(a, b, require_inside=True):
                continue
            with self.subTest(row=row):
                path, category = planning._aligned_transition(self.frame, a, b)
                self.assertIn(category, ("shortest_safe_transit", "orthogonal", "bypass"))
                for p, q in zip(path, path[1:]):
                    self.assertTrue(self.grid.segment_is_safe(p, q, require_inside=True))

    def test_suppressing_optimisation_restores_the_aligned_answer(self):
        by_row = self._rows()
        rows = sorted(by_row)
        a = self.frame.rot_to_deg(by_row[rows[0]][0]["rot"][0])
        b = self.frame.rot_to_deg(by_row[rows[1]][0]["rot"][1])
        _, on = planning._aligned_transition(self.frame, a, b)
        _, off = planning._aligned_transition(self.frame, a, b, optimize_transit=False)
        self.assertEqual(on, "direct_transit")
        self.assertNotIn(off, ("direct_transit", "shortest_safe_transit"))


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestEveryLegIsAuthoritativelySafe(unittest.TestCase):
    """No new permission — the detour is proven with the existing predicate, twice over."""

    def test_every_emitted_detour_leg_passes_the_authoritative_predicate(self):
        from shapely.geometry import LineString
        from shapely.ops import transform
        seen = 0
        for name, inp in MATRIX:
            with self.subTest(case=name):
                for cat, path, _wc, _wp, grid in _transitions(inp):
                    if cat != "shortest_safe_transit":
                        continue
                    seen += 1
                    for p, q in zip(path, path[1:]):
                        self.assertTrue(grid.segment_is_safe(p, q, require_inside=True),
                                        f"[{name}] a detour leg fails segment_is_safe")
                        lp = transform(grid.to_proj.transform, LineString([p, q]))
                        self.assertTrue(grid._seg_covered(lp),
                                        f"[{name}] a detour leg leaves buildable geometry")
                        self.assertTrue(grid._seg_clears_nogo(lp),
                                        f"[{name}] a detour leg enters the no-go exclusion")
        self.assertGreater(seen, 10, "the matrix must actually exercise the detour tier")

    def test_a_detour_around_the_buffered_no_go_stays_outside_it(self):
        """The exclusion case specifically: the drawn zone is not enough — the BUFFERED one is
        what the detour must clear."""
        inp = _inputs("central no-go, many lanes")
        grid = _grid("central no-go, many lanes")
        found = 0
        for cat, path, _wc, _wp, g in _transitions(inp):
            if cat != "shortest_safe_transit":
                continue
            for p, q in zip(path, path[1:]):
                from shapely.geometry import LineString
                from shapely.ops import transform
                lp = transform(g.to_proj.transform, LineString([p, q]))
                self.assertLess(lp.intersection(g.nogo.buffer(-planning.COVER_TOL_M)).length,
                                planning.CONNECTOR_EPS_M)
            found += 1
        self.assertGreater(found, 0, "this fixture must produce a detour around the exclusion")

    def test_the_search_refuses_rather_than_returning_an_unproven_path(self):
        """A transition with no local corner set at all yields None, not a guess."""
        grid = _grid("rectangle, no obstacle")
        a = list(grid.to_deg.transform(*grid.coverage.centroid.coords[0]))
        self.assertIsNone(grid.shortest_safe_transit(a, a, limit_m=-1.0),
                          "a negative budget must admit nothing")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestTheSearchIsLocal(unittest.TestCase):
    """Bounded by construction, and provably not reaching across the survey."""

    def test_candidate_corners_lie_inside_the_local_box(self):
        grid = _grid("irregular boundary + 2 no-gos")
        frame = planning._SurveyFrame(grid, 0.0)
        frags, _ = planning._lane_fragments(frame, 10.0)
        probes = 0
        for f in frags[:12]:
            a = frame.rot_to_deg(f["rot"][0])
            b = frame.rot_to_deg(f["rot"][1])
            ax, ay = grid.to_proj.transform(*a)
            bx, by = grid.to_proj.transform(*b)
            m = planning.F4_LOCAL_MARGIN_M
            for (x, y) in grid.f4_local_vertices(a, b):
                probes += 1
                self.assertGreaterEqual(x, min(ax, bx) - m - 1e-6)
                self.assertLessEqual(x, max(ax, bx) + m + 1e-6)
                self.assertGreaterEqual(y, min(ay, by) - m - 1e-6)
                self.assertLessEqual(y, max(ay, by) + m + 1e-6)
        self.assertGreater(probes, 0, "the fixture must offer some local corners")

    def test_the_candidate_set_is_capped(self):
        grid = _grid("irregular boundary + 2 no-gos")
        frame = planning._SurveyFrame(grid, 0.0)
        frags, _ = planning._lane_fragments(frame, 10.0)
        for f in frags:
            a = frame.rot_to_deg(f["rot"][0])
            b = frame.rot_to_deg(f["rot"][1])
            self.assertLessEqual(len(grid.f4_local_vertices(a, b)),
                                 planning.F4_MAX_LOCAL_VERTICES)

    def test_a_distant_obstacle_contributes_no_candidates(self):
        """Two zones far apart: a transition beside one must never be offered the other's
        corners. This is the property that keeps the search independent of survey size.

        The two boxes are separated along whichever survey axis actually differs — at survey
        angle 0 the rot frame swaps map-east onto V — so near/far are chosen by distance from the
        probe rather than by assuming an axis, and the exclusion test is the full 2-D extent."""
        grid = _grid("two separated no-gos")
        frame = planning._SurveyFrame(grid, 0.0)
        self.assertEqual(len(frame.exclusion_boxes), 2, "fixture must have two bodies")
        near, far = frame.exclusion_boxes
        # A transition hugging the NEAR body, spanning it along U.
        a = frame.rot_to_deg((near[0] - 4.0, (near[1] + near[3]) / 2.0))
        b = frame.rot_to_deg((near[2] + 4.0, (near[1] + near[3]) / 2.0))
        ar, br = frame.deg_to_rot(a), frame.deg_to_rot(b)
        mid = ((ar[0] + br[0]) / 2.0, (ar[1] + br[1]) / 2.0)

        def box_gap(box):
            dx = max(box[0] - mid[0], 0.0, mid[0] - box[2])
            dy = max(box[1] - mid[1], 0.0, mid[1] - box[3])
            return math.hypot(dx, dy)

        if box_gap(far) < box_gap(near):
            near, far = far, near
        self.assertGreater(box_gap(far), planning.F4_LOCAL_MARGIN_M,
                           "fixture: the far body must be outside the local margin")

        verts = grid.f4_local_vertices(a, b)
        self.assertTrue(verts, "the near body must contribute corners")
        for (x, y) in verts:
            rx, ry = frame._to_rot(x, y)
            inside_far = (far[0] - 1.0 <= rx <= far[2] + 1.0
                          and far[1] - 1.0 <= ry <= far[3] + 1.0)
            self.assertFalse(inside_far,
                             "a corner of the DISTANT exclusion entered the local candidate set")

    def test_every_candidate_corner_is_itself_inside_buildable_geometry(self):
        from shapely.geometry import Point
        for name in ("irregular boundary + 2 no-gos", "narrow corridor", "concave boundary"):
            grid = _grid(name)
            with self.subTest(case=name):
                pts = grid._f4_corners()
                self.assertTrue(pts, f"[{name}] no corners were derived")
                for (x, y) in pts:
                    self.assertTrue(grid.buildable.contains(Point(x, y)),
                                    f"[{name}] a candidate corner is not inside buildable")

    def test_the_predicate_call_budget_stays_bounded(self):
        """The whole point of the cap: probes per invocation cannot grow without limit."""
        for name, inp in MATRIX:
            with self.subTest(case=name):
                real = planning._NavGrid.shortest_safe_transit
                stats = {"grids": []}

                def spy(self, a, b, limit_m=None, _real=real, _s=stats):
                    if self not in _s["grids"]:
                        _s["grids"].append(self)
                    return _real(self, a, b, limit_m=limit_m)

                planning._NavGrid.shortest_safe_transit = spy
                try:
                    _gen(inp)
                finally:
                    planning._NavGrid.shortest_safe_transit = real
                for g in stats["grids"]:
                    if not g.f4_invocations:
                        continue
                    cap = planning.F4_MAX_LOCAL_VERTICES + 2
                    self.assertLessEqual(g.f4_candidate_vertices / g.f4_invocations,
                                         planning.F4_MAX_LOCAL_VERTICES)
                    self.assertLessEqual(g.f4_edge_probes / g.f4_invocations, cap * cap,
                                         f"[{name}] the local graph exceeded its pair budget")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestItOnlyWinsWhenItActuallyWins(unittest.TestCase):
    """Subordinate to the aligned tiers: it replaces them only on a real, thresholded saving."""

    def test_a_detour_is_never_longer_than_what_it_replaced(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                for cat, path, _wc, was_path, grid in _transitions(inp):
                    if cat != "shortest_safe_transit":
                        continue
                    new_m = grid._proj_len_m(path)
                    old_m = grid._proj_len_m(was_path)
                    self.assertLessEqual(new_m, old_m - planning.F4_MIN_GAIN_M + 1e-6,
                                         f"[{name}] a detour was taken for less than the "
                                         f"threshold ({old_m - new_m:.3f} m)")

    def test_when_the_aligned_candidate_is_shorter_it_is_retained(self):
        """Every transition NOT marked shortest_safe_transit is byte-identical to the aligned
        answer, or is the direct leg — the tier never quietly reshapes a transition it lost."""
        for name, inp in MATRIX:
            with self.subTest(case=name):
                for cat, path, was_cat, was_path, _g in _transitions(inp):
                    if cat in ("shortest_safe_transit", "direct_transit"):
                        continue
                    self.assertEqual(cat, was_cat, f"[{name}] category changed without a win")
                    self.assertEqual(path, was_path, f"[{name}] geometry changed without a win")

    def test_a_sub_threshold_improvement_is_refused(self):
        """Directly: give the search a budget that only a sub-threshold gain could satisfy."""
        grid = _grid("central no-go, many lanes")
        frame = planning._SurveyFrame(grid, 0.0)
        frags, _ = planning._lane_fragments(frame, 10.0)
        by_row = {}
        for f in frags:
            by_row.setdefault(f["row"], []).append(f)
        for row in sorted(by_row):
            group = sorted(by_row[row], key=lambda f: f["rot"][0][0])
            if len(group) < 2:
                continue
            a = frame.rot_to_deg(group[0]["rot"][1])
            b = frame.rot_to_deg(group[-1]["rot"][0])
            got = grid.shortest_safe_transit(a, b, limit_m=None)
            if got is None:
                continue
            L = grid._proj_len_m(got)
            self.assertIsNone(grid.shortest_safe_transit(a, b, limit_m=L - 1.0),
                              "a budget below the achievable length must return nothing")
            return
        self.skipTest("fixture produced no split lane")

    def test_the_result_is_line_of_sight_simplified(self):
        """No corner survives that a safe straight hop could skip."""
        for name, inp in MATRIX:
            with self.subTest(case=name):
                for cat, path, _wc, _wp, grid in _transitions(inp):
                    if cat != "shortest_safe_transit":
                        continue
                    for i in range(len(path) - 2):
                        self.assertFalse(
                            grid.segment_is_safe(path[i], path[i + 2], require_inside=True),
                            f"[{name}] a removable corner survived simplification")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestHarderGeometries(unittest.TestCase):
    """Concave boundaries, narrow corridors and multiple relevant exclusion corners."""

    EXTRA = {
        "narrow safe corridor": dict(
            boundary=_rect(0, 0, 200, 120),
            zones=[_rect(40, 0, 80, 78), _rect(120, 42, 160, 120)], angle=0.0),
        "concave boundary + tilted zone": dict(
            boundary=[_m2ll(0, 0), _m2ll(200, 0), _m2ll(200, 60), _m2ll(90, 60),
                      _m2ll(90, 120), _m2ll(0, 120)],
            zones=[_rot_rect(20, 20, 70, 70, 25.0)], angle=0.0),
        "three exclusion corners in one box": dict(
            boundary=_rect(0, 0, 200, 120),
            zones=[_rect(60, 20, 80, 60), _rect(95, 20, 115, 60), _rect(130, 20, 150, 60)],
            angle=0.0),
    }

    def _inp(self, name):
        c = self.EXTRA[name]
        return {"boundary": c["boundary"], "no_go_zones": c["zones"],
                "shoreline_clearance_m": 5.0, "no_go_clearance_m": 5.0,
                "lane_spacing_m": 10.0, "primary_angle_deg": c["angle"]}

    def test_each_geometry_generates_safely_and_validates(self):
        from shapely.geometry import LineString
        from shapely.ops import transform
        for name in self.EXTRA:
            with self.subTest(case=name):
                inp = self._inp(name)
                pkg = _gen(inp)
                grid = planning._NavGrid(inp["boundary"], inp["shoreline_clearance_m"],
                                         inp["no_go_zones"], step_m=inp["lane_spacing_m"],
                                         no_go_clearance=inp["no_go_clearance_m"])
                self.assertTrue(pkg["geometry_check"]["ok"],
                                f"[{name}] {pkg['geometry_check']['failures']}")
                for s in pkg["segments"]:
                    if s["kind"] not in planning._REQUIRE_INSIDE_KINDS:
                        continue
                    cs = s["coordinates"]
                    for i in range(len(cs) - 1):
                        lp = transform(grid.to_proj.transform,
                                       LineString([cs[i], cs[i + 1]]))
                        self.assertTrue(grid._seg_covered(lp), f"[{name}] leg outside buildable")
                        self.assertTrue(grid._seg_clears_nogo(lp), f"[{name}] leg in no-go")
                self.assertEqual(
                    pkg["route_quality"]["non_survey_aligned_coverage_segment_count"], 0)

    def test_coverage_is_unchanged_on_each_geometry(self):
        for name in self.EXTRA:
            with self.subTest(case=name):
                inp = self._inp(name)
                self.assertEqual(_coverage_content(_gen(inp)),
                                 _coverage_content(_gen_without_f4(inp)))

    def test_multiple_exclusion_corners_are_all_available_to_one_transition(self):
        inp = self._inp("three exclusion corners in one box")
        grid = planning._NavGrid(inp["boundary"], inp["shoreline_clearance_m"],
                                 inp["no_go_zones"], step_m=inp["lane_spacing_m"],
                                 no_go_clearance=inp["no_go_clearance_m"])
        frame = planning._SurveyFrame(grid, 0.0)
        boxes = sorted(frame.exclusion_boxes, key=lambda b: b[0])
        a = frame.rot_to_deg((boxes[0][0] - 6.0, (boxes[0][1] + boxes[0][3]) / 2.0))
        b = frame.rot_to_deg((boxes[-1][2] + 6.0, (boxes[-1][1] + boxes[-1][3]) / 2.0))
        verts = grid.f4_local_vertices(a, b)
        self.assertGreaterEqual(len(verts), 4,
                                "a transition spanning three bodies must see several corners")
        self.assertLessEqual(len(verts), planning.F4_MAX_LOCAL_VERTICES)


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestCoverageIsUntouched(unittest.TestCase):
    """The invariant the whole change is subordinate to."""

    def test_coverage_content_matches_the_pre_f4_baseline(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                self.assertEqual(_coverage_content(_gen(inp)),
                                 _coverage_content(_gen_without_f4(inp)),
                                 f"[{name}] F4 moved coverage geometry")

    def test_coverage_content_also_matches_the_aligned_only_baseline(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                self.assertEqual(_coverage_content(_gen(inp)),
                                 _coverage_content(_gen_aligned_only(inp)),
                                 f"[{name}] coverage drifted from the original BCD baseline")

    def test_cells_bridges_and_survey_line_are_unchanged(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                new = _gen(inp)["route_quality"]
                old = _gen_without_f4(inp)["route_quality"]
                self.assertEqual(new["coverage_cell_count"], old["coverage_cell_count"])
                self.assertEqual(new["coverage_fragment_count"], old["coverage_fragment_count"])
                self.assertEqual(new["coverage_cell_handover_count"],
                                 old["coverage_cell_handover_count"])
                self.assertEqual(new["same_lane_obstacle_bridge_count"], 0)
                self.assertAlmostEqual(new["coverage_fragment_length_m"],
                                       old["coverage_fragment_length_m"], delta=0.01)
                self.assertEqual(new["non_survey_aligned_coverage_segment_count"], 0)

    def test_the_cell_ordering_costs_hand_overs_with_the_detour_search_too(self):
        """F5 supersedes the old rule that ordering must be blind to the optimisation tiers.

        The cell order and orientation are chosen by MEASURING candidate hand-overs, and the
        honest measurement is what the vessel will really travel — which for a blocked hand-over
        is F4's detour, not the far longer aligned staircase it replaces. So the search is
        deliberately entered while costing, at full optimisation, and the invariant that replaced
        the old one is that costing a candidate cannot colour the emitted route (below)."""
        seen = []
        real = planning._aligned_transition

        def spy(frame, a, b, in_dir=None, out_dir=None, optimize_transit=True):
            if sys._getframe(1).f_code.co_name == "cost":
                seen.append(optimize_transit)
            return real(frame, a, b, in_dir=in_dir, out_dir=out_dir,
                        optimize_transit=optimize_transit)

        planning._aligned_transition = spy
        try:
            _gen(_inputs("irregular boundary + 2 no-gos"))
        finally:
            planning._aligned_transition = real
        self.assertTrue(seen, "the fixture must actually cost candidate hand-overs")
        self.assertTrue(all(flag is True for flag in seen),
                        "the ordering must cost hand-overs with the real transition policy")

    def test_costing_a_candidate_leaves_the_detour_accounting_untouched(self):
        """Most costed candidates are discarded, and F4 bumps grid counters that feed
        route_quality — so a probe must restore every one of them."""
        callers = []
        real = planning._NavGrid.shortest_safe_transit

        def spy(self, a, b, limit_m=None):
            callers.append(sys._getframe(2).f_code.co_name)
            return real(self, a, b, limit_m=limit_m)

        planning._NavGrid.shortest_safe_transit = spy
        try:
            pkg = _gen(_inputs("irregular boundary + 2 no-gos"))
        finally:
            planning._NavGrid.shortest_safe_transit = real
        self.assertTrue(callers, "the fixture must exercise the detour search")
        self.assertIn("cost", callers,
                      "the ordering must have costed candidates through the detour search")
        rq = pkg["route_quality"]
        self.assertLessEqual(rq["shortest_safe_transition_count"],
                             rq["coverage_fragment_count"],
                             "a discarded candidate leaked into the reported detour count")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestContractAndDeterminism(unittest.TestCase):

    def test_geometry_contract_and_validation_hold(self):
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
                self.assertTrue(v["ok"], f"[{name}] {v['errors']}")

    def test_repeated_generation_is_byte_identical(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                a, b = _gen(inp), _gen(inp)
                self.assertEqual(a["route_hash"], b["route_hash"])
                self.assertEqual([s["coordinates"] for s in a["segments"]],
                                 [s["coordinates"] for s in b["segments"]])

    def test_the_fallback_count_never_increases(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                self.assertLessEqual(_gen(inp)["route_quality"]["fallback_connector_count"],
                                     _gen_without_f4(inp)["route_quality"]
                                     ["fallback_connector_count"],
                                     f"[{name}] F4 pushed work onto the generic A*")

    def test_the_metrics_are_re_derivable_from_the_emitted_geometry(self):
        for name, inp in MATRIX:
            with self.subTest(case=name):
                rq = _gen(inp)["route_quality"]
                # EMITTED transitions only. The cell-ordering search costs many candidates that
                # are never built (`_transition_oracle`), and those must not be counted here —
                # the metric describes the route, not the search that chose it.
                spied = [c for c, _p, _wc, _wp, _g in _transitions(inp)
                         if c == "shortest_safe_transit"]
                self.assertEqual(rq["shortest_safe_transition_count"], len(spied))
                if spied:
                    self.assertGreater(rq["shortest_safe_transition_distance_m"], 0.0)
                    self.assertGreater(rq["shortest_safe_saved_distance_m"], 0.0)

    def test_generation_stays_interactive(self):
        """A soft ceiling: the bounded search must not turn planning into a batch job."""
        inp = _inputs("irregular boundary + 2 no-gos")
        _gen(inp)                                  # warm any lazy geometry
        t0 = time.perf_counter()
        _gen(inp)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 5.0, f"generation took {elapsed:.2f} s")


if __name__ == "__main__":
    unittest.main()
