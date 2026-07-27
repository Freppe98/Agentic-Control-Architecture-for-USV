"""Route-quality tests for operator-side survey planning (planning.py) — the deterministic,
safety-checked route CLEANUP layer added on top of the ported generator.

Run from operator-scripts/:  python -m unittest tests.test_planning_quality

What is pinned here, mapped to the route-quality task:
  * safety-checked line-of-sight connector compression (PART 4) collapses a raw grid
    staircase to its turn points AND never shortcuts through a concave boundary or a no-go
    interior (TestLineOfSight);
  * the shared semantic cleanup (PART 5/6) removes duplicates, near-duplicates and provably
    collinear middle points, keeps first/last and operator approach/return waypoints, and
    rejects any unsafe shortcut (TestCleanup);
  * three asymmetric/concave regression fixtures inspired by the manual screenshots — a
    concave notch, a multi-lobed (wide-notch) boundary and a central no-go obstacle — each
    generate a SAFE, VALID, waypoint-efficient, deterministic route whose connector count is
    reduced and whose coverage stays monotonic by sweep row (TestRouteQualityRegression).

Every geometry test is skipped with a clear reason when shapely/pyproj/numpy are absent,
exactly as the endpoints degrade at runtime.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import planning  # noqa: E402

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

# ── Fixtures inspired by the manual observations (task PART 9) ────────────────────────────
# 1. A large concave boundary with a narrow inward notch bitten out of the top edge.
CONCAVE_NOTCH = [[13.000, 56.699], [13.006, 56.699], [13.006, 56.7010],
                 [13.0032, 56.7010], [13.0032, 56.6996], [13.0028, 56.6996],
                 [13.0028, 56.7010], [13.000, 56.7010]]
# 2. A multi-lobed asymmetric boundary (two tall arms round a wide bay) whose horizontal scan
#    lines split into several fragments that can be visited in an awkward order.
MULTI_LOBE = [[13.000, 56.699], [13.006, 56.699], [13.006, 56.7010],
              [13.0038, 56.7010], [13.0038, 56.7000], [13.0022, 56.7000],
              [13.0022, 56.7010], [13.000, 56.7010]]
# 3. A rectangular survey with a central no-go zone that forces A* transition detours.
CENTRAL = [[13.000, 56.699], [13.006, 56.699], [13.006, 56.7010], [13.000, 56.7010]]
CENTRAL_ZONE = [[13.0025, 56.6998], [13.0035, 56.6998], [13.0035, 56.7006], [13.0025, 56.7006]]

# A no-go zone used by the unit tests.
ZONE = [[13.0028, 56.7000], [13.0034, 56.7000], [13.0034, 56.7006], [13.0028, 56.7006]]


def _fixture(name):
    if name == "concave notch":
        return {"boundary": CONCAVE_NOTCH, "shoreline_clearance_m": 3, "lane_spacing_m": 12,
                "primary_angle_deg": 0, "home": [12.9995, 56.6985]}
    if name == "multi-lobe":
        return {"boundary": MULTI_LOBE, "shoreline_clearance_m": 2, "lane_spacing_m": 12,
                "primary_angle_deg": 0, "home": [12.9995, 56.6985]}
    if name == "central obstacle":
        return {"boundary": CENTRAL, "shoreline_clearance_m": 3, "lane_spacing_m": 12,
                "primary_angle_deg": 0, "home": [12.9995, 56.6985], "no_go_zones": [CENTRAL_ZONE]}
    raise KeyError(name)


@requires_geometry
class TestLineOfSight(unittest.TestCase):
    """Safety-checked line-of-sight compression (task PART 4 / PART 9 cases 5–9, 11)."""

    def _grid(self, boundary=CENTRAL, clearance=3, zones=None, step=12):
        return planning._NavGrid(boundary, clearance, zones or [], step_m=step)

    def test_direct_line_of_sight_compression_collapses_staircase(self):
        # A safe interior corridor described as a dense grid staircase collapses to its ends.
        g = self._grid()
        y = 56.70025
        staircase = [[13.0010 + 0.00005 * i, y] for i in range(20)]  # collinear, all safe
        out = g._compress_los(staircase, require_inside=True)
        self.assertEqual(len(out), 2, "a fully-safe straight run must compress to 2 points")
        self.assertEqual(out[0], staircase[0])
        self.assertEqual(out[-1], staircase[-1])

    def test_every_compressed_hop_is_safe(self):
        g = self._grid(boundary=CONCAVE_NOTCH, clearance=3, step=8)
        path = g.safe_connector([13.0020, 56.7006], [13.0040, 56.7006], require_inside=True)
        for a, b in zip(path, path[1:]):
            self.assertTrue(g.segment_is_safe(a, b, require_inside=True),
                            "compression produced an unsafe hop")

    def test_compression_blocked_by_concave_boundary(self):
        # Across the mouth of the notch the direct end-to-end shortcut is UNSAFE, so the
        # compressed connector must keep at least one interior turn point (never collapse to 2).
        g = self._grid(boundary=CONCAVE_NOTCH, clearance=3, step=8)
        a, b = [13.0020, 56.7006], [13.0040, 56.7006]
        self.assertFalse(g.segment_is_safe(a, b, require_inside=True))
        path = g.safe_connector(a, b, require_inside=True)
        self.assertGreater(len(path), 2, "a shortcut through the notch must not be taken")

    def test_compression_blocked_by_no_go_zone(self):
        from shapely.geometry import LineString
        from shapely.ops import transform
        g = self._grid(zones=[ZONE], step=8)
        a, b = [13.0026, 56.7003], [13.0036, 56.7003]   # straight line crosses the zone
        self.assertFalse(g.segment_is_safe(a, b, require_inside=True))
        path = g.safe_connector(a, b, require_inside=True)
        lp = transform(g.to_proj.transform, LineString([(p[0], p[1]) for p in path]))
        self.assertTrue(g._seg_clears_nogo(lp), "compressed connector crossed the no-go interior")
        self.assertGreater(len(path), 2)

    def test_compression_is_deterministic(self):
        g1 = self._grid(boundary=CONCAVE_NOTCH, clearance=3, step=8)
        g2 = self._grid(boundary=CONCAVE_NOTCH, clearance=3, step=8)
        a, b = [13.0020, 56.7006], [13.0040, 56.7006]
        self.assertEqual(g1.safe_connector(a, b, require_inside=True),
                         g2.safe_connector(a, b, require_inside=True))


@requires_geometry
class TestCleanup(unittest.TestCase):
    """Shared deterministic path cleanup (task PART 5 / PART 6 / PART 9 cases 5–10)."""

    def _grid(self, boundary=CENTRAL, clearance=3, zones=None, step=12):
        return planning._NavGrid(boundary, clearance, zones or [], step_m=step)

    def test_exact_and_near_duplicates_removed(self):
        g = self._grid()
        y = 56.70025
        pts = [[13.0010, y], [13.0010, y], [13.0010 + 1e-8, y], [13.0020, y]]
        out = g.clean_path(pts, require_inside=True)
        self.assertEqual(len(out), 2, "duplicate + near-duplicate points must collapse")

    def test_collinear_middle_point_removed(self):
        g = self._grid()
        y = 56.70025
        pts = [[13.0010, y], [13.0015, y], [13.0020, y]]  # exactly collinear
        out = g.clean_path(pts, require_inside=True)
        self.assertEqual(len(out), 2, "a collinear middle point must be removed")

    def test_genuine_corner_is_preserved(self):
        g = self._grid()
        pts = [[13.0010, 56.7002], [13.0020, 56.7002], [13.0020, 56.7008]]  # 90° corner
        out = g.clean_path(pts, require_inside=True)
        self.assertEqual(len(out), 3, "a real corner must not be removed as collinear")

    def test_tiny_zigzag_is_removed(self):
        # A tiny out-and-back off a straight safe run is absorbed by aggressive LOS cleanup.
        g = self._grid()
        y = 56.70025
        pts = [[13.0010, y], [13.0014, y + 1e-6], [13.0018, y], [13.0022, y]]
        out = g.clean_path(pts, require_inside=True, aggressive=True)
        self.assertEqual(len(out), 2, "a tiny near-collinear zigzag on a safe run must collapse")

    def test_unsafe_shortcut_is_rejected(self):
        # Aggressive cleanup of a detour around the notch must keep it safe (no shortcut through).
        g = self._grid(boundary=CONCAVE_NOTCH, clearance=3, step=8)
        detour = g.safe_connector([13.0020, 56.7006], [13.0040, 56.7006], require_inside=True)
        out = g.clean_path(detour, require_inside=True, aggressive=True)
        for a, b in zip(out, out[1:]):
            self.assertTrue(g.segment_is_safe(a, b, require_inside=True))
        self.assertGreater(len(out), 2)

    def test_first_and_last_points_preserved(self):
        g = self._grid()
        y = 56.70025
        pts = [[13.0010, y], [13.0015, y], [13.0020, y]]
        out = g.clean_path(pts, require_inside=True)
        self.assertEqual(out[0], pts[0])
        self.assertEqual(out[-1], pts[-1])

    def test_anchor_points_preserved(self):
        # An interior anchor (an operator waypoint) is never removed, even if collinear.
        g = self._grid()
        y = 56.70025
        anchor = [13.0015, y]
        pts = [[13.0010, y], anchor, [13.0020, y]]
        out = g.clean_path(pts, require_inside=False, anchors=[anchor], aggressive=True)
        self.assertIn(anchor, out, "an operator waypoint must be preserved as an anchor")


@requires_geometry
class TestRouteQualityRegression(unittest.TestCase):
    """The three asymmetric/concave regression fixtures (task PART 9)."""

    FIXTURES = ("concave notch", "multi-lobe", "central obstacle")

    def _gen(self, name):
        return planning.generate_survey(_fixture(name), max_route_waypoints=2000)

    # ── Safety ───────────────────────────────────────────────────────────────────────────
    def test_all_fixtures_generate_and_validate(self):
        from shapely.geometry import LineString
        from shapely.ops import transform
        for name in self.FIXTURES:
            inp = _fixture(name)
            r = self._gen(name)
            grid = planning._NavGrid(inp["boundary"], inp["shoreline_clearance_m"],
                                     inp.get("no_go_zones") or [], step_m=inp["lane_spacing_m"])
            for s in r["segments"]:
                lp = transform(grid.to_proj.transform,
                               LineString([(p[0], p[1]) for p in s["coordinates"]]))
                if s["kind"] in planning._INSIDE_KINDS:
                    out = lp.difference(grid.navigable.buffer(planning.COVER_TOL_M))
                    self.assertTrue(out.is_empty or out.length < 1.0,
                                    f"[{name}] {s['kind']} leaves navigable by {out.length:.1f} m")
                self.assertTrue(grid._seg_clears_nogo(lp),
                                f"[{name}] {s['kind']} crosses a no-go interior")
            v = planning.validate_plan({**inp, "segments": r["segments"],
                                        "route_waypoints": r["route_waypoints"],
                                        "route_hash": r["route_hash"],
                                        "input_revision": r["input_revision"]},
                                       max_route_waypoints=2000)
            self.assertTrue(v["ok"], f"[{name}] validation failed: {v['errors']}")

    # ── Quality ──────────────────────────────────────────────────────────────────────────
    def test_no_duplicate_or_near_duplicate_points(self):
        for name in self.FIXTURES:
            inp = _fixture(name)
            r = self._gen(name)
            grid = planning._NavGrid(inp["boundary"], inp["shoreline_clearance_m"],
                                     inp.get("no_go_zones") or [], step_m=inp["lane_spacing_m"])
            for s in r["segments"]:
                pc = s["coordinates"]
                for a, b in zip(pc, pc[1:]):
                    ax, ay = grid.to_proj.transform(a[0], a[1])
                    bx, by = grid.to_proj.transform(b[0], b[1])
                    import math
                    self.assertGreater(math.hypot(bx - ax, by - ay), 0.1,
                                       f"[{name}] {s['kind']} has a duplicate/near-duplicate leg")

    def test_no_removable_collinear_in_connector_segments(self):
        import math
        conn_kinds = ("start_connector", "survey_entry_connector", "pass_transition",
                      "return_connector", "final_home_connector")
        for name in self.FIXTURES:
            inp = _fixture(name)
            r = self._gen(name)
            grid = planning._NavGrid(inp["boundary"], inp["shoreline_clearance_m"],
                                     inp.get("no_go_zones") or [], step_m=inp["lane_spacing_m"])
            for s in r["segments"]:
                if s["kind"] not in conn_kinds:
                    continue
                pc = s["coordinates"]
                pp = [grid.to_proj.transform(c[0], c[1]) for c in pc]
                ri = s["kind"] in planning._REQUIRE_INSIDE_KINDS
                for i in range(1, len(pc) - 1):
                    ang = planning._turn_angle_deg(pp[i - 1], pp[i], pp[i + 1])
                    removable = (ang <= planning.CLEANUP_COLLINEAR_DEG
                                 and grid.segment_is_safe(pc[i - 1], pc[i + 1], require_inside=ri))
                    self.assertFalse(removable,
                                     f"[{name}] {s['kind']} keeps a removable collinear point")

    def test_no_immediate_backtracking(self):
        for name in self.FIXTURES:
            self.assertLessEqual(self._gen(name)["route_quality"]["backtracking_events"], 0,
                                 f"[{name}] route contains an immediate backtrack")

    def test_connector_simplification_reduces_or_preserves(self):
        for name in self.FIXTURES:
            rq = self._gen(name)["route_quality"]
            self.assertLessEqual(rq["final_connector_waypoint_count"],
                                 rq["raw_connector_waypoint_count"],
                                 f"[{name}] connector simplification increased the count")

    def test_central_obstacle_connector_strictly_reduced(self):
        rq = self._gen("central obstacle")["route_quality"]
        self.assertGreater(rq["raw_connector_waypoint_count"], 2)
        self.assertLess(rq["final_connector_waypoint_count"], rq["raw_connector_waypoint_count"],
                        "the central-obstacle A* path must be simplified to fewer connector points")

    def test_raw_and_final_counts_reported(self):
        for name in self.FIXTURES:
            rq = self._gen(name)["route_quality"]
            self.assertGreater(rq["raw_waypoint_count"], 0)
            self.assertGreater(rq["final_waypoint_count"], 0)
            self.assertGreaterEqual(rq["raw_waypoint_count"], rq["final_waypoint_count"])
            self.assertEqual(rq["removed_waypoint_count"],
                             rq["raw_waypoint_count"] - rq["final_waypoint_count"])
            self.assertTrue(rq["cleanup_applied"])

    def test_coverage_ordering_is_monotonic_by_sweep_row(self):
        # No excessive cross-row jumping: fragment execution order matches sweep-row order, so
        # there are no reorders (an explicit safe detour would be the only allowed exception).
        for name in self.FIXTURES:
            rq = self._gen(name)["route_quality"]
            self.assertEqual(rq["fragment_reorders"], 0,
                             f"[{name}] coverage fragments visited out of sweep-row order")
            frs = rq["coverage_fragments"]
            self.assertEqual([f["fragment_index"] for f in frs],
                             sorted(range(len(frs)), key=lambda i: frs[i]["row_index"]),
                             f"[{name}] execution order disagrees with sweep-row order")

    def test_generation_is_byte_equivalent_and_hash_stable(self):
        for name in self.FIXTURES:
            a = self._gen(name)
            b = self._gen(name)
            self.assertEqual(a["route_waypoints"], b["route_waypoints"],
                             f"[{name}] identical inputs produced a different route")
            self.assertEqual(a["route_hash"], b["route_hash"],
                             f"[{name}] identical inputs produced a different hash")
            self.assertEqual(a["route_quality"], b["route_quality"],
                             f"[{name}] identical inputs produced different quality metrics")

    def test_hash_computed_from_final_simplified_route(self):
        import mission_contract
        for name in self.FIXTURES:
            r = self._gen(name)
            self.assertEqual(r["route_hash"],
                             mission_contract.route_content_hash(r["route_waypoints"]),
                             f"[{name}] route_hash is not the hash of the final route")

    def test_generation_algorithm_provenance_present(self):
        r = self._gen("central obstacle")
        alg = r["generation_algorithm"]
        self.assertEqual(alg["connector_simplification"], "safe-line-of-sight-v1")
        self.assertEqual(alg["cleanup"], "semantic-path-cleanup-v1")


if __name__ == "__main__":
    unittest.main()
