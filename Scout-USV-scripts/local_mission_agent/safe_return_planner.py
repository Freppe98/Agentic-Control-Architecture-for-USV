"""
Safe-return route construction + validation for the first replanning strategy,
SAFE_RETURN_HOME.

Strategy (shortest safe route first, approved retrace as a proven fallback)
-----------------------------------------------------------------------------
The E2 objective is LOW ENERGY -> terminate survey -> reach Home using the
MINIMUM safe travel distance, not "replay as much of the original mission as
possible". build_safe_return_route therefore tries, in order, and returns
whichever first produces a route that is BOTH constructible AND independently
proven valid by validate_route (the exact same authoritative geometry checks
used for the route that is eventually uploaded):

  A. DIRECT: current position -> Home, if that single segment lies wholly
     within navigable_boundary UNION home_corridor and crosses no no-go zone
     (buffered by no_go_clearance_m). The minimum possible route.

  B. SHORTEST CONSTRAINED PATH: when direct travel is not provably safe, a
     small deterministic visibility graph (see _shortest_route_candidate)
     whose nodes are the current position, Home, and the vertices of the
     approved navigable_boundary / home_corridor / no_go_zones (no-go
     vertices offset outward by no_go_clearance_m), and whose edges are added
     ONLY when the segment is proven valid by the SAME geometry rules as (A).
     detour_planner.shortest_path (a plain, deterministic Dijkstra already in
     this codebase, previously groundwork-only) runs over that graph; the
     result is then simplified (route_simplify) by greedily removing any
     interior point whose neighbours can be joined directly by an
     equally-proven segment.

  C. RETRACE_APPROVED_FALLBACK: if neither (A) nor (B) can produce a route
     that validate_route independently accepts, fall back to the ORIGINAL,
     unmodified strategy this module has always used --

         [ current position ] + reverse( approved waypoints traversed ) + [ Home ]

     -- which stays available exactly as before (see
     _build_retrace_approved_route) precisely because it is already proven
     safe by construction (every interior point is an already-approved,
     already-navigated waypoint). This keeps the feature fail-safe: a bug or
     geometry gap in the new shortest-path search degrades to the original,
     already-shipped-and-water-tested behaviour, never to an unproven route.

Whichever of (A)/(B)/(C) wins is reported in the returned dict's "method"
(SHORTEST_SAFE_RETURN or RETRACE_APPROVED_FALLBACK) together with
direct_path_valid / candidate_node_count / fallback_used / planner_runtime_s
evidence, so a 26-point retrace can never be silently mislabeled "shortest".

Fail closed, never a direct UNPROVEN line
---------------------------------------
If the planning package is unusable (no Home, no route), if the connector from
the current position to the approved network exceeds connect_gap_max_m, if
NEITHER the shortest-path search NOR the retrace fallback can build a route
that terminates at Home without an invalid/duplicate point or a no-go
crossing, the planner returns ok/valid False with a reason. It never silently
substitutes an unproven direct line for a failed search -- route (A) above is
only ever used when it independently passes the same validate_route check
everything else does.

Both functions are pure (no I/O), deterministic, and unit-testable with plain
dicts.

Home / launch connector contract (task section 4 -- FINALIZED)
--------------------------------------------------------------
Start syncs Pixhawk Home to the vehicle's actual launch position, which may lie
OUTSIDE the survey navigable geometry. A safe-return route then terminates at
that runtime Home, so its final leg leaves the survey polygon. The contract:

  * Geometry type: `home_corridor` is a SINGLE polygon ring (not a multi-ring
    collection, not a line). Wire order is [longitude, latitude] pairs -- the
    SAME convention as `navigable_geometry`/`no_go_zones` -- canonicalized to
    internal [lat, lon] on acceptance (planning_package.validate_package_v1).
  * Minimum vertices / closure: >= 3 distinct vertices; the ring is IMPLICITLY
    closed (do not repeat the first vertex; a degenerate < 3-vertex ring is
    ignored, i.e. treated as "no corridor" -> fail closed).
  * Relationship to navigable_geometry: the corridor MUST OVERLAP the survey
    navigable boundary at the connector-entry region, so each transition segment
    (survey point <-> Home) lies WHOLLY within either the boundary or the
    corridor. Per-segment containment does not prove a segment that straddles the
    shared edge of two regions; the overlap is what makes the transition segment
    fully corridor-contained (see geo.route_within_regions).
  * Runtime Home: SHOULD lie inside the corridor. If the route's final leg to
    Home is not contained by boundary-or-corridor, validation fails closed with
    HOME_OUTSIDE_NAVIGABLE_BOUNDARY_NO_CORRIDOR.
  * Boundary points: containment uses inclusive point-in-polygon; a point on a
    region edge counts as inside. Overlap at the entry region is therefore how a
    transition segment stays proven across the boundary/corridor seam.
  * No-go: the corridor is NOT exempt. The whole route, connector + Home leg
    included, is checked against every known no-go zone -- buffered outward by
    the package's `no_go_clearance_m` (see below) -- a corridor leg entering
    that buffered exclusion fails closed (HOME_CORRIDOR_CROSSES_NO_GO). This is
    a no-go-buffer check only; the corridor polygon itself is never buffered or
    invalidated by it -- see the no_go_clearance_m paragraph.
  * Shoreline clearance: `shoreline_clearance_m` is a SCALAR only. Scout has no
    shoreline polygon to offset against, so it NEVER claims shoreline clearance
    "verified"; it is reported as an unmet limitation. A scalar never makes an
    out-of-boundary Home connector safe.

  * No-go clearance: `no_go_clearance_m` is a SCALAR outward buffer (metres,
    default 0.0 when absent -- see planning_package.no_go_clearance_m_of) Scout
    applies to `no_go_zones` LOCALLY, using projected/local-metric distance
    (geo.segment_distance_to_polygon_m), never a degree-distance approximation.
    Unlike shoreline clearance, this IS directly checkable: `no_go_zones` is
    already a polygon. `no_go_zones` itself is left untouched (the original,
    Operator-authoritative provenance); the buffer is applied only at the
    moment of the crossing check (geo.route_crosses_no_go's clearance_m), so no
    "buffered no-go zone" polygon is ever persisted or substituted for the
    original. This is checked per-segment, for EVERY route segment including
    the connector and the Home leg -- a route with individually-clear endpoints
    but a segment cutting through the buffer still fails
    (CODE_NO_GO_CROSSING / CODE_CONNECTOR_CROSSES_NO_GO). It does NOT buffer or
    invalidate navigable_boundary or home_corridor themselves -- those containment
    checks are unaffected; only the no-go exclusion grows.

  PROVENANCE RULE (safety): the Operator may only provide a `home_corridor`
  derived from operator-approved / planned navigable geometry. It must NOT invent
  a corridor to an arbitrary runtime launch Home where safety has not been
  established. If the runtime Home lies outside ALL approved navigable/corridor
  geometry, safe return is UNPROVABLE and Scout fails closed in LOITER (SAFE_HOLD)
  -- it never draws a direct line, relaxes the boundary, or treats Home as safe
  merely because the Pixhawk accepted it.
"""
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import detour_planner
import geo
import replan_config

STRATEGY = "SAFE_RETURN_HOME"
# Winning-strategy labels (task: "Expose which planner strategy actually won").
# SHORTEST_SAFE_RETURN covers BOTH preference (A) direct and (B) constrained
# visibility-graph paths -- both are proven-minimal-effort routes through
# approved geometry, as opposed to (C) below which is the pre-existing,
# always-safe retrace kept purely as a fail-safe.
METHOD_SHORTEST = "SHORTEST_SAFE_RETURN"
METHOD_RETRACE_FALLBACK = "RETRACE_APPROVED_FALLBACK"
# Backward-compatible alias (the value itself has changed to make clear this
# method is now ALWAYS reached only as a fallback -- see module docstring).
METHOD = METHOD_RETRACE_FALLBACK

# Reason codes.
CODE_OK = "OK"
CODE_NO_PACKAGE = "NO_PLANNING_PACKAGE"
CODE_NO_HOME = "NO_VERIFIED_HOME"
CODE_NO_POSITION = "NO_CURRENT_POSITION"
CODE_CONNECT_GAP = "CONNECT_GAP_EXCEEDED"
CODE_EMPTY_ROUTE = "EMPTY_ROUTE"
CODE_NOT_TERMINATING_HOME = "ROUTE_NOT_TERMINATING_AT_HOME"
CODE_INVALID_COORDINATE = "INVALID_COORDINATE"
CODE_DUPLICATE = "MALFORMED_DUPLICATE"
CODE_NO_GO_CROSSING = "NO_GO_ZONE_CROSSING"
CODE_BOUNDARY_VIOLATION = "NAVIGABLE_BOUNDARY_VIOLATION"
# Task section 4: the runtime launch Home is outside the survey navigable
# boundary and no approved Home corridor proves a safe connector to it. This is
# DISTINCT from a mid-survey boundary escape -- it identifies the specific
# geometry-contract gap the live bench hit, and tells the operator exactly what
# is missing (an Operator-provided `home_corridor`), rather than a generic
# "segment N leaves boundary".
CODE_HOME_OUTSIDE_BOUNDARY = "HOME_OUTSIDE_NAVIGABLE_BOUNDARY_NO_CORRIDOR"
CODE_CONNECTOR_CROSSES_NO_GO = "HOME_CORRIDOR_CROSSES_NO_GO"

_SAME_POINT_M = 0.5


def _valid_coord(lat, lon) -> bool:
    if lat is None or lon is None:
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    if lat == 0.0 and lon == 0.0:
        return False
    return True


def _home_of(package: dict) -> Optional[Tuple[float, float]]:
    home = (package or {}).get("home") or {}
    lat, lon = home.get("latitude"), home.get("longitude")
    return (lat, lon) if _valid_coord(lat, lon) else None


def _no_go_zones_latlon(package: dict) -> List[List[Tuple[float, float]]]:
    """Normalize the package's no-go zones into [[(lat,lon),...], ...]. Accepts
    either [lat,lon] pairs or {latitude,longitude} dicts. Silently skips a
    malformed vertex rather than failing -- a zone we can't parse simply isn't
    checked, and the route is still built from approved geometry."""
    out: List[List[Tuple[float, float]]] = []
    for zone in (package or {}).get("no_go_zones") or []:
        poly: List[Tuple[float, float]] = []
        for v in zone or []:
            if isinstance(v, dict):
                la, lo = v.get("latitude"), v.get("longitude")
            elif isinstance(v, (list, tuple)) and len(v) >= 2:
                la, lo = v[0], v[1]
            else:
                continue
            if la is not None and lo is not None:
                poly.append((la, lo))
        if len(poly) >= 3:
            out.append(poly)
    return out


def _polygon_field(package: dict, field: str) -> List[Tuple[float, float]]:
    """A named polygon field as [(lat,lon),...] (accepts pairs or dicts). Empty
    when none is supplied or it is degenerate (< 3 vertices)."""
    poly: List[Tuple[float, float]] = []
    for v in (package or {}).get(field) or []:
        if isinstance(v, dict):
            la, lo = v.get("latitude"), v.get("longitude")
        elif isinstance(v, (list, tuple)) and len(v) >= 2:
            la, lo = v[0], v[1]
        else:
            continue
        if la is not None and lo is not None:
            poly.append((la, lo))
    return poly if len(poly) >= 3 else []


def _boundary_latlon(package: dict) -> List[Tuple[float, float]]:
    """The survey navigable boundary as [(lat,lon),...]."""
    return _polygon_field(package, "navigable_boundary")


def _no_go_clearance_m(package: dict) -> float:
    """The effective no_go_clearance_m (metres) to buffer no_go_zones outward
    by: a finite, non-negative float, defaulting to 0.0 -- the historical
    raw-zone-only exclusion -- when the field is missing, non-numeric,
    negative, or non-finite. Mirrors
    planning_package.no_go_clearance_m_of's normalization exactly, duplicated
    locally so this module stays a pure, dependency-free function of the
    package dict (see module docstring)."""
    try:
        f = float((package or {}).get("no_go_clearance_m"))
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) and f >= 0 else 0.0


def _home_corridor_latlon(package: dict) -> List[Tuple[float, float]]:
    """OPTIONAL, Operator-provided approved Home/launch corridor polygon (task
    section 4). When present, a runtime Home outside the survey navigable_boundary
    may be reached through this explicitly-approved, no-go-constrained corridor
    instead of failing closed. Backward-compatible: absent on existing packages,
    in which case the connector to an out-of-boundary Home cannot be proven and
    the safe return fails closed (SAFE_HOLD), exactly as it did on the bench.

    Accepts `home_corridor` (preferred) or `return_corridor` (alias)."""
    poly = _polygon_field(package, "home_corridor")
    return poly if poly else _polygon_field(package, "return_corridor")


def _build_retrace_approved_route(snapshot, package: dict,
                                  cfg: Optional[replan_config.ReplanConfig] = None) -> dict:
    """
    Strategy (C), see module docstring: the ORIGINAL RETRACE_APPROVED
    construction, UNCHANGED from before the shortest-safe-return feature --

        [ current position ] + reverse( approved waypoints traversed ) + [ Home ]

    Kept verbatim as the fail-safe: it is proven safe by construction (every
    interior point is an already-approved, already-navigated waypoint), so it
    remains available exactly as-is for build_safe_return_route to fall back
    to when neither the direct nor the shortest-constrained-path preference
    can produce a route validate_route independently accepts. Returns the
    same shape as before (method now reports METHOD_RETRACE_FALLBACK since
    this function is only ever reached as a fallback -- see
    build_safe_return_route).
    """
    cfg = cfg or replan_config.DEFAULT

    def fail(code, reason):
        return {"ok": False, "reason_code": code, "reason": reason,
                "strategy": STRATEGY, "method": METHOD_RETRACE_FALLBACK, "route": [],
                "preserved_waypoint_count": 0, "removed_waypoint_count": 0,
                "inserted_waypoint_count": 0}

    if not package or not (package.get("route")):
        return fail(CODE_NO_PACKAGE,
                    "No usable approved planning package stored -- cannot build a "
                    "safe return without the approved route.")
    home = _home_of(package)
    if home is None:
        return fail(CODE_NO_HOME,
                    "Planning package has no valid Home -- refusing to build a "
                    "safe return to an unknown recovery point.")
    if not _valid_coord(snapshot.latitude, snapshot.longitude):
        return fail(CODE_NO_POSITION,
                    "No valid current position -- refusing to build a safe return "
                    "from an unknown start.")

    pos = (snapshot.latitude, snapshot.longitude)
    approved = package["route"]
    current_seq = snapshot.current_sequence if isinstance(snapshot.current_sequence, int) else 0
    current_seq = max(0, min(current_seq, len(approved)))

    traversed = approved[:current_seq]              # already-covered, approved
    remaining = approved[current_seq:]              # un-traversed survey legs

    # Connector: current position must be able to join the approved network
    # within a bounded gap. The join point is the last traversed waypoint, or
    # Home if nothing has been traversed yet.
    join = traversed[-1] if traversed else {"latitude": home[0], "longitude": home[1]}
    gap = geo.haversine_m(pos[0], pos[1], join["latitude"], join["longitude"])
    if gap > cfg.connect_gap_max_m:
        return fail(CODE_CONNECT_GAP,
                    f"Current position is {round(gap, 1)} m from the nearest approved "
                    f"point, exceeding the {cfg.connect_gap_max_m} m connector bound -- "
                    "failing closed rather than drawing a long unverified line.")

    # Assemble: current -> reversed(traversed) -> Home. Dedupe near-identical
    # consecutive points so the route carries no malformed duplicates.
    raw: List[Tuple[float, float, float]] = [(pos[0], pos[1], 0.0)]
    for wp in reversed(traversed):
        raw.append((wp["latitude"], wp["longitude"], wp.get("loiter_time_s", 0.0) or 0.0))
    raw.append((home[0], home[1], 0.0))

    route: List[dict] = []
    for lat, lon, loiter in raw:
        if route:
            prev = route[-1]
            if geo.haversine_m(prev["latitude"], prev["longitude"], lat, lon) < _SAME_POINT_M:
                continue
        route.append({"latitude": lat, "longitude": lon, "loiter_time_s": loiter})

    if not route:
        return fail(CODE_EMPTY_ROUTE, "Constructed route is empty after de-duplication.")

    # inserted = current position + Home terminus (the two non-approved points),
    # minus any that were deduped. preserved = retraced approved points. removed
    # = un-traversed approved survey legs left out of the return.
    preserved = len(traversed)
    inserted = len(route) - preserved
    return {
        "ok": True, "reason_code": CODE_OK,
        "reason": (f"Retrace of {preserved} approved waypoint(s) from the current "
                   f"position back to Home ({len(route)} route points)."),
        "strategy": STRATEGY, "method": METHOD_RETRACE_FALLBACK, "route": route,
        "preserved_waypoint_count": preserved,
        "removed_waypoint_count": len(remaining),
        "inserted_waypoint_count": max(0, inserted),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Shortest-safe-return search (preferences A + B, see module docstring).
# ─────────────────────────────────────────────────────────────────────────────
# Defensive bound on visibility-graph size only -- a real navigable_boundary /
# home_corridor / no_go_zones package carries a handful of polygon vertices
# (the live E2 package: one no-go zone). This exists purely so a pathological
# package can never trigger an expensive O(n^2) edge search; it is never
# expected to bind in practice (see module PERFORMANCE note in the task this
# module implements).
_MAX_GRAPH_NODES = 200

# A graph NODE placed exactly ON a polygon vertex/edge (a no_go zone corner at
# no_go_clearance_m == 0 -- the live E2 package's own value -- or a
# navigable_boundary/home_corridor corner) is numerically fragile: the
# ray-casting point-in-polygon test this module deliberately keeps
# dependency-free (no shapely -- see module docstring) is ambiguous for a
# point exactly on a polygon vertex/edge, so an edge touching that exact
# vertex can spuriously register as "entering" a no-go zone it should be
# outside of, or as "leaving" a boundary/corridor it should be inside of.
# This is a NODE-PLACEMENT epsilon only (five centimetres -- far below
# GPS/vessel precision): it nudges a candidate node just OUTSIDE a no-go
# zone / just INSIDE a boundary or corridor so edge search is numerically
# well-posed, while the actual containment/clearance every edge is checked
# against is always the package's real geometry, never this epsilon -- it
# relaxes nothing about what counts as a safe segment (a node nudged the
# "wrong" way on a concave polygon simply contributes no valid edges and is
# excluded by the search, never an unproven one -- see
# _shortest_route_candidate).
_NODE_PLACEMENT_EPSILON_M = 0.05


def _segment_geometrically_valid(
    a: Tuple[float, float], b: Tuple[float, float],
    boundary: List[Tuple[float, float]], corridor: List[Tuple[float, float]],
    zones: List[List[Tuple[float, float]]], no_go_clearance_m: float,
) -> bool:
    """
    True iff the lat/lon segment a->b is proven safe by the EXACT SAME
    authoritative geometry rules validate_route uses for the final,
    about-to-be-uploaded route: contained within navigable_boundary UNION
    home_corridor (only when either is supplied -- an entirely geometry-free
    package is unconstrained here exactly as validate_route treats it, see
    its "boundary or corridor" branch) and clear of every no_go zone buffered
    outward by no_go_clearance_m. This is the single validator both the
    direct-path test and every visibility-graph edge are checked against, so
    a shortest-path candidate can never be geometrically looser than the
    route that is ultimately validated before upload.
    """
    if boundary or corridor:
        if geo.route_within_regions([a, b], [boundary, corridor]) is not None:
            return False
    if zones:
        if geo.route_crosses_no_go([a, b], zones, no_go_clearance_m) is not None:
            return False
    return True


def _dedupe_points(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Drop points within _SAME_POINT_M of one already kept -- keeps the
    visibility graph's node set free of near-duplicate polygon vertices."""
    out: List[Tuple[float, float]] = []
    for p in points:
        if not any(geo.haversine_m(p[0], p[1], q[0], q[1]) < _SAME_POINT_M for q in out):
            out.append(p)
    return out


def _unit_normal(ex: float, ey: float) -> Tuple[float, float]:
    length = math.hypot(ex, ey)
    if length < 1e-9:
        return (0.0, 0.0)
    return (-ey / length, ex / length)


def _signed_area(xy: List[Tuple[float, float]]) -> float:
    """Shoelace signed area (planar, local metres). Positive = CCW winding,
    negative = CW -- used only to orient the interior side of an edge, which
    (unlike a centroid comparison) is correct for a REFLEX vertex on a
    concave polygon too."""
    total = 0.0
    n = len(xy)
    for i in range(n):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return 0.5 * total


def _offset_polygon_vertices_m(
    poly_latlon: List[Tuple[float, float]], offset_m: float,
) -> List[Tuple[float, float]]:
    """
    poly_latlon's own vertices, each moved along the local angle bisector of
    its two adjacent edges by offset_m in local projected metres -- INWARD
    (into the polygon's own interior) for offset_m > 0, OUTWARD for
    offset_m < 0. Used ONLY to seed visibility-graph candidate NODES just
    inside a navigable_boundary / home_corridor, or just outside a buffered
    no-go zone (see _NODE_PLACEMENT_EPSILON_M) -- it is a candidate-node
    placement heuristic, NEVER a safety proof by itself: every edge touching
    one of these nodes is still independently re-proven by
    _segment_geometrically_valid (the exact containment/no-go primitives
    validate_route uses) before it is ever added to the graph.

    The interior side of each edge is derived from the polygon's OWN winding
    order (shoelace sign), not from a centroid comparison -- a centroid
    heuristic silently picks the WRONG side at a REFLEX vertex on a concave
    polygon (the centroid can sit on the "outside" of a notch's inner
    corner), which would make every edge touching that node fail its
    containment check and silently drop a needed candidate node. Winding-
    order-based interior/exterior is a strictly local, always-correct
    property of a simple (non-self-intersecting) polygon, convex or not.

    Returns the raw vertices unchanged when offset_m == 0, the polygon is
    degenerate, or (defensively) its signed area is ~0 (degenerate/
    self-intersecting winding -- nothing reliable to orient against).
    """
    if offset_m == 0 or len(poly_latlon) < 3:
        return list(poly_latlon)
    ref_lat = sum(p[0] for p in poly_latlon) / len(poly_latlon)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(ref_lat))

    def to_xy(lat, lon):
        return (lon * m_per_deg_lon, lat * m_per_deg_lat)

    def to_ll(x, y):
        return (y / m_per_deg_lat, x / m_per_deg_lon)

    xy = [to_xy(la, lo) for la, lo in poly_latlon]
    area = _signed_area(xy)
    if abs(area) < 1e-6:
        return list(poly_latlon)
    # _unit_normal(edge) is the +90 deg (CCW) rotation of the edge direction,
    # which is the LEFT-of-edge normal -- the interior side for a CCW polygon
    # (positive area) and the exterior side for a CW polygon, so negate for CW.
    orientation = 1.0 if area > 0 else -1.0
    n = len(xy)
    out = []
    for i in range(n):
        px, py = xy[i]
        ax, ay = xy[i - 1]
        bx, by = xy[(i + 1) % n]
        n1x, n1y = _unit_normal(px - ax, py - ay)
        n2x, n2y = _unit_normal(bx - px, by - py)
        n1x, n1y = n1x * orientation, n1y * orientation
        n2x, n2y = n2x * orientation, n2y * orientation
        bisx, bisy = n1x + n2x, n1y + n2y
        blen = math.hypot(bisx, bisy)
        if blen < 1e-9:
            bisx, bisy, blen = n1x, n1y, (math.hypot(n1x, n1y) or 1.0)
        bisx, bisy = bisx / blen, bisy / blen
        out.append(to_ll(px + bisx * offset_m, py + bisy * offset_m))
    return out


def _shortest_route_candidate(
    pos: Tuple[float, float], home: Tuple[float, float],
    boundary: List[Tuple[float, float]], corridor: List[Tuple[float, float]],
    zones: List[List[Tuple[float, float]]], no_go_clearance_m: float,
) -> "Tuple[Optional[List[Tuple[float, float]]], int]":
    """
    Preference (B): a small, deterministic visibility graph over the current
    position, Home, and the vertices of the approved geometry -- reusing
    detour_planner's existing, previously groundwork-only Dijkstra
    (build_adjacency/shortest_path) rather than building a second shortest-
    path implementation. Returns (path_latlon, candidate_node_count); path is
    None when no edge-valid route connects pos to home (the caller then falls
    back to the always-safe retrace).
    """
    nodes: Dict[str, Tuple[float, float]] = {"CUR": pos, "HOME": home}
    # Boundary/corridor candidate nodes are nudged slightly INWARD (positive
    # offset -- see _NODE_PLACEMENT_EPSILON_M) so they register unambiguously
    # as contained rather than sitting exactly on the ray-casting-ambiguous
    # polygon edge.
    for i, v in enumerate(_dedupe_points(_offset_polygon_vertices_m(boundary, _NODE_PLACEMENT_EPSILON_M))):
        nodes[f"NAV_{i}"] = v
    for i, v in enumerate(_dedupe_points(_offset_polygon_vertices_m(corridor, _NODE_PLACEMENT_EPSILON_M))):
        nodes[f"COR_{i}"] = v
    for zi, zone in enumerate(zones):
        # No-go nodes are nudged OUTWARD (negative offset) by the real
        # clearance (or the epsilon, whichever is larger).
        node_offset_m = max(no_go_clearance_m, _NODE_PLACEMENT_EPSILON_M)
        offset = _offset_polygon_vertices_m(zone, -node_offset_m)
        for vi, v in enumerate(_dedupe_points(offset)):
            nodes[f"NOGO_{zi}_{vi}"] = v

    node_count = len(nodes)
    if node_count > _MAX_GRAPH_NODES:
        # Fail closed to the retrace fallback rather than guessing which
        # vertices to drop from an unexpectedly huge package.
        return None, node_count

    ids = sorted(nodes)  # deterministic node/edge ordering
    graph_nodes = {nid: {"id": nid, "x": 0.0, "y": 0.0} for nid in ids}
    edges = []
    for i in range(len(ids)):
        a_id = ids[i]
        a = nodes[a_id]
        for j in range(i + 1, len(ids)):
            b_id = ids[j]
            b = nodes[b_id]
            if _segment_geometrically_valid(a, b, boundary, corridor, zones, no_go_clearance_m):
                edges.append({"id": f"E{len(edges)}", "u": a_id, "v": b_id,
                              "length_m": geo.haversine_m(a[0], a[1], b[0], b[1])})
    graph = {"nodes": graph_nodes, "edges": edges}
    path_ids, _cost = detour_planner.shortest_path(graph, "CUR", "HOME")
    if path_ids is None:
        return None, node_count
    return [nodes[nid] for nid in path_ids], node_count


def _simplify_route_latlon(
    points: List[Tuple[float, float]],
    boundary: List[Tuple[float, float]], corridor: List[Tuple[float, float]],
    zones: List[List[Tuple[float, float]]], no_go_clearance_m: float,
) -> List[Tuple[float, float]]:
    """
    ROUTE SIMPLIFICATION: greedily drop any interior point whose neighbours
    (in the simplified route being built) can be joined directly by a segment
    that is STILL proven valid by the exact same
    _segment_geometrically_valid check every graph edge was proven with --
    i.e. for A -> B -> C, remove B iff A -> C is fully valid. Repeats (via the
    greedy furthest-reachable-next-hop scan below) until no further safe
    shortcut exists. Never removes a point by weakening the check -- a
    shortcut is taken only when it is independently re-proven, so
    simplification can only make an already-valid path shorter/leaner, never
    introduce an invalid segment.
    """
    if len(points) <= 2:
        return list(points)
    out = [points[0]]
    i = 0
    n = len(points)
    while i < n - 1:
        j = n - 1
        while j > i + 1 and not _segment_geometrically_valid(
                points[i], points[j], boundary, corridor, zones, no_go_clearance_m):
            j -= 1
        out.append(points[j])
        i = j
    return out


def _dedupe_consecutive_route(route: List[dict]) -> List[dict]:
    """Drop consecutive route points within _SAME_POINT_M -- mirrors the
    de-duplication _build_retrace_approved_route has always applied."""
    out: List[dict] = []
    for wp in route:
        if out:
            prev = out[-1]
            if geo.haversine_m(prev["latitude"], prev["longitude"],
                               wp["latitude"], wp["longitude"]) < _SAME_POINT_M:
                continue
        out.append(wp)
    return out


def _attempt_shortest_safe_return(
    pos: Tuple[float, float], home: Tuple[float, float],
    boundary: List[Tuple[float, float]], corridor: List[Tuple[float, float]],
    zones: List[List[Tuple[float, float]]], no_go_clearance_m: float,
) -> "Tuple[Optional[List[Tuple[float, float]]], Dict[str, Any]]":
    """
    Try preference (A) then (B). Returns (candidate_latlon_or_None, evidence)
    where evidence always carries direct_path_valid/candidate_node_count,
    win or lose, so the caller can attach that diagnostic even when it falls
    back to the retrace strategy.
    """
    direct_valid = _segment_geometrically_valid(pos, home, boundary, corridor, zones, no_go_clearance_m)
    if direct_valid:
        return [pos, home], {"direct_path_valid": True, "candidate_node_count": 2}

    path, node_count = _shortest_route_candidate(pos, home, boundary, corridor, zones, no_go_clearance_m)
    evidence = {"direct_path_valid": False, "candidate_node_count": node_count}
    if path is None:
        return None, evidence
    return _simplify_route_latlon(path, boundary, corridor, zones, no_go_clearance_m), evidence


def build_safe_return_route(snapshot, package: dict,
                            cfg: Optional[replan_config.ReplanConfig] = None) -> dict:
    """
    PLANNING step. Build the SAFE_RETURN_HOME route from the snapshot +
    approved package, preferring the shortest route the approved geometry
    proves safe (see module docstring, preferences A/B/C). Returns:

      {"ok": bool, "reason_code": str, "reason": str, "strategy", "method",
       "route": [{"latitude","longitude","loiter_time_s"}, ...],
       "preserved_waypoint_count", "removed_waypoint_count",
       "inserted_waypoint_count",
       "direct_path_valid", "candidate_node_count", "fallback_used",
       "planner_runtime_s"}

    `route` is the operator-route-equivalent (1..N); Home is included as the
    final NAV_WAYPOINT terminus (Scout owns Pixhawk seq 0 separately). ok is
    False (with route []) whenever a validated route cannot be constructed by
    EITHER the shortest-path search or the retrace fallback.
    """
    cfg = cfg or replan_config.DEFAULT
    t0 = time.monotonic()

    def finish(result: dict, **evidence) -> dict:
        result.setdefault("direct_path_valid", None)
        result.setdefault("candidate_node_count", None)
        result.setdefault("fallback_used", False)
        result.update(evidence)
        result["planner_runtime_s"] = round(time.monotonic() - t0, 4)
        return result

    # Preconditions (package/Home/position) are enforced identically by
    # _build_retrace_approved_route -- delegate straight to it rather than
    # duplicating the checks; it returns the exact same reason codes as
    # before for these cases (CODE_NO_PACKAGE / CODE_NO_HOME / CODE_NO_POSITION).
    home = _home_of(package) if package else None
    pos_ok = _valid_coord(getattr(snapshot, "latitude", None), getattr(snapshot, "longitude", None))
    if not package or not package.get("route") or home is None or not pos_ok:
        return finish(_build_retrace_approved_route(snapshot, package, cfg))

    pos = (snapshot.latitude, snapshot.longitude)
    approved = package["route"]
    current_seq = snapshot.current_sequence if isinstance(snapshot.current_sequence, int) else 0
    current_seq = max(0, min(current_seq, len(approved)))
    traversed = approved[:current_seq]
    remaining = approved[current_seq:]

    # Same connector-plausibility sanity gate as before (task: do not change
    # this bound's semantics/value) -- a current position implausibly far
    # from the known approved network fails closed before EITHER strategy is
    # attempted, exactly as it always has.
    join = traversed[-1] if traversed else {"latitude": home[0], "longitude": home[1]}
    gap = geo.haversine_m(pos[0], pos[1], join["latitude"], join["longitude"])
    if gap > cfg.connect_gap_max_m:
        return finish(_build_retrace_approved_route(snapshot, package, cfg))

    boundary = _boundary_latlon(package)
    corridor = _home_corridor_latlon(package)
    zones = _no_go_zones_latlon(package)
    no_go_clearance_m = _no_go_clearance_m(package)

    candidate, evidence = _attempt_shortest_safe_return(pos, home, boundary, corridor, zones, no_go_clearance_m)
    if candidate is not None:
        route = _dedupe_consecutive_route(
            [{"latitude": la, "longitude": lo, "loiter_time_s": 0.0} for la, lo in candidate])
        if route:
            # Never hand back a merely-constructed candidate unproven: the
            # SAME authoritative validate_route the controller calls again
            # afterward gates it here too, so a shortest-path candidate that
            # somehow fails final validation is rejected/falls back, never
            # returned as the winner (edge case: task section "EDGE CASES" #10).
            check = validate_route(route, package, snapshot, cfg)
            if check["valid"]:
                built = {
                    "ok": True, "reason_code": CODE_OK,
                    "reason": (f"Shortest safe return via "
                              f"{'direct line' if evidence['direct_path_valid'] else 'constrained shortest path'} "
                              f"through approved geometry: {len(route)} route point(s)."),
                    "strategy": STRATEGY, "method": METHOD_SHORTEST, "route": route,
                    "preserved_waypoint_count": 0,
                    "removed_waypoint_count": len(remaining),
                    "inserted_waypoint_count": len(route),
                }
                return finish(built, fallback_used=False, **evidence)

    fallback = _build_retrace_approved_route(snapshot, package, cfg)
    return finish(fallback, fallback_used=True, **evidence)


def validate_route(route: List[dict], package: dict,
                   snapshot=None,
                   cfg: Optional[replan_config.ReplanConfig] = None) -> dict:
    """
    VALIDATING step. Independent, fail-closed validation of a built route BEFORE
    it is uploaded (the Flask upload service validates again authoritatively;
    this catches problems locally first and never uploads a bad route). Returns
    {"valid": bool, "reason_code", "reason", "checks": {...}}.
    """
    cfg = cfg or replan_config.DEFAULT
    checks: Dict[str, Any] = {}

    boundary = _boundary_latlon(package)
    corridor = _home_corridor_latlon(package)
    zones = _no_go_zones_latlon(package)
    no_go_clearance_m = _no_go_clearance_m(package)
    shoreline_scalar = (package or {}).get("shoreline_clearance_m")
    home_pt = _home_of(package)
    home_in_boundary = bool(boundary and home_pt is not None
                            and geo.point_in_polygon(home_pt, boundary))
    home_in_corridor = bool(corridor and home_pt is not None
                            and geo.point_in_polygon(home_pt, corridor))
    geometry = {
        "boundary_available": bool(boundary),
        "boundary_checked": False,
        # Explicit Home-connector contract evidence (task section 4). runtime_home
        # is the terminus the route must reach -- the launch Home synced at Start;
        # it is distinguished from the survey polygon so an out-of-boundary Home is
        # visible rather than silently rejected as a generic boundary violation.
        "runtime_home": ({"latitude": home_pt[0], "longitude": home_pt[1]}
                         if home_pt is not None else None),
        "home_in_navigable_boundary": home_in_boundary,
        "home_corridor_available": bool(corridor),
        "home_corridor_checked": False,
        "home_in_corridor": home_in_corridor,
        "no_go_available": bool(zones),
        "no_go_checked": False,
        # Effective outward buffer (metres) applied to no_go_zones for this
        # validation -- 0.0 for a legacy package with no field (see
        # planning_package.no_go_clearance_m_of / _no_go_clearance_m above).
        # Always surfaced, even when no_go_available is False, for evidence.
        "no_go_clearance_m": no_go_clearance_m,
        # A bare shoreline_clearance_m scalar is NOT directly usable as geometry
        # (there is no shoreline polygon to offset), so it is never treated as an
        # available check -- see limitations.
        "shoreline_clearance_available": False,
        "connector_proven_safe": False,
        "limitations": [],
    }

    def bad(code, reason):
        return {"valid": False, "reason_code": code, "reason": reason,
                "checks": checks, "geometry_validation": geometry}

    if not route:
        return bad(CODE_EMPTY_ROUTE, "Route is empty.")

    home = _home_of(package)
    if home is None:
        return bad(CODE_NO_HOME, "Package has no valid Home to validate the terminus against.")

    # Every coordinate valid, no consecutive duplicates.
    for i, wp in enumerate(route):
        lat, lon = wp.get("latitude"), wp.get("longitude")
        if not _valid_coord(lat, lon):
            return bad(CODE_INVALID_COORDINATE, f"Route point {i} has an invalid coordinate ({lat},{lon}).")
        if wp.get("loiter_time_s", 0) < 0:
            return bad(CODE_INVALID_COORDINATE, f"Route point {i} has a negative loiter time.")
        if i > 0:
            prev = route[i - 1]
            if geo.haversine_m(prev["latitude"], prev["longitude"], lat, lon) < _SAME_POINT_M:
                return bad(CODE_DUPLICATE, f"Route points {i-1} and {i} are duplicates.")
    checks["coordinate_count"] = len(route)

    # Must terminate at Home.
    last = route[-1]
    home_gap = geo.haversine_m(last["latitude"], last["longitude"], home[0], home[1])
    checks["terminus_gap_to_home_m"] = round(home_gap, 2)
    if home_gap > _SAME_POINT_M:
        return bad(CODE_NOT_TERMINATING_HOME,
                   f"Route terminus is {round(home_gap, 1)} m from Home; a safe return "
                   "must end at verified Home.")

    # Must begin at/near the current position, within the connector bound.
    if snapshot is not None and _valid_coord(snapshot.latitude, snapshot.longitude):
        first = route[0]
        start_gap = geo.haversine_m(first["latitude"], first["longitude"],
                                    snapshot.latitude, snapshot.longitude)
        checks["start_gap_to_position_m"] = round(start_gap, 2)
        if start_gap > cfg.connect_gap_max_m:
            return bad(CODE_CONNECT_GAP,
                       f"Route start is {round(start_gap, 1)} m from the current position, "
                       f"exceeding the {cfg.connect_gap_max_m} m connector bound.")

    latlon = [(wp["latitude"], wp["longitude"]) for wp in route]

    # Navigable containment (task section 4). Every segment (connector + Home leg
    # included) must lie fully inside the approved SURVEY navigable_boundary OR,
    # for the launch/Home connector, inside an approved Home CORRIDOR. Fail closed
    # on a violation -- a short connector is NOT treated as safe merely because it
    # is short; it must be contained by explicitly-approved geometry.
    #
    # The critical distinction the live bench exposed: Start syncs Pixhawk Home to
    # the actual launch position, which may be OUTSIDE the survey polygon. A route
    # terminating at that runtime Home then leaves the boundary on its final leg.
    # That is NOT a mid-survey escape and must NOT be papered over by relaxing the
    # boundary -- it requires an approved corridor (an Operator-provided
    # `home_corridor` polygon) that contains the connector and is itself no-go
    # constrained. Without such a corridor, we fail closed with a SPECIFIC reason.
    checks["navigable_boundary_vertices"] = len(boundary)
    checks["home_corridor_vertices"] = len(corridor)
    if boundary or corridor:
        geometry["boundary_checked"] = bool(boundary)
        geometry["home_corridor_checked"] = bool(corridor)
        # A segment is contained if it lies within the survey boundary OR the
        # approved Home corridor. The corridor is expected to OVERLAP the boundary
        # at the connector-entry region so each transition segment is wholly inside
        # the corridor (see geo.route_within_regions).
        outside = geo.route_within_regions(latlon, [boundary, corridor])
        if outside is not None:
            # Diagnose the specific, actionable case: the offending segment is the
            # final leg to a runtime Home that sits outside the survey boundary and
            # is not covered by an approved corridor.
            is_home_leg = outside == len(latlon) - 2
            if is_home_leg and boundary and not home_in_boundary and not home_in_corridor:
                geometry["limitations"].append(
                    "runtime launch Home is outside the survey navigable_boundary and no approved "
                    "home_corridor contains the connector to it; safe return fails closed. Operator "
                    "must supply a `home_corridor` polygon (overlapping the boundary at the connector "
                    "entry, itself no-go constrained) to prove a safe launch/Home connector.")
                return bad(CODE_HOME_OUTSIDE_BOUNDARY,
                           f"Route segment {outside} (final leg to Home) leaves the approved "
                           "navigable boundary and no approved Home corridor proves it; a runtime "
                           "Home outside the survey area needs an Operator-approved home_corridor.")
            return bad(CODE_BOUNDARY_VIOLATION,
                       f"Route segment {outside} lies outside all approved geometry "
                       f"(navigable boundary / Home corridor).")

    # No-go zones (only when locally available -- see geo.route_crosses_no_go).
    # The Home corridor is NOT exempt from no-go constraints: the whole route,
    # connector included, is checked against every known no-go zone.
    checks["no_go_zone_count"] = len(zones)
    checks["no_go_clearance_m"] = no_go_clearance_m
    if zones:
        geometry["no_go_checked"] = True
        crossing = geo.route_crosses_no_go(latlon, zones, no_go_clearance_m)
        if crossing is not None:
            code = (CODE_CONNECTOR_CROSSES_NO_GO
                    if corridor and crossing == len(latlon) - 2 else CODE_NO_GO_CROSSING)
            clearance_note = (f" (within the required {no_go_clearance_m} m clearance)"
                              if no_go_clearance_m > 0 else "")
            return bad(code,
                       f"Route segment {crossing} crosses a known no-go zone{clearance_note}.")

    # Connector safety verdict + honest limitations. The connector is the first
    # segment (current position -> first approved point); the Home leg is the last.
    # "Proven safe" only when the route was validated for CONTAINMENT against
    # approved geometry (boundary and/or corridor) and passed -- never merely
    # because a length is under connect_gap_max_m.
    geometry["connector_proven_safe"] = bool(geometry["boundary_checked"]
                                             or geometry["home_corridor_checked"])
    if not (boundary or corridor):
        geometry["limitations"].append(
            "no navigable boundary or Home corridor geometry available; connector/route safety "
            "proven only by the bounded connector length (and no-go checks if present), not by "
            "containment")
    if home_pt is not None and boundary and not home_in_boundary and not home_in_corridor:
        # Reachable here only when the route did not actually terminate outside
        # (e.g. degenerate boundary); still surface that Home sits out of the
        # survey polygon with no corridor, as an explicit limitation.
        geometry["limitations"].append(
            "runtime Home is outside the survey navigable_boundary and no home_corridor is provided")
    if not geometry["no_go_checked"]:
        geometry["limitations"].append("no no-go geometry available to check against")
    if shoreline_scalar is not None:
        geometry["limitations"].append(
            "shoreline_clearance_m supplied as a scalar only; not directly usable as geometry, "
            "so shoreline clearance was NOT checked")

    return {"valid": True, "reason_code": CODE_OK,
            "reason": (f"Route validated: {len(route)} points, terminates at Home; "
                       f"boundary_checked={geometry['boundary_checked']}, "
                       f"home_corridor_checked={geometry['home_corridor_checked']}, "
                       f"no_go_checked={geometry['no_go_checked']}, "
                       f"connector_proven_safe={geometry['connector_proven_safe']}."),
            "checks": checks, "geometry_validation": geometry}
