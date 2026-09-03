"""
Graph-based detour planner (Local Agent side, dry-run only).

Given an *approved* survey graph (the two lawnmower passes exposed as nodes +
undirected edges by Mission Control's mission_graph.py) and a long-range
obstacle blocking the current forward mission edge, this module produces a
deterministic detour *proposal* through existing approved graph nodes/edges.

One strategy only, exactly as scoped:

    1. block the current forward edge;
    2. select a rejoin node beyond the blocked portion;
    3. run deterministic shortest-path search over the remaining approved
       graph (edge cost = geometric edge length only -- no energy, comms,
       turn penalties, or coverage optimisation);
    4. return the ordered graph nodes / mission-waypoint references.

This module NEVER LOITERs the vehicle, uploads a revised mission, or resumes
AUTO. It returns a proposal object and stops. The Local Agent (or the
experiment) decides what, if anything, to do with it.

Graph contract (produced by flask/mission_graph.build_survey_graph, or built
directly in tests):

    {
      "nodes": {"<id>": {"id","x","y","kind","pass",...}, ...},
      "edges": [{"id","u","v","pass","length_m"}, ...],   # undirected
      "passes": {"H": [id,...], "V": [id,...]},            # optional
    }

Nodes must carry an "id"; "length_m" on an edge is used as cost when present,
otherwise the planar distance between the two node (x, y) coordinates is used.
"""
import heapq
import math
from typing import Any, Dict, List, Optional, Tuple

import config
import obstacle_model


def _edge_length(graph: Dict[str, Any], edge: Dict[str, Any]) -> float:
    if edge.get("length_m") is not None:
        return float(edge["length_m"])
    u, v = graph["nodes"][edge["u"]], graph["nodes"][edge["v"]]
    return math.hypot(u.get("x", 0.0) - v.get("x", 0.0),
                      u.get("y", 0.0) - v.get("y", 0.0))


def build_adjacency(graph: Dict[str, Any],
                    blocked: Optional[set] = None) -> Dict[str, List[Tuple[str, float]]]:
    """
    Undirected adjacency {node_id: [(neighbour_id, cost), ...]} with `blocked`
    (a set of frozenset({u, v}) pairs) removed. Neighbours are sorted by id so
    the search order -- and therefore the result -- is fully deterministic.
    """
    blocked = blocked or set()
    adj: Dict[str, List[Tuple[str, float]]] = {nid: [] for nid in graph["nodes"]}
    for edge in graph["edges"]:
        u, v = edge["u"], edge["v"]
        if frozenset((u, v)) in blocked:
            continue
        cost = _edge_length(graph, edge)
        adj.setdefault(u, []).append((v, cost))
        adj.setdefault(v, []).append((u, cost))
    for nid in adj:
        adj[nid].sort(key=lambda t: t[0])
    return adj


def shortest_path(graph: Dict[str, Any], start: str, goal: str,
                  blocked: Optional[set] = None) -> Tuple[Optional[List[str]], Optional[float]]:
    """
    Deterministic Dijkstra over the undirected approved graph. Ties are broken
    by node id (the heap entries carry the id), so repeated runs on the same
    graph return the identical path and cost. Returns (path, total_cost) or
    (None, None) if no route exists.
    """
    if start not in graph["nodes"] or goal not in graph["nodes"]:
        return None, None
    adj = build_adjacency(graph, blocked)
    dist = {start: 0.0}
    prev: Dict[str, str] = {}
    # heap entries: (accumulated_cost, node_id) -- id in the tuple makes tie
    # ordering deterministic.
    heap = [(0.0, start)]
    visited = set()
    while heap:
        d, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        if node == goal:
            break
        for nbr, cost in adj.get(node, []):
            if nbr in visited:
                continue
            nd = d + cost
            if nd < dist.get(nbr, math.inf) - 1e-12:
                dist[nbr] = nd
                prev[nbr] = node
                heapq.heappush(heap, (nd, nbr))
    if goal not in dist:
        return None, None
    # Reconstruct
    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    path.reverse()
    return path, round(dist[goal], 3)


def _waypoint_refs(graph: Dict[str, Any], node_ids: List[str]) -> List[Dict[str, Any]]:
    """Compact mission-waypoint references for the nodes on a path."""
    refs = []
    for nid in node_ids:
        n = graph["nodes"].get(nid, {})
        refs.append({
            "node_id": nid,
            "kind": n.get("kind"),
            "pass": n.get("pass"),
            "order": n.get("order"),
            "lng": n.get("lng"),
            "lat": n.get("lat"),
        })
    return refs


def _affected_original_sequences(graph: Dict[str, Any],
                                 current_edge: Tuple[str, str]) -> Dict[str, Any]:
    """
    The original approved waypoint sequences the detour touches: the traversal
    order (from graph["passes"]) of whichever pass the blocked edge belongs to,
    plus the ordered indices of the blocked edge's endpoints within it. This is
    what the operator needs to see which planned survey run is being reordered.
    """
    u, v = current_edge
    passes = graph.get("passes", {})
    affected = {}
    for label, ids in passes.items():
        if u in ids and v in ids:
            affected[label] = {
                "pass": label,
                "node_sequence": ids,
                "blocked_from_index": ids.index(u),
                "blocked_to_index": ids.index(v),
            }
    return affected


def _select_rejoin_node(graph: Dict[str, Any],
                        current_edge: Tuple[str, str]) -> str:
    """
    The rejoin node beyond the blocked portion. For the single scoped
    strategy this is the far endpoint of the blocked forward edge -- the first
    approved node past the obstacle we must get back onto the survey network
    at. Kept as its own function so a future strategy can pick a node further
    downstream without touching the search.
    """
    return current_edge[1]


def propose_detour(graph: Dict[str, Any],
                   current_edge: Tuple[str, str],
                   obstacle: obstacle_model.ObstacleEvent,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """
    Build the dry-run detour proposal for a single obstacle event on the
    current forward edge. Returns a proposal/status object; performs no vehicle
    action. See module docstring for the strategy.
    """
    classification = obstacle.classify(now)
    action = obstacle.recommended_action(now)

    proposal: Dict[str, Any] = {
        "schema_version": "1.0",
        "dry_run": True,
        "obstacle_event": obstacle.to_dict(),
        "obstacle_classification": classification,
        "recommended_action": action,
        "current_edge": list(current_edge),
        "blocked_edge": None,
        "selected_rejoin_node": None,
        "detour_node_sequence": [],
        "detour_waypoint_refs": [],
        "detour_cost_m": None,
        "affected_original_sequences": {},
        "route_found": None,
        "validation_outcome": None,
        "reason": None,
        # Explicit reminders that this proposal changes nothing on the vehicle.
        "loiter_commanded": False,
        "mission_uploaded": False,
        "auto_resumed": False,
    }

    # Close obstacle: immediate LOITER only. Do NOT replan while still moving,
    # do NOT reverse, do NOT search the graph.
    if action == obstacle_model.ACTION_LOITER:
        proposal["validation_outcome"] = "LOITER_ONLY"
        proposal["reason"] = (
            f"Obstacle at {obstacle.distance_m} m is within the "
            f"{config.OBSTACLE_CLOSE_DISTANCE_M} m close range; "
            "immediate LOITER only -- no reverse, no replan while moving."
        )
        return proposal

    # Clear / expired: nothing to do.
    if action == obstacle_model.ACTION_NONE:
        proposal["validation_outcome"] = "NO_ACTION"
        proposal["reason"] = (
            "Obstacle expired (stale detection)." if classification == obstacle_model.EXPIRED
            else "No obstacle present; continuing on the approved mission."
        )
        return proposal

    # Long-range: block the forward edge and search the approved graph.
    u, v = current_edge
    if u not in graph["nodes"] or v not in graph["nodes"]:
        proposal["route_found"] = False
        proposal["validation_outcome"] = "INVALID_EDGE"
        proposal["reason"] = (
            f"Current edge ({u} -> {v}) references a node not in the approved graph."
        )
        return proposal

    blocked_pair = frozenset((u, v))
    proposal["blocked_edge"] = list(current_edge)

    rejoin = _select_rejoin_node(graph, current_edge)
    proposal["selected_rejoin_node"] = rejoin

    path, cost = shortest_path(graph, u, rejoin, blocked={blocked_pair})
    proposal["affected_original_sequences"] = _affected_original_sequences(graph, current_edge)

    if path is None:
        proposal["route_found"] = False
        proposal["validation_outcome"] = "NO_ROUTE"
        proposal["reason"] = (
            f"No detour exists through the approved graph from {u} to rejoin "
            f"node {rejoin} once edge {u}->{v} is blocked."
        )
        return proposal

    proposal["route_found"] = True
    proposal["detour_node_sequence"] = path
    proposal["detour_waypoint_refs"] = _waypoint_refs(graph, path)
    proposal["detour_cost_m"] = cost
    # The path must not traverse the blocked edge (guaranteed by construction)
    # and must actually rejoin.
    uses_blocked = any(
        frozenset((path[i], path[i + 1])) == blocked_pair
        for i in range(len(path) - 1)
    )
    proposal["validation_outcome"] = "OK" if (not uses_blocked and path[-1] == rejoin) else "INVALID"
    proposal["reason"] = (
        f"Deterministic detour of {len(path)} approved nodes "
        f"({cost} m) rejoining at {rejoin}."
    )
    return proposal


# ---------------------------------------------------------------------------
# Manual dry-run entry point:  python3 detour_planner.py --demo
#
# Builds a small 3x3 Manhattan-style approved survey graph (a horizontal and a
# vertical pass crossing at 9 interior nodes), injects a ~10 m long-range
# obstacle on the current forward edge, and prints the proposal. Nothing is
# sent to any vehicle.
# ---------------------------------------------------------------------------
def _demo_grid_graph() -> Dict[str, Any]:
    """3x3 lattice: nodes G{r}{c}, unit-metre spacing, H and V pass edges."""
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
                add(f"G{r}{c}", f"G{r}{c+1}")   # horizontal edges
            if r < 2:
                add(f"G{r}{c}", f"G{r+1}{c}")   # vertical edges
    passes = {
        "H": [f"G0{c}" for c in range(3)],
        "V": [f"G{r}0" for r in range(3)],
    }
    return {"nodes": nodes, "edges": edges, "passes": passes,
            "crs": "demo-unit-metres"}


def _demo() -> Dict[str, Any]:
    import json
    graph = _demo_grid_graph()
    # Current forward edge G00 -> G01 (first horizontal survey leg) is blocked.
    obstacle = obstacle_model.ObstacleEvent(
        event_type=obstacle_model.OBSTACLE_AHEAD, distance_m=10,
        source="EXPERIMENT_INJECTION", confidence=1.0, expires_after_s=30)
    proposal = propose_detour(graph, ("G00", "G01"), obstacle)
    print(json.dumps(proposal, indent=2))
    return proposal


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv or len(sys.argv) == 1:
        _demo()
