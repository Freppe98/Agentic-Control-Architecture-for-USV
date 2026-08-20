"""Survey-frame coverage tests for planning.py — the sonar-quality guarantee that a no-go
clearance REMOVES surveyable space instead of REORIENTING the survey pattern.

Run from operator-scripts/:  python -m unittest tests.test_survey_frame_coverage

THE REGRESSION THIS FILE PINS
-----------------------------
The ported Scout generator bridged a lane an obstacle had cut in two by walking ALONG the
obstacle boundary. With the operator's no-go clearance applied, that obstacle is the ROUND
buffered exclusion, so the "coverage" came out as a chain of ~1 m chords tracing a buffer arc —
safe, but with no stable sonar heading. TestCurrentUiRegression reproduces exactly the case that
was visible on the Plan page (rectangular survey, one central no-go, 5 m no-go clearance, ~42°
survey angle, 5 m lane spacing) and pins the corrected geometry.

WHAT IS ASSERTED THROUGHOUT
---------------------------
  * coverage legs are STRAIGHT and parallel to the survey angle (U), transitions are parallel to
    the perpendicular (V) — in the SURVEY frame, never snapped to geographic north/east;
  * no arc-following: no run of short legs whose heading creeps around a buffer;
  * the safety geometry is UNCHANGED — every leg is still re-proven inside the navigable region
    and outside the buffered no-go exclusion with planning's own predicates, and the buffered
    exclusion is still exactly `drawn zone ⊕ no_go_clearance_m`.

Angles are compared in PROJECTED METRIC coordinates with an angular TOLERANCE (never exact
float equality), and degenerate sub-metre legs are excluded from angle classification.
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import planning  # noqa: E402

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

# ── Fixtures ─────────────────────────────────────────────────────────────────────────────
# A plain rectangular survey (~366 m × 278 m) — the shape the Plan page was showing.
RECT = [[13.000, 56.699], [13.0060, 56.699], [13.0060, 56.7015], [13.000, 56.7015]]
# One central rectangular no-go (~61 m × 67 m).
CENTER_ZONE = [[13.0026, 56.7000], [13.0036, 56.7000], [13.0036, 56.7006], [13.0026, 56.7006]]
# An irregular (L-shaped, obliquely-cut) no-go: its edges run at arbitrary headings, so a
# boundary-following bypass would show up immediately as arbitrary-angle coverage.
IRREGULAR_ZONE = [[13.0024, 56.7001], [13.0033, 56.6999], [13.0038, 56.7004],
                  [13.0031, 56.7005], [13.0030, 56.7008], [13.0025, 56.7006]]
# A near-circular no-go: buffered, its exclusion boundary is entirely arcs.
ROUND_ZONE = [[13.0031 + 0.00055 * math.cos(math.radians(a)),
               56.7003 + 0.00030 * math.sin(math.radians(a))] for a in range(0, 360, 15)]
# A boundary with a narrow finger. After the 5 m shoreline clearance the finger is ~2 m wide, so
# lanes crossing it are clipped to slivers — the minimum-useful-fragment case, produced by the
# shoreline geometry rather than by a no-go zone (which at this width would DISCONNECT the region
# and be refused outright, a different and already-covered failure).
SPUR = [[13.000, 56.699], [13.0060, 56.699], [13.0060, 56.7015],
        [13.0032, 56.7015], [13.0032, 56.7024], [13.0030, 56.7024],
        [13.0030, 56.7015], [13.000, 56.7015]]

ANGLE_TOL_DEG = 6.0          # generous vs planning.SURVEY_ALIGN_TOL_DEG (5.0)
MIN_ANGLE_LEG_M = 1.5        # ignore degenerate legs when classifying a heading


def _inputs(**over):
    base = {"boundary": RECT, "shoreline_clearance_m": 5, "no_go_clearance_m": 5,
            "lane_spacing_m": 5, "primary_angle_deg": 42, "home": [12.9995, 56.6985]}
    base.update(over)
    return base


def _grid(inp):
    return planning._NavGrid(inp["boundary"], inp["shoreline_clearance_m"],
                            inp.get("no_go_zones") or [], step_m=inp["lane_spacing_m"],
                            no_go_clearance=inp.get("no_go_clearance_m", 0))


def _bearing(a_proj, b_proj):
    """Metric bearing of a projected leg, modulo 180° (a lane flown either way is one axis)."""
    return math.degrees(math.atan2(b_proj[0] - a_proj[0], b_proj[1] - a_proj[1])) % 180.0


def _axis_delta(brg, ref_deg):
    d = abs(brg - (ref_deg % 180.0)) % 180.0
    return min(d, 180.0 - d)


def _classify(a_proj, b_proj, angle_deg, tol=ANGLE_TOL_DEG):
    """"U" / "V" / "short" / "other" for one projected leg against a survey angle."""
    if math.hypot(b_proj[0] - a_proj[0], b_proj[1] - a_proj[1]) < MIN_ANGLE_LEG_M:
        return "short"
    brg = _bearing(a_proj, b_proj)
    if _axis_delta(brg, angle_deg) <= tol:
        return "U"
    if _axis_delta(brg, angle_deg + 90.0) <= tol:
        return "V"
    return "other"


def _coverage_legs(pkg, grid, kinds=("primary", "secondary")):
    """[(kind, a_proj, b_proj)] for EVERY leg of every coverage segment - survey fragments AND
    the transits between them. Use this for properties both must satisfy (safety, no arc
    following); use `_fragment_legs` for the survey-frame ALIGNMENT contract, which binds the
    survey fragments only."""
    out = []
    for s in pkg["segments"]:
        if s["kind"] not in kinds:
            continue
        pp = [grid.to_proj.transform(c[0], c[1]) for c in s["coordinates"]]
        out.extend((s["kind"], pp[i], pp[i + 1]) for i in range(len(pp) - 1))
    return out


def _split_coverage_legs(pkg, grid, kinds=("primary", "secondary"), pairs=None):
    """(fragment_legs, transition_legs), each [(kind, a_proj, b_proj)].

    A coverage segment is fragments AND transits concatenated. Only the FRAGMENTS are sonar
    passes and only they are bound by the survey-frame alignment contract; a transit is the
    vessel repositioning between two finished fragments and may take any safe heading. The
    split keys off the generator's own recorded fragment endpoints - which are cleanup anchors
    and so survive verbatim - so it is exact rather than inferred from geometry.

    `pairs` overrides where those endpoints come from. A fleet CHILD package carries no
    `route_quality` (its coverage is assembled from pre-clipped fleet survey lines, not from
    _survey_frame_coverage), so its caller supplies the line endpoints instead."""
    if pairs is None:
        pairs = {(tuple(f["start"]), tuple(f["end"]))
                 for f in pkg["route_quality"]["coverage_fragments"]}
    frags, trans = [], []
    for s in pkg["segments"]:
        if s["kind"] not in kinds:
            continue
        cs = s["coordinates"]
        for i in range(len(cs) - 1):
            a, b = cs[i], cs[i + 1]
            key = ((round(a[0], 7), round(a[1], 7)), (round(b[0], 7), round(b[1], 7)))
            leg = (s["kind"], grid.to_proj.transform(a[0], a[1]),
                   grid.to_proj.transform(b[0], b[1]))
            (frags if key in pairs else trans).append(leg)
    return frags, trans


def _fragment_legs(pkg, grid, kinds=("primary", "secondary"), pairs=None):
    """[(kind, a_proj, b_proj)] for the SURVEY FRAGMENT legs only - the sonar passes."""
    return _split_coverage_legs(pkg, grid, kinds, pairs)[0]


def _arc_run_lengths(legs, max_leg_m=3.0, min_run=3, drift_lo=3.0, drift_hi=60.0):
    """The travelled length of each arc-following run: `min_run`+ consecutive SHORT legs whose
    heading creeps in one direction — the signature of a path tracing a rounded buffer boundary.

    LENGTH, not just a count, because the two things that shape matches are not the same defect.
    A path that BRIDGES along an exclusion arc (what the ported generator did, and what this file
    exists to keep out) traces tens of metres of buffer as dozens of ~1 m chords. A bounded F4
    detour that steps round the corner of an exclusion at a lane turn produces the same local
    shape over a few metres and is exactly what F4 is for — it is a proven-safe transit, not
    coverage, and it never follows the arc beyond the obstruction. Measuring the span separates
    them; counting alone cannot."""
    spans = []
    i = 0
    n = len(legs)
    while i < n - 1:
        j = i
        sign = None
        while j < n - 1:
            (_, a1, b1), (_, a2, b2) = legs[j], legs[j + 1]
            if (math.hypot(b1[0] - a1[0], b1[1] - a1[1]) > max_leg_m
                    or math.hypot(b2[0] - a2[0], b2[1] - a2[1]) > max_leg_m):
                break
            delta = (_bearing(a2, b2) - _bearing(a1, b1) + 90.0) % 180.0 - 90.0
            if not (drift_lo <= abs(delta) <= drift_hi):
                break
            if sign is not None and (delta > 0) != sign:
                break
            sign = delta > 0
            j += 1
        if j - i + 1 >= min_run:
            spans.append(sum(math.hypot(b[0] - a[0], b[1] - a[1])
                             for _k, a, b in legs[i:j + 1]))
            i = j + 1
        else:
            i += 1
    return spans


def _arc_runs(legs, **kw):
    """How many arc-following runs, of any length."""
    return len(_arc_run_lengths(legs, **kw))


def _fragment_bearings(pkg, grid, pass_kind):
    """Bearing of each emitted coverage FRAGMENT of one pass (its straight start→end axis)."""
    out = []
    for f in pkg["route_quality"]["coverage_fragments"]:
        if f["pass_kind"] != pass_kind:
            continue
        a = grid.to_proj.transform(f["start"][0], f["start"][1])
        b = grid.to_proj.transform(f["end"][0], f["end"][1])
        if math.hypot(b[0] - a[0], b[1] - a[1]) >= MIN_ANGLE_LEG_M:
            out.append(_bearing(a, b))
    return out


def _assert_legs_safe(case, pkg, grid):
    """Every coverage leg is STILL inside the navigable region and STILL clear of the buffered
    no-go exclusion — proven with planning's own predicates, not with an alignment argument."""
    from shapely.geometry import LineString
    from shapely.ops import transform
    for kind, a, b in _coverage_legs(pkg, grid):
        lp = transform(grid.to_proj.transform,
                       LineString([grid.to_deg.transform(*a), grid.to_deg.transform(*b)]))
        case.assertTrue(grid._seg_covered(lp), f"{kind} leg leaves the navigable region")
        case.assertTrue(grid._seg_clears_nogo(lp), f"{kind} leg enters the no-go exclusion")


# ═══════════════════════════════════════════════════════════════════════════════════════
@requires_geometry
class TestNoNoGoZone(unittest.TestCase):
    """CASE 1 — with no no-go zone the coverage content is equivalent to the ported generator's
    and every coverage leg is survey-frame aligned."""

    def setUp(self):
        self.inp = _inputs()
        self.pkg = planning.generate_survey(self.inp, max_route_waypoints=5000)
        self.grid = _grid(self.inp)

    def test_coverage_line_content_matches_the_ported_generator(self):
        # The lane FAMILY is unchanged, so the total surveyed line length must match the ported
        # boustrophedon's own along-lane length. (Only the transitions between lanes differ, and
        # those are transit, not coverage.) This keeps run_lawnmower_with_obstacles a live,
        # asserted reference rather than a claim in a comment.
        #
        # The one ACCOUNTED-FOR difference is the wire margin: lanes are clipped to
        # `grid.coverage`, COVERAGE_EDGE_INSET_M inside the approved region, so each fragment is
        # trimmed at both ends (see planning.WIRE_MARGIN_M — coverage built flush against the
        # approved edge serializes to legs Scout rejects). That is asserted as a BOUND, not
        # absorbed into a wider delta: the lane COUNT must still match exactly, and the trim per
        # fragment must be positive and at most both ends' worth of inset amplified by the most
        # oblique boundary crossing worth allowing (1/sin 30° = 2x). A change to the lane family
        # itself moves the total by a whole lane (~280 m here) and cannot hide inside this.
        ported = planning._dedup(planning.run_lawnmower_with_obstacles(
            RECT, 5, 42, 5, None))
        pp = [self.grid.to_proj.transform(c[0], c[1]) for c in ported]
        ported_lanes = [(pp[i], pp[i + 1]) for i in range(len(pp) - 1)
                        if _classify(pp[i], pp[i + 1], 42) == "U"]
        ported_lane_m = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in ported_lanes)
        frags = self.pkg["route_quality"]["coverage_fragments"]
        new_lane_m = sum(f["length_m"] for f in frags)
        self.assertEqual(len(frags), len(ported_lanes),
                         "the survey-frame generator changed the lane family")
        trim_per_fragment = (ported_lane_m - new_lane_m) / len(frags)
        max_trim = 2 * planning.COVERAGE_EDGE_INSET_M / math.sin(math.radians(30))
        self.assertGreater(trim_per_fragment, 0.0,
                           "coverage is not clipped inside the approved region at all")
        self.assertLessEqual(trim_per_fragment, max_trim,
                             "the survey-frame generator changed the coverage line content")

    def test_lane_spacing_meaning_is_unchanged(self):
        frags = self.pkg["route_quality"]["coverage_fragments"]
        sweeps = sorted({f["sweep_coordinate"] for f in frags})
        gaps = [round(b - a, 2) for a, b in zip(sweeps, sweeps[1:])]
        self.assertTrue(gaps, "expected several lanes")
        for g in gaps:
            self.assertAlmostEqual(g, 5.0, delta=0.6, msg=f"lane spacing drifted to {g} m")

    def test_every_coverage_leg_is_survey_frame_aligned(self):
        legs = _fragment_legs(self.pkg, self.grid)
        self.assertGreater(len(legs), 15)
        for kind, a, b in legs:
            self.assertIn(_classify(a, b, 42), ("U", "V", "short"),
                          f"{kind} leg at {_bearing(a, b):.1f}° is not survey-frame aligned")
        rq = self.pkg["route_quality"]
        self.assertEqual(rq["non_survey_aligned_coverage_segment_count"], 0)
        self.assertEqual(rq["non_survey_aligned_segment_count"], 0)   # same, compat name

    def test_geometry_contract_holds(self):
        self.assertTrue(self.pkg["geometry_check"]["ok"])
        _assert_legs_safe(self, self.pkg, self.grid)


@requires_geometry
class TestRectangularNoGoInTheMiddle(unittest.TestCase):
    """CASE 2 — a central rectangular no-go with a 5 m clearance clips lanes into straight
    fragments; the transitions are survey-frame orthogonal; no curved leg is produced."""

    def setUp(self):
        self.inp = _inputs(no_go_zones=[CENTER_ZONE])
        self.pkg = planning.generate_survey(self.inp, max_route_waypoints=5000)
        self.grid = _grid(self.inp)

    def test_lanes_are_clipped_into_several_fragments(self):
        rq = self.pkg["route_quality"]
        no_zone = planning.generate_survey(_inputs(), max_route_waypoints=5000)
        self.assertGreater(rq["coverage_fragment_count"],
                           no_zone["route_quality"]["coverage_fragment_count"],
                           "the exclusion must SPLIT lanes into more fragments")

    def test_every_fragment_is_straight_and_parallel_to_the_survey_angle(self):
        for brg in _fragment_bearings(self.pkg, self.grid, "primary"):
            self.assertLessEqual(_axis_delta(brg, 42), ANGLE_TOL_DEG,
                                 f"a coverage fragment runs at {brg:.1f}°, not ≈42°")
        # …and each fragment really is a 2-point straight line, not a polyline.
        for f in self.pkg["route_quality"]["coverage_fragments"]:
            self.assertEqual(f["point_count"], 2)

    def test_no_arbitrary_angle_coverage_leg(self):
        for kind, a, b in _fragment_legs(self.pkg, self.grid):
            self.assertIn(_classify(a, b, 42), ("U", "V", "short"),
                          f"{kind} leg at {_bearing(a, b):.1f}° is not survey-frame aligned")
        self.assertEqual(
            self.pkg["route_quality"]["non_survey_aligned_coverage_segment_count"], 0)

    def test_no_curved_bypass(self):
        self.assertEqual(_arc_runs(_coverage_legs(self.pkg, self.grid)), 0,
                         "the coverage path still traces a rounded exclusion boundary")

    def test_transitions_are_built_by_the_bounded_tiers_not_the_generic_a_star(self):
        rq = self.pkg["route_quality"]
        built = (rq["direct_transit_transition_count"]
                 + rq["shortest_safe_transition_count"]
                 + rq["aligned_direct_transition_count"]
                 + rq["orthogonal_transition_count"])
        self.assertGreater(built, 0, "the exclusion must force real transitions")
        self.assertEqual(rq["fallback_connector_count"], 0,
                         "a transition fell through to the generic A* where a bounded tier "
                         "could have answered")

    def test_safety_geometry_is_unchanged(self):
        _assert_legs_safe(self, self.pkg, self.grid)
        self.assertTrue(self.pkg["geometry_check"]["ok"])

    def test_buffered_exclusion_is_still_the_authoritative_forbidden_geometry(self):
        # The exclusion is exactly `drawn zone ⊕ no_go_clearance_m`, round joins — not a
        # rectangle, not a Manhattan approximation, not shrunk to make routing easier.
        from shapely.geometry import Polygon
        from shapely.ops import transform
        drawn = transform(self.grid.to_proj.transform,
                          Polygon([(c[0], c[1]) for c in CENTER_ZONE]))
        expected = drawn.buffer(5.0)
        self.assertAlmostEqual(self.grid.nogo.area, expected.area, delta=1.0)
        self.assertTrue(self.grid.nogo.buffer(0.01).contains(expected.buffer(-0.01)))
        self.assertEqual(self.pkg["planning_inputs"]["no_go_zones"],
                         [planning._ring(CENTER_ZONE)])
        self.assertEqual(self.pkg["planning_inputs"]["no_go_clearance_m"], 5.0)


@requires_geometry
class TestSurveyAngles(unittest.TestCase):
    """CASES 3–5 — the survey frame follows the chosen angle and is never snapped to N/E."""

    def _run(self, angle):
        inp = _inputs(primary_angle_deg=angle, no_go_zones=[CENTER_ZONE])
        return planning.generate_survey(inp, max_route_waypoints=5000), _grid(inp)

    def test_case_3_rotated_angle_42_is_not_snapped_to_north_east(self):
        pkg, grid = self._run(42)
        frag_legs = [(a, b) for _, a, b in _fragment_legs(pkg, grid)
                     if _classify(a, b, 42) != "short"]
        us = [(a, b) for a, b in frag_legs if _classify(a, b, 42) == "U"]
        self.assertGreater(len(us), 10, "expected long coverage legs at ≈42°")
        self.assertEqual(len(us), len(frag_legs),
                         "every survey fragment must lie on the U axis of its own frame")
        for a, b in us:
            self.assertLessEqual(_axis_delta(_bearing(a, b), 42), ANGLE_TOL_DEG)
        # Cross-lane MOVEMENT still happens on every lane turn. It is asserted from the
        # transition tally rather than from leg bearings, because a transit is free to take the
        # short direct heading instead of the 132° V axis - see _aligned_transition tier 0.
        rq = pkg["route_quality"]
        moves = (rq["direct_transit_transition_count"] + rq["aligned_direct_transition_count"]
                 + rq["orthogonal_transition_count"])
        self.assertGreater(moves, 5, "expected cross-lane transitions between the fragments")
        # Nothing sits on a geographic axis: 42/132 are far from 0/90, so a single FRAGMENT near
        # north or east would prove a snap to the projection grid. (A transit legitimately may
        # run at any heading, so the snap test is a statement about the survey lanes.)
        for a, b in frag_legs:
            brg = _bearing(a, b)
            self.assertGreater(min(_axis_delta(brg, 0.0), _axis_delta(brg, 90.0)), 20.0,
                               f"a coverage leg at {brg:.1f}° looks snapped to N/E")

    def test_case_4_angle_0_is_axis_aligned_in_the_projected_frame(self):
        pkg, grid = self._run(0)
        for _kind, a, b in _fragment_legs(pkg, grid):
            self.assertIn(_classify(a, b, 0), ("U", "V", "short"),
                          f"a coverage leg at {_bearing(a, b):.1f}° is not axis aligned")
        self.assertEqual(
            pkg["route_quality"]["non_survey_aligned_coverage_segment_count"], 0)
        self.assertTrue(any(_axis_delta(_bearing(a, b), 0.0) <= ANGLE_TOL_DEG
                            and math.hypot(b[0] - a[0], b[1] - a[1]) > 20
                            for _k, a, b in _fragment_legs(pkg, grid)),
                        "expected long lanes running ≈north at survey angle 0")

    def test_case_5_angle_90_swaps_the_orientation(self):
        pkg, grid = self._run(90)
        for _kind, a, b in _fragment_legs(pkg, grid):
            self.assertIn(_classify(a, b, 90), ("U", "V", "short"))
        self.assertEqual(
            pkg["route_quality"]["non_survey_aligned_coverage_segment_count"], 0)
        # The LANES (not every leg — a bypass step is a long V leg by design) run ≈east/west.
        brgs = _fragment_bearings(pkg, grid, "primary")
        self.assertTrue(brgs)
        for brg in brgs:
            self.assertLessEqual(_axis_delta(brg, 90.0), ANGLE_TOL_DEG,
                                 f"at survey angle 90 a lane runs at {brg:.1f}°, not ≈90°")

    def test_angle_0_and_90_lanes_are_perpendicular_to_each_other(self):
        pkg0, grid0 = self._run(0)
        pkg90, grid90 = self._run(90)
        b0 = _fragment_bearings(pkg0, grid0, "primary")[0]
        b90 = _fragment_bearings(pkg90, grid90, "primary")[0]
        self.assertLessEqual(abs(_axis_delta(b0, b90) - 90.0), 2 * ANGLE_TOL_DEG,
                             f"lanes at survey angle 0 ({b0:.1f}°) and 90 ({b90:.1f}°) "
                             f"are not perpendicular")


@requires_geometry
class TestIrregularAndRoundedExclusions(unittest.TestCase):
    """CASES 6–7 — the exclusion keeps its true buffered shape, and the coverage never traces it."""

    def test_case_6_irregular_polygon_no_go(self):
        inp = _inputs(no_go_zones=[IRREGULAR_ZONE])
        pkg = planning.generate_survey(inp, max_route_waypoints=5000)
        grid = _grid(inp)
        # The safety exclusion is the true buffered irregular shape…
        from shapely.geometry import Polygon
        from shapely.ops import transform
        drawn = transform(grid.to_proj.transform,
                          Polygon([(c[0], c[1]) for c in IRREGULAR_ZONE]).buffer(0))
        self.assertAlmostEqual(grid.nogo.area, drawn.buffer(5.0).area, delta=1.0)
        # …and no coverage leg follows any of its arbitrary-heading edges.
        for kind, a, b in _fragment_legs(pkg, grid):
            self.assertIn(_classify(a, b, 42), ("U", "V", "short"),
                          f"{kind} leg at {_bearing(a, b):.1f}° traces the zone outline")
        self.assertEqual(
            pkg["route_quality"]["non_survey_aligned_coverage_segment_count"], 0)
        # NO COVERAGE leg may follow the rounded buffer boundary — a survey pass is straight.
        self.assertEqual(_arc_runs(_fragment_legs(pkg, grid)), 0)
        # And no TRANSIT may bridge ALONG it either. A transit is allowed to step round the
        # obstruction — that is what the bounded detour tier does at a lane turn whose straight
        # hop is blocked, and it is local by construction — but it must never trace the arc the
        # way the ported generator's bridge did, which ran the LENGTH of the exclusion.
        #
        # The two are separated by an order of magnitude, so the ceiling is not a fitted number:
        # the detour this fixture produces spans 6.0 m (a 5.98 m path where the straight hop is
        # 5.03 m), while this zone is ~60 m across, which is what a boundary-following bridge
        # would have to trace. Two lane spacings sits in the gap with margin either side.
        for span in _arc_run_lengths(_coverage_legs(pkg, grid)):
            self.assertLess(span, 2 * inp["lane_spacing_m"],
                            f"a transit traced {span:.1f} m of the rounded exclusion boundary")
        _assert_legs_safe(self, pkg, grid)

    def test_case_7_rounded_buffered_exclusion(self):
        inp = _inputs(no_go_zones=[ROUND_ZONE])
        pkg = planning.generate_survey(inp, max_route_waypoints=5000)
        grid = _grid(inp)
        # The exclusion boundary really is arc-like (many vertices, no long straight edge)…
        ring = pkg["no_go_exclusion_rings"][0]
        self.assertGreater(len(ring), 30, "expected a rounded, many-vertex exclusion boundary")
        # …yet every coverage leg stays straight and on a survey axis.
        for kind, a, b in _fragment_legs(pkg, grid):
            self.assertIn(_classify(a, b, 42), ("U", "V", "short"),
                          f"{kind} leg at {_bearing(a, b):.1f}° follows the arc")
        self.assertEqual(
            pkg["route_quality"]["non_survey_aligned_coverage_segment_count"], 0)
        self.assertEqual(_arc_runs(_coverage_legs(pkg, grid)), 0,
                         "the coverage path is still following the buffer arc")
        _assert_legs_safe(self, pkg, grid)


# ── connector-level cases (CASES 8–11, 14) ───────────────────────────────────────────────
@requires_geometry
class TestAlignedTransition(unittest.TestCase):
    """The connector priority itself, exercised directly on _aligned_transition so each tier is
    pinned rather than inferred from a whole mission.

    These cases pin the ALIGNED tiers (1-4), so they call the generator with
    `optimize_transit=False`. Without it the direct-safe tier 0 would answer first wherever a
    straight leg is safe and the orthogonal / bypass ordering below it would never be exercised.
    Tier 0 has its own coverage in tests/test_transition_policy.py."""

    ANGLE = 42.0

    def _aligned(self, a, b, **kw):
        """_aligned_transition with tier 0 suppressed - see the class docstring."""
        return planning._aligned_transition(self.frame, a, b, optimize_transit=False, **kw)

    def setUp(self):
        self.inp = _inputs(no_go_zones=[CENTER_ZONE])
        self.grid = _grid(self.inp)
        self.frame = planning._SurveyFrame(self.grid, self.ANGLE)
        self.box = self.frame.exclusion_boxes[0]

    def _rot(self, du, dv):
        """A degree point at (exclusion-box-relative) survey-frame offsets, so the test can place
        points precisely with respect to the buffered exclusion."""
        x0, y0, x1, y1 = self.box
        return self.frame.rot_to_deg((x0 + du, y0 + dv))

    def _legs_are_safe_and_aligned(self, path):
        for p, q in zip(path, path[1:]):
            self.assertTrue(self.grid.segment_is_safe(p, q, require_inside=True),
                            "an emitted transition leg is not safe")
        pp = [self.grid.to_proj.transform(c[0], c[1]) for c in path]
        for i in range(len(pp) - 1):
            self.assertIn(_classify(pp[i], pp[i + 1], self.ANGLE), ("U", "V", "short"),
                          f"transition leg at {_bearing(pp[i], pp[i+1]):.1f}° is not aligned")

    def test_case_8_direct_connector_crossing_the_exclusion_is_rejected(self):
        x1 = self.box[2] - self.box[0]
        a = self._rot(-8.0, 30.0)         # left of the exclusion, mid height
        b = self._rot(x1 + 8.0, 30.0)     # right of it, SAME lane → the direct leg cuts through
        self.assertFalse(self.grid.segment_is_safe(a, b, require_inside=True),
                         "fixture invalid: the direct connector must cross the exclusion")
        path, category = self._aligned(a, b)
        self.assertNotEqual([list(a), list(b)], [list(p) for p in path],
                            "the direct connector was emitted even though it crosses the no-go")
        self.assertIn(category, ("orthogonal", "bypass"))
        self._legs_are_safe_and_aligned(path)

    def test_case_9_bend_order_is_chosen_not_assumed(self):
        # V-then-U valid, U-then-V invalid: the "U first" corner lands inside the exclusion.
        x_span = self.box[2] - self.box[0]
        y_span = self.box[3] - self.box[1]
        a = self._rot(-12.0, y_span * 0.5)
        b = self._rot(x_span * 0.5, y_span + 12.0)
        u_then_v_corner = self._rot(x_span * 0.5, y_span * 0.5)
        self.assertFalse(self.grid.point_clears_nogo(u_then_v_corner),
                         "fixture invalid: the U-first bend point must be inside the exclusion")
        path, category = self._aligned(a, b)
        self.assertEqual(category, "orthogonal")
        self.assertEqual(len(path), 3, "expected a two-leg orthogonal transition")
        self._legs_are_safe_and_aligned(path)
        # It chose V first: the bend point shares the START's along-U coordinate.
        bend = self.frame.deg_to_rot(path[1])
        self.assertAlmostEqual(bend[0], self.frame.deg_to_rot(a)[0], delta=0.5)

        # …and the mirror image picks the OTHER order, proving neither is hard-coded.
        a2 = self._rot(x_span * 0.5, -12.0)
        b2 = self._rot(x_span + 12.0, y_span * 0.5)
        path2, category2 = self._aligned(a2, b2)
        self.assertEqual(category2, "orthogonal")
        bend2 = self.frame.deg_to_rot(path2[1])
        self.assertAlmostEqual(bend2[0], self.frame.deg_to_rot(b2)[0], delta=0.5,
                               msg="expected the U-first bend order here")
        self._legs_are_safe_and_aligned(path2)

    def test_case_10a_both_orthogonal_variants_blocked_falls_to_a_proven_bypass(self):
        # Same lane, opposite sides of the exclusion: both L orders degenerate into the direct
        # (blocked) line, so only a bypass staircase or the generic connector can serve.
        x_span = self.box[2] - self.box[0]
        y_span = self.box[3] - self.box[1]
        a = self._rot(-6.0, y_span * 0.5)
        b = self._rot(x_span + 6.0, y_span * 0.5)
        path, category = self._aligned(a, b)
        self.assertIn(category, ("bypass", "fallback"))
        for p, q in zip(path, path[1:]):
            self.assertTrue(self.grid.segment_is_safe(p, q, require_inside=True))
        if category == "bypass":
            self._legs_are_safe_and_aligned(path)

    def test_case_10b_unroutable_transition_fails_closed(self):
        # A target sealed off by no-go geometry yields ConnectorError — never a "best effort" leg.
        sealed = _inputs(no_go_zones=[
            [[13.0028, 56.7002], [13.0034, 56.7002], [13.0034, 56.70025], [13.0028, 56.70025]],
            [[13.0028, 56.7005], [13.0034, 56.7005], [13.0034, 56.70055], [13.0028, 56.70055]],
            [[13.0028, 56.7002], [13.00285, 56.7002], [13.00285, 56.70055], [13.0028, 56.70055]],
            [[13.00335, 56.7002], [13.0034, 56.7002], [13.0034, 56.70055], [13.00335, 56.70055]],
        ])
        grid = _grid(sealed)
        frame = planning._SurveyFrame(grid, self.ANGLE)
        inside = [13.0031, 56.70038]      # sealed inside the ring of zones
        outside = [13.0015, 56.7004]
        with self.assertRaises(planning.ConnectorError):
            planning._aligned_transition(frame, outside, inside)

    def test_case_11_transition_near_the_shoreline_inset_stays_navigable(self):
        # Real lane-turn transitions: a lane fragment ENDS on the shoreline inset edge, so the
        # turn to the next lane is the connector most at risk of stepping outside it. Every leg of
        # every such turn must stay inside the navigable region — the inset is never relaxed.
        from shapely.geometry import LineString
        from shapely.ops import transform
        fragments, _skipped = planning._lane_fragments(self.frame, 5)
        self.assertGreater(len(fragments), 10)
        checked = 0
        for prev, nxt in zip(fragments, fragments[1:]):
            a = self.frame.rot_to_deg(prev["rot"][1])   # end of one lane, on the inset edge
            b = self.frame.rot_to_deg(nxt["rot"][1])    # end of the next lane, also on the edge
            if planning._close(a, b):
                continue
            path, _category = self._aligned(a, b)
            for p, q in zip(path, path[1:]):
                lp = transform(self.grid.to_proj.transform, LineString([p, q]))
                self.assertTrue(self.grid._seg_covered(lp),
                                "a transition leg left the navigable (shoreline-inset) region")
                self.assertTrue(self.grid._seg_clears_nogo(lp))
            checked += 1
        self.assertGreater(checked, 10)

    def test_case_14_safe_endpoints_but_a_crossing_leg_is_rejected(self):
        # Both endpoints clear the exclusion; the straight leg between them does not. The SEGMENT
        # predicate — not a waypoint check — is what rejects it, and generation honours that.
        x_span = self.box[2] - self.box[0]
        y_span = self.box[3] - self.box[1]
        a = self._rot(-8.0, y_span * 0.5)
        b = self._rot(x_span + 8.0, y_span * 0.5)
        self.assertTrue(self.grid.point_clears_nogo(a))
        self.assertTrue(self.grid.point_clears_nogo(b))
        self.assertFalse(self.grid.segment_is_safe(a, b, require_inside=True))
        path, _c = self._aligned(a, b)
        self.assertGreater(len(path), 2)
        # …and check_mission_geometry independently refuses a hand-built crossing segment.
        result = planning.check_mission_geometry(
            segments=[{"segment_id": "seg-01-primary", "kind": "primary",
                       "coordinates": [list(a), list(b)]}],
            route_waypoints=planning._route_waypoints([a, b]),
            navigable_geometry=planning._navigable_rings_deg(RECT, 5),
            no_go_zones=[planning._ring(CENTER_ZONE)], no_go_clearance_m=5,
            planning_home=None, home_corridor=None)
        self.assertFalse(result["ok"])
        self.assertIn("ROUTE_NO_GO_VIOLATION", {f["code"] for f in result["failures"]})


# ── minimum useful fragment (CASE 12) ────────────────────────────────────────────────────
@requires_geometry
class TestMinimumUsefulFragment(unittest.TestCase):
    """CASE 12 — a lane clipped to a sliver at an obstacle corner is handled by an explicit,
    reported rule rather than becoming a meaningless rapid-turn micro-route."""

    def test_the_rule_is_tied_to_lane_spacing_and_the_cleanup_tolerance(self):
        self.assertEqual(planning._min_useful_fragment_m(5), 1.25)
        self.assertEqual(planning._min_useful_fragment_m(10), 2.5)
        self.assertEqual(planning._min_useful_fragment_m(20), 5.0)
        # …and never below the existing near-duplicate spacing, whatever the spacing.
        self.assertEqual(planning._min_useful_fragment_m(1), planning.CLEANUP_MIN_SPACING_M)
        self.assertEqual(planning._min_useful_fragment_m(0), planning.CLEANUP_MIN_SPACING_M)

    def _sliver_inputs(self):
        """A survey whose boundary carries a narrow finger: after the 5 m shoreline clearance the
        finger is ~2 m wide, so the lanes that cross it are clipped to slivers well under the
        2.5 m minimum useful fragment for a 10 m lane spacing — an obstacle-corner sliver, without
        needing a no-go zone that would disconnect the region."""
        return {"boundary": SPUR, "shoreline_clearance_m": 5, "no_go_clearance_m": 5,
                "lane_spacing_m": 10, "primary_angle_deg": 90, "home": [12.9995, 56.6985]}

    def test_sliver_fragments_are_dropped_counted_and_reported(self):
        inp = self._sliver_inputs()
        frame = planning._SurveyFrame(_grid(inp), 90)
        fragments, skipped = planning._lane_fragments(frame, 10)
        self.assertGreater(skipped["count"], 0, "fixture invalid: expected sliver fragments")
        min_useful = planning._min_useful_fragment_m(10)
        for f in fragments:
            self.assertGreaterEqual(f["length_m"], min_useful,
                                    "a sub-useful fragment survived the filter")
        # The loss is bounded by the rule itself, never an arbitrary discard.
        self.assertLess(skipped["length_m"], skipped["count"] * min_useful)

    def test_generation_reports_the_dropped_fragments(self):
        inp = self._sliver_inputs()
        pkg = planning.generate_survey(inp, max_route_waypoints=9000)
        rq = pkg["route_quality"]
        self.assertGreater(rq["skipped_short_fragment_count"], 0)
        self.assertGreater(rq["skipped_short_fragment_length_m"], 0)
        self.assertEqual(rq["minimum_useful_fragment_m"], 2.5)
        self.assertTrue(any("shorter than" in w for w in pkg["warnings"]),
                        "dropping coverage fragments must be stated, not hidden")
        # Dropping slivers must not open a real coverage gap: the loss stays a rounding error
        # against the surveyed line length.
        kept = sum(f["length_m"] for f in rq["coverage_fragments"])
        self.assertLess(rq["skipped_short_fragment_length_m"], 0.01 * kept)
        # …and the mission is still a valid, fully proven mission.
        self.assertTrue(pkg["geometry_check"]["ok"])
        self.assertEqual(rq["non_survey_aligned_segment_count"], 0)

    def test_counters_are_zero_when_nothing_is_dropped(self):
        rq = planning.generate_survey(_inputs(no_go_zones=[CENTER_ZONE]),
                                      max_route_waypoints=5000)["route_quality"]
        self.assertEqual(rq["skipped_short_fragment_count"], 0)
        self.assertEqual(rq["skipped_short_fragment_length_m"], 0.0)

    def test_no_micro_route_survives_at_a_corner(self):
        inp = _inputs(no_go_zones=[IRREGULAR_ZONE])
        pkg = planning.generate_survey(inp, max_route_waypoints=5000)
        min_useful = pkg["route_quality"]["minimum_useful_fragment_m"]
        for f in pkg["route_quality"]["coverage_fragments"]:
            self.assertGreaterEqual(f["length_m"], min_useful)


# ── dual pass (CASE 13) ──────────────────────────────────────────────────────────────────
@requires_geometry
class TestDualPass(unittest.TestCase):
    """CASE 13 — each pass is generated in its OWN survey frame and handles the no-go itself."""

    def setUp(self):
        self.inp = _inputs(primary_angle_deg=30, dual_pass=True, no_go_zones=[CENTER_ZONE])
        self.pkg = planning.generate_survey(self.inp, max_route_waypoints=8000)
        self.grid = _grid(self.inp)

    def test_secondary_angle_is_primary_plus_90(self):
        self.assertEqual(self.pkg["planning_inputs"]["secondary_angle_deg"], 120.0)

    def test_each_pass_is_aligned_to_its_own_angle(self):
        for brg in _fragment_bearings(self.pkg, self.grid, "primary"):
            self.assertLessEqual(_axis_delta(brg, 30.0), ANGLE_TOL_DEG,
                                 f"a primary fragment runs at {brg:.1f}°, not ≈30°")
        sec = _fragment_bearings(self.pkg, self.grid, "secondary")
        self.assertTrue(sec, "expected secondary-pass fragments")
        for brg in sec:
            self.assertLessEqual(_axis_delta(brg, 120.0), ANGLE_TOL_DEG,
                                 f"a secondary fragment runs at {brg:.1f}°, not ≈120°")

    def test_no_first_pass_orientation_leaks_into_the_second_pass(self):
        # Every survey FRAGMENT is aligned to THAT segment's own frame. Transits between
        # fragments are excluded on purpose - they carry no survey heading to leak.
        for kind, angle in (("primary", 30.0), ("secondary", 120.0)):
            legs = _fragment_legs(self.pkg, self.grid, kinds=(kind,))
            self.assertTrue(legs, f"expected {kind} fragments")
            for _k, a, b in legs:
                self.assertIn(_classify(a, b, angle), ("U", "V", "short"),
                              f"{kind} leg at {_bearing(a, b):.1f}° is not "
                              f"aligned to its own {angle}° frame")
        self.assertEqual(
            self.pkg["route_quality"]["non_survey_aligned_coverage_segment_count"], 0)

    def test_both_passes_split_around_the_exclusion_independently(self):
        frags = self.pkg["route_quality"]["coverage_fragments"]
        prim_rows = {f["sweep_coordinate"] for f in frags if f["pass_kind"] == "primary"}
        sec_rows = {f["sweep_coordinate"] for f in frags if f["pass_kind"] == "secondary"}
        prim = [f for f in frags if f["pass_kind"] == "primary"]
        sec = [f for f in frags if f["pass_kind"] == "secondary"]
        self.assertGreater(len(prim), len(prim_rows), "the exclusion must split primary lanes")
        self.assertGreater(len(sec), len(sec_rows), "the exclusion must split secondary lanes")

    def test_geometry_contract_holds_for_both_passes(self):
        self.assertTrue(self.pkg["geometry_check"]["ok"])
        _assert_legs_safe(self, self.pkg, self.grid)


# ── whole-mission cases (CASE 15 + the UI regression) ────────────────────────────────────
@requires_geometry
class TestE2LikeMission(unittest.TestCase):
    """CASE 15 — a realistic mission (home, approach, return, one central no-go, 5 m clearance,
    ~42° survey angle) generates, validates and is lawnmower-aligned."""

    def setUp(self):
        self.inp = {
            "boundary": RECT, "no_go_zones": [CENTER_ZONE],
            "shoreline_clearance_m": 5, "no_go_clearance_m": 5, "lane_spacing_m": 8,
            "primary_angle_deg": 42, "home": [12.9995, 56.6985],
            "approach_waypoints": [[13.0005, 56.6992], [13.0010, 56.6996]],
            "return_waypoints": [[13.0010, 56.6996], [13.0005, 56.6992]],
        }
        self.pkg = planning.generate_survey(self.inp, max_route_waypoints=5000)
        self.grid = _grid(self.inp)

    def test_mission_is_valid(self):
        self.assertTrue(self.pkg["ok"])
        self.assertTrue(self.pkg["geometry_check"]["ok"])
        v = planning.validate_plan({**self.inp,
                                    "segments": self.pkg["segments"],
                                    "route_waypoints": self.pkg["route_waypoints"],
                                    "route_hash": self.pkg["route_hash"],
                                    "input_revision": self.pkg["input_revision"],
                                    "home_corridor": self.pkg["home_corridor"]},
                                   max_route_waypoints=5000)
        self.assertTrue(v["ok"], f"validation failed: {v['errors']}")

    def test_coverage_is_lawnmower_aligned_around_the_no_go(self):
        for kind, a, b in _fragment_legs(self.pkg, self.grid):
            self.assertIn(_classify(a, b, 42), ("U", "V", "short"),
                          f"{kind} leg at {_bearing(a, b):.1f}° is not survey-frame aligned")
        self.assertEqual(
            self.pkg["route_quality"]["non_survey_aligned_coverage_segment_count"], 0)
        # No leg of the segment - fragment or transit - may trace the buffered exclusion arc.
        self.assertEqual(_arc_runs(_coverage_legs(self.pkg, self.grid)), 0)

    def test_transit_geometry_is_untouched_by_the_survey_frame_rule(self):
        # Approach/return/home legs are TRANSIT, not sonar coverage: they keep their existing
        # semantics and are deliberately NOT required to be survey-frame orthogonal.
        kinds = {s["kind"] for s in self.pkg["segments"]}
        self.assertIn("start_connector", kinds)
        self.assertIn("return_approach", kinds)
        self.assertIn("final_home_connector", kinds)
        for wp in self.inp["approach_waypoints"]:
            self.assertTrue(any(planning._close([w["longitude"], w["latitude"]], wp)
                                for w in self.pkg["route_waypoints"]),
                            "an operator approach waypoint was moved or dropped")

    def test_route_identity_is_still_derived_the_same_way(self):
        import mission_contract
        self.assertEqual(self.pkg["route_hash"],
                         mission_contract.route_content_hash(self.pkg["route_waypoints"]))
        self.assertEqual(self.pkg["contract_version"], "mission-contract-v1")


@requires_geometry
class TestCurrentUiRegression(unittest.TestCase):
    """THE KEY REGRESSION — the exact case visible on the Plan page: rectangular survey, one
    central no-go, 5 m no-go clearance, ~42° survey angle, 5 m lane spacing.

    Before: the route locally followed the rounded/diagonal buffered obstacle geometry.
    After:  coverage legs are straight and parallel to the survey angle, the obstacle removes
            portions of lanes, transitions use survey-frame orthogonal geometry, and the no-go
            buffer is fully respected."""

    def setUp(self):
        self.inp = _inputs(lane_spacing_m=5, primary_angle_deg=42, no_go_zones=[CENTER_ZONE])
        self.pkg = planning.generate_survey(self.inp, max_route_waypoints=5000)
        self.grid = _grid(self.inp)
        self.legs = _coverage_legs(self.pkg, self.grid)

    def test_no_coverage_leg_follows_the_rounded_obstacle_geometry(self):
        # Arc-following is forbidden for EVERY leg of the coverage segment, transits included...
        self.assertEqual(_arc_runs(self.legs), 0,
                         "the coverage path still traces the buffered exclusion boundary")
        # ...while an arbitrary HEADING is a defect only for the survey fragments.
        self.assertEqual(
            self.pkg["route_quality"]["non_survey_aligned_coverage_segment_count"], 0)
        offenders = [(round(_bearing(a, b), 1), round(math.hypot(b[0] - a[0], b[1] - a[1]), 1))
                     for _k, a, b in _fragment_legs(self.pkg, self.grid)
                     if _classify(a, b, 42) == "other"]
        self.assertEqual(offenders, [], f"arbitrary-angle coverage legs: {offenders[:10]}")

    def test_coverage_legs_are_straight_and_parallel_to_the_survey_angle(self):
        brgs = _fragment_bearings(self.pkg, self.grid, "primary")
        self.assertGreater(len(brgs), 30)
        for brg in brgs:
            self.assertLessEqual(_axis_delta(brg, 42), ANGLE_TOL_DEG)

    def test_the_obstacle_removes_lane_length_rather_than_bending_lanes(self):
        no_zone = planning.generate_survey(_inputs(lane_spacing_m=5, primary_angle_deg=42),
                                           max_route_waypoints=5000)
        with_zone_m = sum(f["length_m"] for f in self.pkg["route_quality"]["coverage_fragments"])
        no_zone_m = sum(f["length_m"] for f in no_zone["route_quality"]["coverage_fragments"])
        self.assertLess(with_zone_m, no_zone_m, "the exclusion must REMOVE surveyable line")
        self.assertGreater(with_zone_m, 0.8 * no_zone_m,
                           "far more coverage was lost than the exclusion actually occupies")

    def test_transitions_are_built_by_the_bounded_tiers_and_need_no_fallback(self):
        rq = self.pkg["route_quality"]
        built = (rq["direct_transit_transition_count"]
                 + rq["shortest_safe_transition_count"]
                 + rq["aligned_direct_transition_count"]
                 + rq["orthogonal_transition_count"])
        self.assertGreater(built, 0)
        self.assertEqual(rq["fallback_connector_count"], 0)

    def test_the_no_go_buffer_is_fully_respected(self):
        _assert_legs_safe(self, self.pkg, self.grid)
        self.assertTrue(self.pkg["geometry_check"]["ok"])
        for wp in self.pkg["route_waypoints"]:
            self.assertTrue(self.grid.point_clears_nogo([wp["longitude"], wp["latitude"]]))

    def test_generation_is_deterministic(self):
        again = planning.generate_survey(self.inp, max_route_waypoints=5000)
        self.assertEqual(self.pkg["route_waypoints"], again["route_waypoints"])
        self.assertEqual(self.pkg["route_hash"], again["route_hash"])
        self.assertEqual(self.pkg["route_quality"], again["route_quality"])

    def test_provenance_names_the_survey_frame_generator(self):
        alg = self.pkg["generation_algorithm"]
        self.assertEqual(alg["coverage"], "survey-frame-boustrophedon-v1")
        self.assertEqual(alg["coverage_transitions"],
                         "shortest-safe-local-direct-first-survey-frame-orthogonal-v3")
        self.assertEqual(alg["coverage_lane_family"], "ported-scout-boustrophedon-v1")


@requires_geometry
class TestFleetConsistency(unittest.TestCase):
    """A child mission must not route around a no-go zone by a different rule from a
    single-vehicle mission — both go through the same shared helpers."""

    def setUp(self):
        import fleet_planning
        self.fleet_planning = fleet_planning
        self.inp = {
            "boundary": RECT, "no_go_zones": [CENTER_ZONE],
            "shoreline_clearance_m": 5, "no_go_clearance_m": 5, "lane_spacing_m": 10,
            "primary_angle_deg": 42,
            "vehicles": [{"vehicle_id": "usv-1", "home": [12.9995, 56.6985]},
                         {"vehicle_id": "usv-2", "home": [13.0065, 56.7020]}],
        }
        self.plan = fleet_planning.generate_fleet(self.inp, max_route_waypoints=5000)

    def test_fleet_survey_lines_come_from_the_shared_lane_clipper(self):
        grid = _grid(self.inp)
        frame = planning._SurveyFrame(grid, 42)
        fragments, _skipped = planning._lane_fragments(frame, 10)
        lines = self.fleet_planning._survey_lines(grid, RECT, 10, 42, 5, [CENTER_ZONE], "pass-1")
        self.assertEqual(len(lines), len(fragments),
                         "fleet survey lines diverged from the shared lane fragments")
        for ln in lines:
            a = grid.to_proj.transform(*ln["start_deg"])
            b = grid.to_proj.transform(*ln["end_deg"])
            self.assertLessEqual(_axis_delta(_bearing(a, b), 42), ANGLE_TOL_DEG,
                                 "a fleet survey line is not parallel to the survey angle")
            self.assertGreaterEqual(ln["length_m"], planning._min_useful_fragment_m(10))

    def test_child_coverage_is_survey_frame_aligned(self):
        grid = _grid(self.inp)
        # A child's SURVEY LINES are the sonar passes; the hops between them are transit and,
        # like a single-vehicle mission's, may take any safe heading. The line endpoints come
        # from the fleet plan itself, since a child package carries no route_quality.
        pairs = set()
        for ln in self.plan["survey_lines"]:
            cs = ln["coordinates"]
            a = (round(cs[0][0], 7), round(cs[0][1], 7))
            b = (round(cs[-1][0], 7), round(cs[-1][1], 7))
            pairs.add((a, b))
            pairs.add((b, a))       # a line is flown in whichever direction the sweep needs
        for vp in self.plan["vehicles"]:
            pkg = vp["mission_package"]
            legs = _fragment_legs(pkg, grid, pairs=pairs)
            self.assertTrue(legs, f"{vp['vehicle_id']} covered no survey line")
            for kind, a, b in legs:
                self.assertIn(_classify(a, b, 42), ("U", "V", "short"),
                              f"{vp['vehicle_id']} {kind} leg at {_bearing(a, b):.1f}° "
                              f"is not survey-frame aligned")

    def test_child_missions_still_validate(self):
        for vp in self.plan["vehicles"]:
            self.assertTrue(vp["mission_package"]["geometry_check"]["ok"],
                            f"{vp['vehicle_id']} child mission failed its geometry contract")


if __name__ == "__main__":
    unittest.main()
