"""
Focused tests for detour_planner.py -- the single graph-detour strategy.

No pytest dependency:

    python3 test_detour_planner.py
"""
import unittest

import detour_planner as dp
import obstacle_model as om


def _grid_graph():
    """
    3x3 unit-metre Manhattan lattice (horizontal + vertical approved passes
    crossing at every node). Same shape detour_planner._demo_grid_graph()
    builds, kept local so the test does not depend on the demo helper.

        G00 - G01 - G02
         |     |     |
        G10 - G11 - G12
         |     |     |
        G20 - G21 - G22
    """
    import math
    nodes = {}
    for r in range(3):
        for c in range(3):
            nid = f"G{r}{c}"
            nodes[nid] = {"id": nid, "x": float(c), "y": float(r),
                          "lng": c * 1e-4, "lat": r * 1e-4,
                          "kind": "intersection", "pass": None, "order": None}
    edges = []

    def add(a, b):
        edges.append({"id": f"E{len(edges)}", "u": a, "v": b, "pass": None,
                      "length_m": math.hypot(nodes[a]["x"] - nodes[b]["x"],
                                             nodes[a]["y"] - nodes[b]["y"])})
    for r in range(3):
        for c in range(3):
            if c < 2:
                add(f"G{r}{c}", f"G{r}{c+1}")
            if r < 2:
                add(f"G{r}{c}", f"G{r+1}{c}")
    passes = {"H": [f"G0{c}" for c in range(3)], "V": [f"G{r}0" for r in range(3)]}
    return {"nodes": nodes, "edges": edges, "passes": passes}


def _long_range():
    return om.ObstacleEvent(event_type=om.OBSTACLE_AHEAD, distance_m=10,
                            confidence=1.0, expires_after_s=30,
                            detected_at=1000.0)


class TestForwardEdgeBlocked(unittest.TestCase):
    def test_blocked_edge_recorded_and_excluded(self):
        g = _grid_graph()
        p = dp.propose_detour(g, ("G00", "G01"), _long_range(), now=1001.0)
        self.assertEqual(p["blocked_edge"], ["G00", "G01"])
        # The proposed detour must not traverse the blocked edge.
        seq = p["detour_node_sequence"]
        pairs = {frozenset((seq[i], seq[i + 1])) for i in range(len(seq) - 1)}
        self.assertNotIn(frozenset(("G00", "G01")), pairs)

    def test_shortest_path_helper_excludes_blocked(self):
        g = _grid_graph()
        blocked = {frozenset(("G00", "G01"))}
        path, cost = dp.shortest_path(g, "G00", "G01", blocked=blocked)
        self.assertIsNotNone(path)
        self.assertEqual(path[0], "G00")
        self.assertEqual(path[-1], "G01")
        self.assertGreater(cost, 1.0)  # direct 1 m edge is gone


class TestValidManhattanDetour(unittest.TestCase):
    def test_detour_rejoins_and_is_valid(self):
        g = _grid_graph()
        p = dp.propose_detour(g, ("G00", "G01"), _long_range(), now=1001.0)
        self.assertEqual(p["obstacle_classification"], om.LONG_RANGE)
        self.assertTrue(p["route_found"])
        self.assertEqual(p["validation_outcome"], "OK")
        self.assertEqual(p["selected_rejoin_node"], "G01")
        self.assertEqual(p["detour_node_sequence"][0], "G00")
        self.assertEqual(p["detour_node_sequence"][-1], "G01")
        # Manhattan detour around one blocked unit edge costs 3 m.
        self.assertAlmostEqual(p["detour_cost_m"], 3.0, places=3)
        self.assertFalse(p["mission_uploaded"])
        self.assertFalse(p["loiter_commanded"])

    def test_affected_original_sequence_reported(self):
        g = _grid_graph()
        p = dp.propose_detour(g, ("G00", "G01"), _long_range(), now=1001.0)
        self.assertIn("H", p["affected_original_sequences"])
        aff = p["affected_original_sequences"]["H"]
        self.assertEqual(aff["blocked_from_index"], 0)
        self.assertEqual(aff["blocked_to_index"], 1)


class TestCloseObstacleLoiterOnly(unittest.TestCase):
    def test_close_obstacle_is_loiter_only_no_search(self):
        g = _grid_graph()
        close = om.ObstacleEvent(distance_m=3, confidence=1.0,
                                 expires_after_s=30, detected_at=1000.0)
        p = dp.propose_detour(g, ("G00", "G01"), close, now=1001.0)
        self.assertEqual(p["recommended_action"], om.ACTION_LOITER)
        self.assertEqual(p["validation_outcome"], "LOITER_ONLY")
        # No graph search performed for a close obstacle.
        self.assertEqual(p["detour_node_sequence"], [])
        self.assertIsNone(p["blocked_edge"])
        self.assertFalse(p["loiter_commanded"])  # dry-run: proposal only


class TestNoRouteAvailable(unittest.TestCase):
    def test_no_route_when_rejoin_isolated(self):
        # A minimal graph with a single edge: blocking it isolates the rejoin
        # node -- there is no alternative path.
        g = {
            "nodes": {
                "A": {"id": "A", "x": 0.0, "y": 0.0, "kind": "waypoint", "pass": "H", "order": 0},
                "B": {"id": "B", "x": 1.0, "y": 0.0, "kind": "waypoint", "pass": "H", "order": 1},
            },
            "edges": [{"id": "E0", "u": "A", "v": "B", "pass": "H", "length_m": 1.0}],
            "passes": {"H": ["A", "B"], "V": []},
        }
        p = dp.propose_detour(g, ("A", "B"), _long_range(), now=1001.0)
        self.assertFalse(p["route_found"])
        self.assertEqual(p["validation_outcome"], "NO_ROUTE")
        self.assertIsNotNone(p["reason"])
        self.assertEqual(p["detour_node_sequence"], [])


class TestDeterminism(unittest.TestCase):
    def test_repeated_result_identical(self):
        g = _grid_graph()
        obstacle = _long_range()
        results = [dp.propose_detour(g, ("G00", "G01"), obstacle, now=1001.0)
                   for _ in range(5)]
        first = results[0]["detour_node_sequence"]
        for r in results[1:]:
            self.assertEqual(r["detour_node_sequence"], first)
            self.assertEqual(r["detour_cost_m"], results[0]["detour_cost_m"])

    def test_shortest_path_deterministic_on_symmetric_graph(self):
        g = _grid_graph()
        blocked = {frozenset(("G11", "G12"))}
        paths = [dp.shortest_path(g, "G00", "G22", blocked=blocked)[0]
                 for _ in range(5)]
        for pth in paths[1:]:
            self.assertEqual(pth, paths[0])


if __name__ == "__main__":
    unittest.main()
