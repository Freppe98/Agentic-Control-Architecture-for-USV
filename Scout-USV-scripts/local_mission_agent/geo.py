"""
Dependency-free planar/spherical geometry helpers for the replanning feature.

Deliberately no shapely/numpy -- these are small, well-understood functions
used for return-distance estimation and for checking a safe-return route
against locally-available no-go geometry. All coordinates are (latitude,
longitude) in degrees unless noted; distances are metres.

Accuracy note: haversine is exact-enough for a single lake at Scout's scale;
the no-go checks use an equirectangular projection about a reference latitude
(the same approximation mission_graph.py uses), good to well under a metre over
one operating area.
"""
import math
from typing import List, Optional, Sequence, Tuple

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def path_length_m(points: Sequence[Tuple[float, float]]) -> float:
    """Total length of a polyline given as [(lat, lon), ...] in metres."""
    total = 0.0
    for i in range(len(points) - 1):
        (la, lo), (lb, lob) = points[i], points[i + 1]
        total += haversine_m(la, lo, lb, lob)
    return total


def _projector(ref_lat: float):
    """Equirectangular (lat, lon)-degrees -> local (x_east_m, y_north_m)."""
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(ref_lat))

    def to_xy(lat: float, lon: float) -> Tuple[float, float]:
        return (lon * m_per_deg_lon, lat * m_per_deg_lat)

    return to_xy


def _point_in_polygon(px: float, py: float, poly: List[Tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon in local metres. poly is [(x, y), ...]."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _segments_cross(p1, p2, p3, p4) -> bool:
    """True if segment p1-p2 properly intersects segment p3-p4 (planar metres)."""
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])

    d1 = ccw(p3, p4, p1)
    d2 = ccw(p3, p4, p2)
    d3 = ccw(p1, p2, p3)
    d4 = ccw(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


def segment_intersects_polygon(
    a: Tuple[float, float], b: Tuple[float, float],
    polygon_latlon: List[Tuple[float, float]],
) -> bool:
    """
    True if the lat/lon segment a->b enters/crosses the given polygon (a list of
    (lat, lon) vertices, implicitly closed). Used to reject a safe-return
    segment that would pass through a known no-go zone when that geometry is
    locally available.

    Detects both a boundary crossing and the fully-contained case (either
    endpoint inside the polygon). Returns False for a degenerate polygon
    (< 3 vertices) -- there is nothing to be inside of.
    """
    if not polygon_latlon or len(polygon_latlon) < 3:
        return False
    all_lat = [a[0], b[0]] + [v[0] for v in polygon_latlon]
    ref_lat = sum(all_lat) / len(all_lat)
    to_xy = _projector(ref_lat)

    ax, ay = to_xy(a[0], a[1])
    bx, by = to_xy(b[0], b[1])
    poly = [to_xy(la, lo) for la, lo in polygon_latlon]

    if _point_in_polygon(ax, ay, poly) or _point_in_polygon(bx, by, poly):
        return True

    n = len(poly)
    for i in range(n):
        p3 = poly[i]
        p4 = poly[(i + 1) % n]
        if _segments_cross((ax, ay), (bx, by), p3, p4):
            return True
    return False


def point_in_polygon(point_latlon: Tuple[float, float],
                     polygon_latlon: List[Tuple[float, float]]) -> bool:
    """True if the (lat, lon) point lies inside the polygon (implicitly closed).
    False for a degenerate polygon (< 3 vertices)."""
    if not polygon_latlon or len(polygon_latlon) < 3:
        return False
    all_lat = [point_latlon[0]] + [v[0] for v in polygon_latlon]
    to_xy = _projector(sum(all_lat) / len(all_lat))
    px, py = to_xy(point_latlon[0], point_latlon[1])
    poly = [to_xy(la, lo) for la, lo in polygon_latlon]
    return _point_in_polygon(px, py, poly)


def segment_within_polygon(
    a: Tuple[float, float], b: Tuple[float, float],
    polygon_latlon: List[Tuple[float, float]],
) -> bool:
    """
    True if the whole lat/lon segment a->b lies inside the polygon: both
    endpoints inside AND the segment crosses no boundary edge (which proves
    containment for a simple polygon, convex or not). False for a degenerate
    polygon -- containment cannot be proven, so callers fail closed.
    """
    if not polygon_latlon or len(polygon_latlon) < 3:
        return False
    all_lat = [a[0], b[0]] + [v[0] for v in polygon_latlon]
    to_xy = _projector(sum(all_lat) / len(all_lat))
    ax, ay = to_xy(a[0], a[1])
    bx, by = to_xy(b[0], b[1])
    poly = [to_xy(la, lo) for la, lo in polygon_latlon]
    if not (_point_in_polygon(ax, ay, poly) and _point_in_polygon(bx, by, poly)):
        return False
    n = len(poly)
    for i in range(n):
        if _segments_cross((ax, ay), (bx, by), poly[i], poly[(i + 1) % n]):
            return False
    return True


def route_outside_boundary(
    route_latlon: List[Tuple[float, float]],
    boundary_latlon: Optional[List[Tuple[float, float]]],
) -> Optional[int]:
    """
    If any segment of the route is not fully within the navigable boundary,
    return the 0-based index of the first offending segment; otherwise None. A
    None/empty/degenerate boundary means no usable boundary geometry is
    available -- returns None ("cannot check"); the caller must treat that as
    "boundary not checked", never as "proven inside".
    """
    if not boundary_latlon or len(boundary_latlon) < 3:
        return None
    for i in range(len(route_latlon) - 1):
        if not segment_within_polygon(route_latlon[i], route_latlon[i + 1], boundary_latlon):
            return i
    return None


def route_within_regions(
    route_latlon: List[Tuple[float, float]],
    regions: List[Optional[List[Tuple[float, float]]]],
) -> Optional[int]:
    """
    Multi-region containment (task section 4 Home-connector contract). Each route
    segment must lie fully within AT LEAST ONE of the supplied polygons (e.g. the
    survey navigable_boundary OR an approved Home corridor). Returns the 0-based
    index of the first segment contained in NO region, else None.

    Regions that are None/empty/degenerate are ignored. If NO usable region is
    supplied at all, returns None ("cannot check") -- the caller must treat that
    as "not checked", never "proven inside", exactly like route_outside_boundary.
    A segment straddling two regions (crossing their shared edge) is NOT proven by
    this per-segment test and is reported; the corridor is expected to OVERLAP the
    boundary at the connector-entry region so each transition segment lies wholly
    within the corridor.
    """
    usable = [p for p in regions if p and len(p) >= 3]
    if not usable:
        return None
    for i in range(len(route_latlon) - 1):
        a, b = route_latlon[i], route_latlon[i + 1]
        if not any(segment_within_polygon(a, b, poly) for poly in usable):
            return i
    return None


def _point_to_segment_distance_m(px: float, py: float,
                                 ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from point (px,py) to segment (ax,ay)-(bx,by), local metres."""
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _segment_to_segment_distance_m(p1, p2, p3, p4) -> float:
    """Minimum distance between segment p1-p2 and segment p3-p4, local metres.
    Zero if they intersect (including a touching endpoint)."""
    if _segments_cross(p1, p2, p3, p4):
        return 0.0
    return min(
        _point_to_segment_distance_m(p1[0], p1[1], p3[0], p3[1], p4[0], p4[1]),
        _point_to_segment_distance_m(p2[0], p2[1], p3[0], p3[1], p4[0], p4[1]),
        _point_to_segment_distance_m(p3[0], p3[1], p1[0], p1[1], p2[0], p2[1]),
        _point_to_segment_distance_m(p4[0], p4[1], p1[0], p1[1], p2[0], p2[1]),
    )


def segment_distance_to_polygon_m(
    a: Tuple[float, float], b: Tuple[float, float],
    polygon_latlon: List[Tuple[float, float]],
) -> Optional[float]:
    """
    Minimum distance in metres between the lat/lon segment a->b and the given
    polygon (edges), in the same local equirectangular projection the rest of
    this module uses. Zero when either endpoint lies inside the polygon or the
    segment crosses an edge. None for a degenerate polygon (< 3 vertices) --
    there is nothing to measure a distance to.

    This is the primitive an outward polygon buffer is checked against without
    ever constructing the buffered polygon itself: a segment lies outside a
    polygon buffered by `clearance_m` iff this distance is >= clearance_m (see
    route_crosses_no_go).
    """
    if not polygon_latlon or len(polygon_latlon) < 3:
        return None
    all_lat = [a[0], b[0]] + [v[0] for v in polygon_latlon]
    ref_lat = sum(all_lat) / len(all_lat)
    to_xy = _projector(ref_lat)
    pa = to_xy(a[0], a[1])
    pb = to_xy(b[0], b[1])
    poly = [to_xy(la, lo) for la, lo in polygon_latlon]
    if _point_in_polygon(pa[0], pa[1], poly) or _point_in_polygon(pb[0], pb[1], poly):
        return 0.0
    n = len(poly)
    best = math.inf
    for i in range(n):
        d = _segment_to_segment_distance_m(pa, pb, poly[i], poly[(i + 1) % n])
        if d < best:
            best = d
    return best


def route_crosses_no_go(
    route_latlon: List[Tuple[float, float]],
    no_go_zones: Optional[List[List[Tuple[float, float]]]],
    clearance_m: float = 0.0,
) -> Optional[int]:
    """
    If any segment of the route crosses any no-go zone, return the 0-based index
    of the first offending segment; otherwise None. A None/empty no_go_zones
    means no local no-go geometry is available to check against -- returns None
    (nothing to reject on), which the planner treats as "cannot prove a
    crossing", relying instead on the route being built from approved geometry.

    `clearance_m` (default 0.0, fully backward compatible) additionally treats a
    segment that comes within `clearance_m` of a zone -- without necessarily
    crossing it -- as a violation, i.e. checks against the zone buffered
    outward by `clearance_m` (see segment_distance_to_polygon_m). At
    clearance_m == 0 this is exactly the original raw-polygon crossing check: a
    segment may approach the boundary, only entering it is rejected.
    """
    if not no_go_zones:
        return None
    for i in range(len(route_latlon) - 1):
        a = route_latlon[i]
        b = route_latlon[i + 1]
        for zone in no_go_zones:
            if segment_intersects_polygon(a, b, zone):
                return i
            if clearance_m > 0:
                d = segment_distance_to_polygon_m(a, b, zone)
                if d is not None and d < clearance_m:
                    return i
    return None
