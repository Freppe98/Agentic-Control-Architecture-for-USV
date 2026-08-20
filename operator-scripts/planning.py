"""Operator-side side-scan-sonar survey planning — coverage generation, return-path
planning, segmentation, metrics and deterministic validation for the Plan page.

WHY THIS MODULE OWNS GENERATION (and where the algorithm came from)
-------------------------------------------------------------------
Mission planning belongs to the Operator Station: Scout receives a finalized, validated
mission package, it does not compute the survey. The coverage algorithm itself, however,
is NOT rewritten from memory — the boustrophedon (lawnmower) generator with obstacle
avoidance and the A* return-path planner are PORTED, preserving their geometry behaviour,
from the authoritative Scout AqualityONE implementation:

    flask/lawnmower_with_obstacles.py :: run_lawnmower_with_obstacles
    flask/app.py                      :: compute_return_path

(original lawnmower author: Peter Lehnér & Gemini-2.5-Pro; integrated into the Aquality USV
Mission Planner.) Porting rather than reimplementing is deliberate: a second, differently
written coverage algorithm would drift from the one the fleet was validated against. This
module keeps the two ported functions faithful and adds ONLY an operator-side orchestration
layer (segmentation, metrics, validation) on top.

ONE DELIBERATE DEPARTURE — HOW A NO-GO ZONE IS ROUTED AROUND
-----------------------------------------------------------
The ported generator's OBSTACLE handling bridges a lane that an obstacle cut in two by walking
ALONG THE OBSTACLE BOUNDARY. With the operator's no-go clearance applied, the obstacle it is
handed is the ROUND buffered exclusion, so those bridges came out as chains of ~1 m chords
tracing a buffer arc — safe, but a rounded, constantly-turning "coverage" leg with no stable
sonar heading. Side-scan sonar wants the opposite: long straight legs on one heading.

Coverage generation therefore runs through _survey_frame_coverage (see the SURVEY-FRAME COVERAGE
GENERATION banner) instead: the SAME lane family at the SAME spacing and sweep order, but each
lane is CLIPPED against the approved region into straight survey-angle-parallel fragments, and
fragments are joined with survey-frame-orthogonal transitions. run_lawnmower_with_obstacles and
its obstacle-bridging helpers are retained below as the ported reference geometry (and are what
the no-obstacle lane family is still faithful to), but they no longer produce the mission route.
The safety geometry is IDENTICAL either way — same shoreline inset, same buffered no-go
exclusion, same _NavGrid.segment_is_safe on every leg.

DEPENDENCIES / GRACEFUL DEGRADATION
-----------------------------------
Generation needs shapely + pyproj + numpy (geometry, UTM projection, scan-line math). The
rest of the operator backend deliberately runs on fastapi + stdlib only, so these heavy
imports are GUARDED here: if they are not installed, PLANNING_AVAILABLE is False and the
module still imports. The Plan endpoints in main.py check that flag and answer with an
honest "planning backend unavailable" rather than a 500 — the UI-honesty rule applied to a
whole feature: never pretend a capability the backend cannot currently back up.

COORDINATE CONVENTION
---------------------
All geometry crosses this module's boundary as GeoJSON [longitude, latitude] pairs, the
same order Scout's generator uses and the same order the frontend Leaflet layer stores. The
mission-contract route waypoints it emits are {latitude, longitude, loiter_time_s} — the
route-only shape mission_contract.py hashes and POST /api/commands (MISSION_UPLOAD) accepts.
"""

import hashlib
import heapq
import json
import math
import time
from datetime import datetime, timezone

import mission_contract  # stdlib-only (hashlib/json) — safe even when geometry deps absent

try:  # heavy geometry stack — see module docstring "GRACEFUL DEGRADATION"
    import numpy as np
    import pyproj
    from shapely.geometry import (Polygon, LineString, MultiPolygon, Point,
                                   MultiLineString, MultiPoint, GeometryCollection)
    from shapely.ops import unary_union, transform
    from shapely.affinity import rotate
    PLANNING_AVAILABLE = True
    PLANNING_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - exercised only where deps are absent
    PLANNING_AVAILABLE = False
    PLANNING_IMPORT_ERROR = str(_exc)

TOLERANCE = 1e-9

# Duration is an ESTIMATE only (the UI labels it so). This is the fallback planning speed
# used when the operator supplies none; it is deliberately conservative and configurable
# from the request — never an invented sonar-specific default baked in as truth. Lowered
# from 1.5 to 1.0 m/s to match the fleet-wide survey-speed default (single + fleet planning).
DEFAULT_PLANNING_SPEED_MPS = 1.0

# ── Planning-parameter defaults a FRESH plan starts from ────────────────────────────────
# These are the values the Plan page shows immediately (operator/lib/planning.js
# defaultParams mirrors them) and the values applied when a caller — an older draft, a
# script, a replayed request — omits the field entirely. They are NOT applied to a supplied
# value: an explicit 0 clearance means zero clearance, and an explicit non-positive lane
# spacing is still an error.
#
# `shoreline_clearance_m` keeps its historic ABSENT semantics (0.0, see
# normalize_generate_inputs) so no stored plan silently changes meaning; the 5 m default is
# what the UI starts a NEW plan with.
DEFAULT_SHORELINE_CLEARANCE_M = 5.0
DEFAULT_NO_GO_CLEARANCE_M = 5.0
DEFAULT_LANE_SPACING_M = 10.0


class PlanningUnavailable(RuntimeError):
    """Raised when a generation/validation entry point is called but the geometry stack is
    not importable. main.py maps this to an honest 503, never a 500."""


def _require_available():
    if not PLANNING_AVAILABLE:
        raise PlanningUnavailable(
            "Survey planning requires shapely, pyproj and numpy, which are not installed "
            f"in this backend environment ({PLANNING_IMPORT_ERROR}).")


# ═══════════════════════════════════════════════════════════════════════════════════════
# PORTED FROM SCOUT — lawnmower_with_obstacles.py. Kept faithful to preserve geometry
# behaviour; only cosmetic (import location) differs. See module docstring.
# ═══════════════════════════════════════════════════════════════════════════════════════

def _generate_boustrophedon_path(main_polygon_deg, separation_meters=10.0, angle_deg=90.0, safety_distance=0.0):
    crs_deg = "EPSG:4326"
    centroid_deg = main_polygon_deg.centroid
    utm_zone = int((centroid_deg.x + 180) / 6) + 1
    hemi = 6 if centroid_deg.y >= 0 else 7
    crs_proj = f"EPSG:32{hemi}{utm_zone:02d}"
    to_proj = pyproj.Transformer.from_crs(crs_deg, crs_proj, always_xy=True)
    to_deg = pyproj.Transformer.from_crs(crs_proj, crs_deg, always_xy=True)

    main_proj = transform(to_proj.transform, main_polygon_deg)

    if safety_distance > TOLERANCE:
        inset = main_proj.buffer(-abs(safety_distance), join_style=2)
        if not inset.is_empty and inset.is_valid:
            main_proj = inset

    centroid_proj = main_proj.centroid
    math_angle = (90.0 - angle_deg) % 360.0
    geom_rot = rotate(main_proj, -math_angle, origin=centroid_proj)

    minx, miny, maxx, maxy = geom_rot.bounds
    width = maxx - minx
    height = maxy - miny
    sep = abs(separation_meters) if separation_meters > 1e-6 else 0.1

    y_coords = np.linspace(miny + sep / 2, maxy - sep / 2, num=max(1, int(height / sep)))
    if not y_coords.size:
        y_coords = np.array([(miny + maxy) / 2])

    clipped_lines = []
    for y in y_coords:
        scan = LineString([(minx - width * 1.1, y), (maxx + width * 1.1, y)])
        clipped = geom_rot.buffer(0).intersection(scan)
        if clipped.is_empty:
            continue
        segs = []
        if isinstance(clipped, LineString) and clipped.length > TOLERANCE:
            segs = [clipped]
        elif isinstance(clipped, MultiLineString):
            segs = [s for s in clipped.geoms if s.length > TOLERANCE]
        if segs:
            segs.sort(key=lambda s: s.coords[0][0])
            clipped_lines.append(segs)

    if not clipped_lines:
        return LineString(), centroid_proj, to_deg

    path_coords = []
    direction = True
    last_end = None

    for segs in clipped_lines:
        conn = segs[0].coords[0] if direction else segs[-1].coords[-1]
        end = segs[-1].coords[-1] if direction else segs[0].coords[0]
        if last_end:
            if Point(last_end).distance(Point(conn)) > TOLERANCE:
                path_coords.append(last_end)
                path_coords.append(conn)
            elif not path_coords or Point(path_coords[-1]).distance(Point(conn)) > TOLERANCE:
                path_coords.append(conn)
        elif not path_coords:
            path_coords.append(conn)
        line_pts = []
        if direction:
            for seg in segs:
                c = list(seg.coords)
                if not line_pts or Point(line_pts[-1]).distance(Point(c[0])) > TOLERANCE:
                    line_pts.append(c[0])
                line_pts.extend(c[1:])
        else:
            for seg in reversed(segs):
                c = list(seg.coords)[::-1]
                if not line_pts or Point(line_pts[-1]).distance(Point(c[0])) > TOLERANCE:
                    line_pts.append(c[0])
                line_pts.extend(c[1:])
        if path_coords and Point(path_coords[-1]).distance(Point(line_pts[0])) < TOLERANCE:
            path_coords.extend(line_pts[1:])
        else:
            path_coords.extend(line_pts)
        last_end = end
        direction = not direction

    if last_end and (not path_coords or Point(path_coords[-1]).distance(Point(last_end)) > TOLERANCE):
        path_coords.append(last_end)

    if len(path_coords) < 2:
        return LineString(), centroid_proj, to_deg

    cleaned = [path_coords[0]]
    for p in path_coords[1:]:
        if Point(p).distance(Point(cleaned[-1])) > TOLERANCE:
            cleaned.append(p)

    path_rot = LineString(cleaned)
    path_rot = path_rot.simplify(TOLERANCE * 10, preserve_topology=True)
    if path_rot.is_empty or len(path_rot.coords) < 2:
        return LineString(), centroid_proj, to_deg

    path_proj = rotate(path_rot, math_angle, origin=centroid_proj)

    final_clean = [path_proj.coords[0]]
    for p in path_proj.coords[1:]:
        if Point(p).distance(Point(final_clean[-1])) > TOLERANCE:
            final_clean.append(p)

    if len(final_clean) < 2:
        return LineString(), centroid_proj, to_deg

    return LineString(final_clean), centroid_proj, to_deg


def _split_path_by_obstacles(base_path, obstacles):
    if not base_path.is_valid or base_path.is_empty:
        return [base_path]
    valid_obs = [o for o in obstacles if o.is_valid and not o.is_empty]
    if not valid_obs:
        return [base_path]
    try:
        obs_union = unary_union(valid_obs)
        diff = base_path.difference(obs_union)
    except Exception:
        return [base_path]

    tracks = []
    if diff.is_empty:
        return []
    elif isinstance(diff, LineString):
        if diff.length > TOLERANCE:
            tracks = [diff]
    elif isinstance(diff, MultiLineString):
        tracks = sorted([l for l in diff.geoms if l.length > TOLERANCE],
                        key=lambda l: base_path.project(Point(l.coords[0])) if l.coords else float('inf'))
    elif isinstance(diff, GeometryCollection):
        lines = []
        for g in diff.geoms:
            if isinstance(g, LineString) and g.length > TOLERANCE:
                lines.append(g)
            elif isinstance(g, MultiLineString):
                lines.extend([l for l in g.geoms if l.length > TOLERANCE])
        tracks = sorted(lines, key=lambda l: base_path.project(Point(l.coords[0])) if l.coords else float('inf'))

    cleaned = []
    for t in tracks:
        if not t.coords:
            continue
        c = [t.coords[0]]
        for p in t.coords[1:]:
            if Point(p).distance(Point(c[-1])) > TOLERANCE:
                c.append(p)
        if len(c) >= 2:
            cleaned.append(LineString(c))
    return cleaned


def _find_intersection_points(path, obstacles):
    valid_obs = [o for o in obstacles if isinstance(o, (Polygon, MultiPolygon)) and o.is_valid and not o.is_empty]
    if not valid_obs or path.is_empty:
        return []
    try:
        boundaries = unary_union([o.boundary for o in valid_obs])
        inter = path.intersection(boundaries)
    except Exception:
        return []
    if inter.is_empty:
        return []

    pts = []
    if isinstance(inter, Point):
        pts.append((inter.x, inter.y))
    elif isinstance(inter, MultiPoint):
        for p in inter.geoms:
            pts.append((p.x, p.y))
    elif isinstance(inter, (LineString, MultiLineString, GeometryCollection)):
        geoms = inter.geoms if isinstance(inter, (MultiLineString, GeometryCollection)) else [inter]
        for g in geoms:
            if isinstance(g, Point):
                pts.append((g.x, g.y))
            elif isinstance(g, LineString) and g.coords:
                pts.append(g.coords[0])
                pts.append(g.coords[-1])
            elif isinstance(g, MultiPoint):
                for p in g.geoms:
                    pts.append((p.x, p.y))

    unique = []
    seen = set()
    for pt in pts:
        r = (round(pt[0], 7), round(pt[1], 7))
        if r not in seen:
            for bdy in getattr(boundaries, 'geoms', [boundaries]):
                if bdy.distance(Point(pt)) < TOLERANCE * 10:
                    unique.append(pt)
                    seen.add(r)
                    break
    return unique


def _create_augmented_obstacle_tracks(obstacles, intersection_points):
    tracks = []
    inter_geoms = [Point(p) for p in intersection_points]
    valid_obs = [o for o in obstacles if isinstance(o, (Polygon, MultiPolygon)) and o.is_valid and not o.is_empty]

    for obs in valid_obs:
        bdy = obs.boundary
        if bdy.is_empty:
            continue
        bdy_list = [bdy] if isinstance(bdy, LineString) else list(bdy.geoms) if isinstance(bdy, MultiLineString) else []

        for bdy_ls in bdy_list:
            if not bdy_ls.coords:
                continue
            orig_coords = list(bdy_ls.coords)
            relevant = [p for p in inter_geoms if bdy_ls.distance(p) < TOLERANCE]
            combined = {Point(p) for p in orig_coords} | set(relevant)

            filtered = []
            for p in combined:
                if not any(p.distance(e) < TOLERANCE for e in filtered):
                    filtered.append(p)

            if len(filtered) < 2:
                continue

            try:
                dists = [{'pt': p, 'd': bdy_ls.project(p)} for p in filtered]
                dists.sort(key=lambda x: x['d'])
                sorted_coords = [(x['pt'].x, x['pt'].y) for x in dists]

                is_closed = Point(orig_coords[0]).distance(Point(orig_coords[-1])) < TOLERANCE
                is_aug_closed = Point(sorted_coords[0]).distance(Point(sorted_coords[-1])) < TOLERANCE
                if is_closed and not is_aug_closed:
                    sorted_coords.append(sorted_coords[0])
                elif not is_closed and is_aug_closed and len(sorted_coords) > 1:
                    sorted_coords = sorted_coords[:-1]

                final = [sorted_coords[0]]
                for p in sorted_coords[1:]:
                    if Point(p).distance(Point(final[-1])) > TOLERANCE:
                        final.append(p)
                if len(final) >= 2:
                    tracks.append(LineString(final))
            except Exception:
                continue
    return tracks


def _perpendicular_distance(point, line_start, line_end):
    px, py = point
    ax, ay = line_start
    bx, by = line_end
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    mag_sq = vx ** 2 + vy ** 2
    if mag_sq < TOLERANCE ** 2:
        return math.sqrt(wx ** 2 + wy ** 2)
    t = (wx * vx + wy * vy) / mag_sq
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * vx, ay + t * vy
    return math.sqrt((px - cx) ** 2 + (py - cy) ** 2)


def _rdp_simplify(points, epsilon):
    if len(points) < 3:
        return points
    max_dist, max_idx = 0.0, 0
    for i in range(1, len(points) - 1):
        d = _perpendicular_distance(points[i], points[0], points[-1])
        if d > max_dist:
            max_dist, max_idx = d, i
    if max_dist > epsilon:
        r1 = _rdp_simplify(points[:max_idx + 1], epsilon)
        r2 = _rdp_simplify(points[max_idx:], epsilon)
        return r1[:-1] + r2
    return [points[0], points[-1]]


def _find_bridging_path(end_coord, start_coord, obstacle_tracks, tolerance=1e-5):
    best = None
    min_len = float('inf')
    for track in obstacle_tracks:
        if not track.coords or len(track.coords) < 2:
            continue
        coords = list(track.coords)
        is_closed = Point(coords[0]).distance(Point(coords[-1])) < 1e-7
        if not is_closed and len(coords) > 2:
            continue
        n = len(coords)
        num_seg = n - 1 if is_closed else n

        def find_idx(pt):
            min_d, best_i = float('inf'), -1
            for i, c in enumerate(coords):
                d = Point(pt).distance(Point(c))
                if d < min_d:
                    min_d, best_i = d, i
            return best_i if min_d < tolerance else -1

        i1, i2 = find_idx(end_coord), find_idx(start_coord)
        if i1 == -1 or i2 == -1:
            continue

        def path_len(seg):
            return LineString(seg).length if len(seg) >= 2 else 0.0

        if i1 == i2:
            seg, length = [coords[i1]], 0.0
        elif not is_closed:
            s, e = min(i1, i2), max(i1, i2)
            seg = coords[s:e + 1]
            if i1 > i2:
                seg = seg[::-1]
            length = path_len(seg)
        else:
            fwd, curr = [], i1
            while curr != i2:
                fwd.append(curr)
                curr = (curr + 1) % num_seg
            fwd.append(i2)
            seg_fwd = [coords[j] for j in fwd]
            bwd, curr = [], i1
            while curr != i2:
                bwd.append(curr)
                curr = (curr - 1 + num_seg) % num_seg
            bwd.append(i2)
            seg_bwd = [coords[j] for j in bwd]
            lf, lb = path_len(seg_fwd), path_len(seg_bwd)
            seg, length = (seg_fwd, lf) if lf <= lb else (seg_bwd, lb)

        if length < min_len:
            min_len, best = length, seg
    return best or []


def _order_tracks(tracks, base_path):
    if not tracks or not base_path.coords:
        return tracks

    def proj_dist(t):
        if not t.coords:
            return float('inf')
        try:
            return base_path.project(Point(t.coords[0]))
        except Exception:
            return float('inf')
    return sorted(tracks, key=proj_dist)


def _stitch_segments(ordered_tracks, obstacle_tracks):
    if not ordered_tracks:
        return None
    valid = [t for t in ordered_tracks if t.is_valid and t.coords]
    if not valid:
        return None

    final_coords = list(valid[0].coords)

    for i in range(len(valid) - 1):
        end_i = valid[i].coords[-1]
        start_i1 = valid[i + 1].coords[0]

        bridge = _find_bridging_path(end_i, start_i1, obstacle_tracks)
        if not bridge:
            return LineString(final_coords) if len(final_coords) >= 2 else None

        if len(bridge) >= 3:
            try:
                simplified = LineString(bridge).simplify(0.01, preserve_topology=True)
                if simplified.is_valid and not simplified.is_empty and len(simplified.coords) >= 2:
                    bridge = list(simplified.coords)
            except Exception:
                pass

        if Point(final_coords[-1]).distance(Point(bridge[0])) < 1e-7:
            if len(bridge) > 1:
                final_coords.extend(bridge[1:])
        else:
            final_coords.extend(bridge)

        next_coords = list(valid[i + 1].coords)
        if Point(final_coords[-1]).distance(Point(next_coords[0])) < 1e-7:
            if len(next_coords) > 1:
                final_coords.extend(next_coords[1:])
        else:
            final_coords.extend(next_coords)

    dedup = [final_coords[0]]
    for p in final_coords[1:]:
        if Point(p).distance(Point(dedup[-1])) > TOLERANCE:
            dedup.append(p)

    if len(dedup) >= 3:
        try:
            dedup = _rdp_simplify(dedup, 0.05)
        except Exception:
            pass

    return LineString(dedup) if len(dedup) >= 2 else None


def run_lawnmower_with_obstacles(polygon_coords, spacing_meters, angle_deg=90,
                                 safety_meters=0, no_go_zones=None):
    """Lawnmower coverage path with optional no-go-zone avoidance. PORTED from Scout.

    Args:
        polygon_coords: [[lng, lat], ...] survey area
        spacing_meters: distance between parallel survey lines (lane spacing)
        angle_deg:      direction of the survey lines
        safety_meters:  shoreline margin (inset from the survey boundary)
        no_go_zones:    list of [[lng, lat], ...] polygons to keep the route out of
    Returns:
        [[lng, lat], ...] coverage waypoints (empty list if nothing could be generated)
    """
    coords_deg = [(c[0], c[1]) for c in polygon_coords]
    if coords_deg[0] != coords_deg[-1]:
        coords_deg.append(coords_deg[0])
    poly_deg = Polygon(coords_deg)
    if not poly_deg.is_valid:
        poly_deg = poly_deg.buffer(0)

    base_path_proj, centroid_proj, to_deg = _generate_boustrophedon_path(
        poly_deg, spacing_meters, angle_deg, safety_meters)

    if base_path_proj.is_empty or len(base_path_proj.coords) < 2:
        return []

    if not no_go_zones:
        path_deg = transform(to_deg.transform, base_path_proj)
        return [[c[0], c[1]] for c in path_deg.coords]

    centroid_geog = poly_deg.centroid
    utm_zone = int((centroid_geog.x + 180) / 6) + 1
    hemi = 6 if centroid_geog.y >= 0 else 7
    crs_proj = f"EPSG:32{hemi}{utm_zone:02d}"
    to_proj = pyproj.Transformer.from_crs("EPSG:4326", crs_proj, always_xy=True)

    obstacles_proj = []
    for zone_coords in no_go_zones:
        zc = [(c[0], c[1]) for c in zone_coords]
        if zc[0] != zc[-1]:
            zc.append(zc[0])
        z_poly = Polygon(zc)
        if not z_poly.is_valid:
            z_poly = z_poly.buffer(0)
        try:
            z_proj = transform(to_proj.transform, z_poly)
            obstacles_proj.append(z_proj)
        except Exception:
            continue

    if not obstacles_proj:
        path_deg = transform(to_deg.transform, base_path_proj)
        return [[c[0], c[1]] for c in path_deg.coords]

    intersection_pts = _find_intersection_points(base_path_proj, obstacles_proj)
    obstacle_tracks = _create_augmented_obstacle_tracks(obstacles_proj, intersection_pts)
    split_tracks = _split_path_by_obstacles(base_path_proj, obstacles_proj)

    if not split_tracks:
        return []

    ordered_tracks = _order_tracks(split_tracks, base_path_proj)
    stitched_path = _stitch_segments(ordered_tracks, obstacle_tracks)

    if not stitched_path or stitched_path.is_empty:
        path_deg = transform(to_deg.transform, base_path_proj)
        return [[c[0], c[1]] for c in path_deg.coords]

    path_deg = transform(to_deg.transform, stitched_path)
    return [[c[0], c[1]] for c in path_deg.coords]


def compute_return_path(polygon_coords, spacing_meters, safety_meters, last_waypoint,
                        home_coord, no_go_zones=None):
    """Grid A* return path from the last coverage waypoint back to the planning home,
    staying inside the (inset) survey polygon and out of no-go zones. PORTED from Scout
    (flask/app.py :: compute_return_path). Returns [[lng, lat], ...] or [] if no route.

    Note: this produces an orthogonal grid path (4-neighbour A*), not arbitrary diagonal
    connectors — it is the tested return planner, kept as its own route segment."""
    import heapq

    coords_deg = [(c[0], c[1]) for c in polygon_coords]
    if coords_deg[0] != coords_deg[-1]:
        coords_deg.append(coords_deg[0])
    poly_deg = Polygon(coords_deg)

    centroid = poly_deg.centroid
    utm_zone = int((centroid.x + 180) / 6) + 1
    hemi = 6 if centroid.y >= 0 else 7
    crs_proj = f"EPSG:32{hemi}{utm_zone:02d}"
    to_proj = pyproj.Transformer.from_crs("EPSG:4326", crs_proj, always_xy=True)
    to_deg = pyproj.Transformer.from_crs(crs_proj, "EPSG:4326", always_xy=True)
    poly_proj = transform(to_proj.transform, poly_deg)

    if safety_meters > 0:
        poly_proj = poly_proj.buffer(-safety_meters)
        if poly_proj.is_empty:
            return []

    minx, miny, maxx, maxy = poly_proj.bounds
    step = spacing_meters if spacing_meters and spacing_meters > 1e-6 else 10.0
    cols = int((maxx - minx) / step) + 1
    rows = int((maxy - miny) / step) + 1

    grid = []
    for r in range(rows):
        row = []
        y = miny + r * step
        for c in range(cols):
            x = minx + c * step
            row.append(0 if poly_proj.contains(Point(x, y)) else 1)
        grid.append(row)

    if no_go_zones:
        for zone_coords in no_go_zones:
            zone_deg = [(c[0], c[1]) for c in zone_coords]
            if zone_deg[0] != zone_deg[-1]:
                zone_deg.append(zone_deg[0])
            try:
                zone_poly = transform(to_proj.transform, Polygon(zone_deg))
                for r in range(rows):
                    y = miny + r * step
                    for c in range(cols):
                        x = minx + c * step
                        if zone_poly.contains(Point(x, y)):
                            grid[r][c] = 1
            except Exception:
                pass

    def to_grid(lng, lat):
        x, y = to_proj.transform(lng, lat)
        c = int(round((x - minx) / step))
        r = int(round((y - miny) / step))
        return r, c

    def to_coord(r, c):
        x = minx + c * step
        y = miny + r * step
        lng, lat = to_deg.transform(x, y)
        return [lng, lat]

    def is_free(r, c):
        if r < 0 or r >= rows or c < 0 or c >= cols:
            return True
        return grid[r][c] == 0

    def nearest_free_inside(r, c):
        if 0 <= r < rows and 0 <= c < cols and grid[r][c] == 0:
            return r, c
        for dist in range(1, max(rows, cols)):
            for dr in range(-dist, dist + 1):
                for dc in range(-dist, dist + 1):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < rows and 0 <= nc < cols and grid[nr][nc] == 0:
                        return nr, nc
        return r, c

    def astar(start, goal):
        h = lambda a, b: abs(a[0] - b[0]) + abs(a[1] - b[1])
        open_set = [(h(start, goal), 0, start, None)]
        came_from = {}
        gscore = {start: 0}
        closed = set()
        while open_set:
            _, g, node, parent = heapq.heappop(open_set)
            if node in closed:
                continue
            came_from[node] = parent
            if node == goal:
                path, cur = [], node
                while cur is not None:
                    path.append(cur)
                    cur = came_from[cur]
                path.reverse()
                return path
            closed.add(node)
            for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nr, nc = node[0] + dr, node[1] + dc
                if is_free(nr, nc):
                    ng = g + 1
                    if (nr, nc) not in gscore or ng < gscore[(nr, nc)]:
                        gscore[(nr, nc)] = ng
                        heapq.heappush(open_set, (ng + h((nr, nc), goal), ng, (nr, nc), node))
        return None

    start_grid = to_grid(last_waypoint[0], last_waypoint[1])
    goal_grid = to_grid(home_coord[0], home_coord[1])
    start_inside = nearest_free_inside(*start_grid)
    goal_inside = nearest_free_inside(*goal_grid)

    path = astar(start_inside, goal_inside)
    if not path:
        return []

    full_path = [start_grid] + path + [goal_grid]
    deduped = [full_path[0]]
    for p in full_path[1:]:
        if p != deduped[-1]:
            deduped.append(p)
    return [to_coord(r, c) for r, c in deduped]


# ═══════════════════════════════════════════════════════════════════════════════════════
# OPERATOR-SIDE ORCHESTRATION — segmentation, metrics, validation. Not ported; this is the
# Plan-page-specific layer built on top of the two ported generators above.
# ═══════════════════════════════════════════════════════════════════════════════════════

# The finalized survey-plan package version. Distinct from the mission-contract-v1 ROUTE
# contract (which the Pixhawk upload still uses verbatim): a package carries the richer
# operator geometry/segmentation the immutable original-mission record retains for later
# Local Agent replanning — see main.py's mission record and PART 6 of the task.
MISSION_PACKAGE_VERSION = "operator-survey-plan-v1"
ROUTE_CONTRACT_VERSION = "mission-contract-v1"

# Ordered route segment kinds. Every displayed section maps to one of these, and the flat
# Pixhawk route is exactly their concatenation — there are no implicit straight jumps between
# arrays (each gap between coverage/operator sections is an EXPLICIT connector segment, which
# is the whole point of the redesign: a segment endpoint is always the next segment's start).
SEGMENT_KINDS = (
    "start_connector",       # route start (planning home) → first approach waypoint
    "approach",              # operator-approved approach waypoints, in numbered order
    "survey_entry_connector",  # last approach WP (or start) → primary coverage entry
    "primary",               # primary coverage pass (internal lane turns kept safe)
    "pass_transition",       # primary end → secondary start (dual pass only)
    "secondary",             # secondary coverage pass
    "return_connector",      # coverage end → first return waypoint (or planning home)
    "return_approach",       # operator-approved return waypoints, in numbered order
    "final_home_connector",  # last return WP → planning home
)

# PLANNING-ONLY segment kinds. These are APPROVED transit geometry that is deliberately NOT part
# of the uploaded execution route, so they never appear in `segments`, never reach
# `_flatten_segments`, and never enter the route or its hash. They ride on the package/record
# under `planning_only_transit_segments` and exist for corridor derivation, geometric validation
# and safe-return provenance — see the EXECUTION ROUTE vs APPROVED TRANSIT GEOMETRY note in
# generate_survey.
PLANNING_ONLY_SEGMENT_KINDS = (
    # planning home → first approach waypoint, when `route_start_mode: first_approach` says the
    # EXECUTED route begins at A1. The leg is still approved planning geometry: it is what proves
    # Home is connected to the survey, and it is the corridor's anchor at the Home end.
    "home_transit_connector",
)

# WHERE THE EXECUTED MISSION ROUTE BEGINS — and nothing else.
#
#   planning_home   the uploaded route begins at the planning Home:
#                     Home → approach (if any) → survey entry → survey → return (if any) → Home
#   first_approach  the uploaded route begins at the FIRST APPROACH WAYPOINT:
#                     A1 → … → survey entry → survey → return (if any) → Home
#
# The mode chooses the EXECUTION start. It does NOT change the safety meaning of the approved
# Home/transit geometry: in BOTH modes the approved transit network runs from the planning Home
# through the approach chain to the navigable survey geometry, and back from the survey through
# the return chain to the planning Home. In `first_approach` the Home → A1 leg is approved
# PLANNING-ONLY geometry (see PLANNING_ONLY_SEGMENT_KINDS) rather than an executed leg, so
#
#     execution start != geometry provenance start
#
# and the Home corridor is derived from the approved network, not from the execution subset.
ROUTE_START_MODES = ("planning_home", "first_approach")

# Safe-connector resolution bound. A grid finer than this many cells on an axis is refused as
# an excessive-resolution generation error rather than silently blowing up runtime/memory —
# the operator increases lane spacing or shrinks the survey (see _NavGrid._build_grid).
MAX_GRID_CELLS_PER_AXIS = 400

# Metre tolerances (UTM/projected space). COVER_TOL absorbs the projection/rounding noise at
# the inset edge the generator clips exactly to; a real excursion is far larger.
COVER_TOL_M = 0.5
CONNECTOR_EPS_M = 1.0
# Degrees: two route points closer than this are the same join point (≈1 cm), used to detect
# the shared endpoint between adjacent segments so the flat route carries no duplicate.
JOIN_TOL_DEG = 1e-7

# ── WIRE PRECISION: why coverage is not built flush against the navigable edge ────────────
# Route waypoints AND geometry rings both go on the wire rounded to 7 decimal places
# (mission_contract.route_content_hash, _route_waypoints, the `[[lng, lat], ...]` ring
# emitters). At Nordic latitudes 1e-7° is ~1.11 cm of latitude and ~0.61 cm of longitude, so
# ONE rounding can displace a point by up to ~0.64 cm — and the route and the polygon are
# rounded INDEPENDENTLY, so the two can move ~1.3 cm relative to each other.
#
# That matters because `_coverage_fragments` clips each lane against the navigable polygon
# itself: a lane's endpoints land EXACTLY on the navigable boundary, and the cross-lane
# transition joining two such endpoints therefore runs ALONG that boundary. Serialize it and
# the leg lands a few millimetres OUTSIDE the polygon it was clipped from. COVER_TOL_M hides
# that from the operator; Scout, which must prove every approved leg is retraceable and does
# so with exact containment, rejects the package with ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY.
#
# WIRE_MARGIN_M is the margin coverage geometry is built INSIDE the navigable region so it
# survives that round trip. It is deliberately an order of magnitude below any clearance an
# operator can configure (a 1 m shoreline clearance is 20x this), and it only ever moves
# coverage FURTHER from the shore and the exclusion — it can never open a violation. It is a
# representation margin, not a safety tolerance: the safety regions themselves are untouched.
WIRE_MARGIN_M = 0.05
# How far coverage LANES are clipped inside the approved region — one wire margin further in
# than WIRE_MARGIN_M, so a lane end is STRICTLY interior to the region every transition leaving
# it is proven against (a leg collinear with the region edge cannot be certified: a collinear
# line's `difference` is the whole line, not an empty set). The measurable cost is this much
# trimmed off each end of each coverage fragment, ~0.1% of the surveyed line length.
COVERAGE_EDGE_INSET_M = 2.0 * WIRE_MARGIN_M

# Route-cleanup tunables (projected metres / degrees). Deliberately conservative: a point the
# cleanup drops lies — within these bounds — on the straight leg the Pixhawk already flies
# between its two kept neighbours, so removing it cannot move the executed corridor. This is
# route CLEANUP (fewer mission items on straight runs), NOT trajectory smoothing — the vehicle
# still flies straight segments between items. See PART 5/PART 13 of the route-quality task.
CLEANUP_MIN_SPACING_M = 1.0    # consecutive points closer than this collapse to one
CLEANUP_COLLINEAR_DEG = 2.0    # a middle point whose turn is under this is redundant
# An "immediate backtrack" (the task's A→B→A artifact) is a near-reversal that RETURNS to
# essentially where it came from — the route point after the reversal lands within
# BACKTRACK_RETURN_M of the point before it. This deliberately does NOT count a legitimate
# coverage U-turn between lanes, whose return point is offset by the (far larger) lane spacing.
BACKTRACK_ANGLE_DEG = 160.0
BACKTRACK_RETURN_M = 3.0

# ── SURVEY FRAME (sonar coverage quality) ────────────────────────────────────────────────
# The SURVEY FRAME is the two axes of the operator's chosen survey angle:
#     U  parallel to survey_angle       — the sonar lane direction
#     V  perpendicular to survey_angle  — the cross-lane (lane-spacing) direction
# "Orthogonal" anywhere in this module means orthogonal IN THAT FRAME. It never means
# geographic north/east: at survey_angle 42° the coverage legs are ≈42° and the cross-lane
# transitions ≈132°, and nothing is snapped to the projection's grid axes.
#
# A generated leg counts as survey-aligned when its projected bearing sits within this tolerance
# of U or V, modulo the 180° direction reversal (a lane flown either way is the same axis). Used
# for the diagnostics and for deciding whether a DIRECT transition is already aligned — never as
# a substitute for the geometric safety checks, which are unchanged.
SURVEY_ALIGN_TOL_DEG = 5.0
# Legs shorter than this carry no meaningful heading (a sub-metre stitch leg is not an
# "arbitrary-angle coverage leg"), so the alignment classifier ignores them. Tied to the existing
# cleanup spacing rather than invented.
ALIGN_MIN_LEG_M = CLEANUP_MIN_SPACING_M
# MINIMUM USEFUL COVERAGE FRAGMENT. A buffered no-go exclusion clipping a nominal lane can leave
# a 1–2 m sliver at a corner: two extra turns for a fragment that carries almost no sonar swath.
# The threshold is derived from EXISTING semantics, not invented — a quarter of the operator's
# own lane spacing, and never below the cleanup's near-duplicate spacing:
#     min_useful = max(CLEANUP_MIN_SPACING_M, 0.25 · lane_spacing_m)
# The unexamined water a dropped fragment can leave is therefore under a quarter of one lane
# cell, and every drop is COUNTED and REPORTED (route_quality.skipped_short_fragment_count /
# _length_m, plus a generation warning) rather than silently swallowed.
MIN_FRAGMENT_LANE_FRACTION = 0.25
# How far OUTSIDE a no-go exclusion's survey-frame extent a bypass staircase is offset. It only
# has to beat the containment/no-go tolerances — the candidate is still proven geometrically.
BYPASS_MARGIN_M = max(2.0 * COVER_TOL_M, 1.0)
# Bounded candidate generation (the planner stays lightweight and deterministic — no visibility
# graph, no general search): at most this many exclusion bodies contribute bypass candidates to
# one transition, nearest first.
MAX_BYPASS_BODIES = 2

# ── F4: bounded shortest-safe inter-fragment transit ──────────────────────────────────────
# When the straight A→B is NOT safe, a short detour around the one thing blocking it is usually
# far shorter than the survey-frame L or staircase that currently replaces it (measured on the
# operator's own large polygon: 110.1→78.2 m, 160.6→114.5 m, 162.6→116.3 m). The detour is found
# with a LOCAL visibility graph, and every one of these constants exists to keep that search
# bounded — this is deliberately not a general path planner.
#
# LOCAL_MARGIN: how far outside the A/B bounding box a corner may sit and still be considered.
# A transit that needs to reach further than this around an obstacle is not a "short detour"; it
# is exactly the case the existing bypass/A* tiers already handle.
F4_LOCAL_MARGIN_M = 15.0
# How far inside the buildable region the candidate corners are placed. Strictly greater than
# COVER_TOL_M so a corner is clear of the tolerance band `_seg_covered` / `_seg_clears_nogo`
# measure against, and therefore cannot itself be the reason a leg is rejected.
F4_VERTEX_INSET_M = COVER_TOL_M + 0.1
# The corner ring is simplified before use. A buffered no-go is a ROUND polygon with dozens of
# near-collinear arc vertices; without this they would fill the candidate budget with points that
# describe the same corner. Douglas-Peucker at this tolerance keeps the genuine corners.
F4_VERTEX_SIMPLIFY_M = 0.5
# Hard cap on the local candidate set, so the graph is O(1) in the size of the survey area.
# 24 corners => at most 26 nodes => at most 325 undirected pair probes per transition, all
# memoised through the same `_seg_safe_cached` the rest of generation uses.
F4_MAX_LOCAL_VERTICES = 24
# The minimum saving that justifies replacing a proven aligned candidate. Small, but non-zero:
# a near-tie must not churn the route hash for a few centimetres.
F4_MIN_GAIN_M = 0.5

# ── F5: BCD CELL-ORDER AND CELL-ORIENTATION OPTIMISATION ─────────────────────────────────
# F2/F4 draw the SHORTEST SAFE line between two coverage fragments. They cannot fix the choice
# of WHICH fragment comes next — and on the operator's own large polygon that choice, not the
# drawing, was what remained: the cells were covered in a fixed geometric key order
# (east region → the column south of the obstacle → the column north of it → west region), so
# the route crossed the whole survey twice and the survey ENTRY connector ran 196.7 m from the
# approach to a cell on the far side. This tier chooses the cell ORDER and each cell's
# ORIENTATION above F2/F4; the transitions themselves are still drawn by F2/F4, unchanged.
#
# COVERAGE IS NOT TOUCHED. The lane family, the clip, the cells and their membership are inputs
# here. A cell is still covered by the same lane-monotonic alternating boustrophedon — the
# optimiser only picks which of the four legal traversals of that same pattern is flown, and in
# what order the cells are taken. See _cell_traversal_states.
#
# EXACT UP TO THIS MANY CELLS. Cell count is a property of the sweep TOPOLOGY, not of lane
# spacing: the eight regression geometries produce 1–7 cells, and the operator's large polygon
# produces 4 at every spacing from 5 m down to 1 m. Below the threshold the search is an exact
# Held-Karp DP over (visited set, current cell, current orientation) and the result is globally
# optimal for the cost below; above it a deterministic topology-aware heuristic runs instead, so
# an unusually fragmented survey degrades gracefully rather than hanging. 9 cells is
# 2^9·9·4·9·4 ≈ 663k DP steps — still a fraction of a second — while 12 would be ~9.4M.
BCD_EXACT_MAX_CELLS = 9
# Above the exact threshold: how many extra non-adjacent cells (nearest by centroid) join each
# cell's candidate successor set, so the heuristic keeps a bounded, deterministic branching
# factor instead of considering every remaining cell.
BCD_HEURISTIC_EXTRA_NEIGHBOURS = 3
# Bounded deterministic local improvement (segment-reversal / re-orientation sweeps) after the
# heuristic construction. Bounded so the optimiser can never become the slow part.
BCD_LOCAL_IMPROVEMENT_PASSES = 4
# ── the cost terms ───────────────────────────────────────────────────────────────────────
# DISTANCE IS THE OBJECTIVE. Everything below is a small deterministic nudge expressed in
# metres, scaled by the operator's own lane spacing so it means the same thing at every scale,
# and deliberately far smaller than the distances being compared (a lane is tens to hundreds of
# metres, these are one or two lane spacings).
#
# TOPOLOGY. Two cells are adjacent when they meet across a sweep row in the decomposition — a
# split or a merge (see _bcd_adjacency). A hand-over between adjacent cells is a lane-to-lane
# hop; one between unrelated cells crosses ground the survey has already covered. Distance
# usually says this by itself; the penalty is what breaks the case where it does not, and it is
# NOT large enough to buy back a genuinely longer route.
BCD_TOPOLOGY_PENALTY_LANES = 2.0
# REVERSAL. Leaving a cell on a heading that points away from the next one means turning round
# and running back along the lane just surveyed. Ranked, never forbidden.
BCD_REVERSAL_PENALTY_LANES = 1.0
# FALLBACK IS A CONTRACT TERM, NOT A PREFERENCE. A hand-over that drops to the generic grid A*
# emits arbitrary-angle geometry, which the survey-frame alignment contract counts as a defect
# (route_quality.fallback_connector_count is asserted at zero). The penalty is large enough to
# be lexicographic in practice — no realistic distance saving can buy one — while keeping the
# DP a plain additive minimisation.
BCD_FALLBACK_PENALTY_M = 1.0e5

# Provenance for the finalized package (PART 11): names the exact, reproducible algorithm at
# each pipeline stage so the thesis run is explainable. Not a version the upload contract reads.
GENERATION_ALGORITHM = {
    # The ported boustrophedon lane family (count, spacing, sweep order, angle), clipped to the
    # approved region in the SURVEY FRAME so an exclusion removes lane length instead of bending
    # the lane around itself. See the SURVEY-FRAME COVERAGE GENERATION banner.
    "coverage": "survey-frame-boustrophedon-v1",
    "coverage_lane_family": "ported-scout-boustrophedon-v1",
    # Fragments are grouped into cells at the sweep's critical points and each cell is covered
    # completely, lane-monotonic and alternating, before the next (see _bcd_cells). This replaced
    # a strict global (row, along_index) order, which forced a trip around the exclusion and back
    # on every lane an exclusion split.
    # Cells are grouped by _bcd_cells (unchanged), then the ORDER they are covered in and the
    # ORIENTATION each is covered with are chosen by an exact/topology-aware search over the
    # real safe transition distances between them (see _bcd_cell_plan). Coverage geometry is an
    # input to that search and is never modified by it.
    "fragment_ordering": "bcd-cell-order-orientation-optimised-v2",
    # Inter-fragment transits try the straight leg FIRST at any heading; when it is unsafe, a
    # BOUNDED LOCAL detour is searched and taken only if it beats the aligned candidate. Both are
    # accepted solely by the same authoritative safety predicate, and the unchanged survey-frame
    # orthogonal/bypass/A* hierarchy remains underneath (see _aligned_transition tiers 0 and 0b).
    # Coverage lanes themselves remain strictly survey-frame aligned.
    "coverage_transitions": "shortest-safe-local-direct-first-survey-frame-orthogonal-v3",
    "safe_connector": "bounded-grid-a-star-v1",
    "connector_simplification": "safe-line-of-sight-v1",
    "cleanup": "semantic-path-cleanup-v1",
}

# Per-segment-kind cleanup policy (PART 6). Aggressive kinds are generated connectors with no
# interior semantic points, so a full safety-checked line-of-sight pass applies. Moderate kinds
# carry operator waypoints that are preserved as anchors; only the generated points between them
# are compressed. Coverage kinds get conservative cleanup only (dedup + provably-collinear), so
# lane spacing / endpoints / fragment boundaries are never shortcut across.
_AGGRESSIVE_KINDS = frozenset({"start_connector", "home_transit_connector",
                               "survey_entry_connector", "pass_transition",
                               "return_connector", "final_home_connector"})
_MODERATE_KINDS = frozenset({"approach", "return_approach"})
_CONSERVATIVE_KINDS = frozenset({"primary", "secondary"})
# Kinds whose cleanup safety predicate is full navigable containment (require_inside=True); the
# rest (home/approach/return legs that legitimately run near-shore) are no-go-clearance only —
# this exactly mirrors the connector policy in _NavGrid.segment_is_safe.
_REQUIRE_INSIDE_KINDS = frozenset({"survey_entry_connector", "pass_transition",
                                   "return_connector", "primary", "secondary"})


class ConnectorError(ValueError):
    """No safe connector could be found between two approved route points inside the
    navigable region. Raised rather than emitting an invalid straight connector that leaves
    the shoreline-offset area or crosses a no-go interior. main.py maps it to a 400 with the
    specific reason, exactly like other planning-input errors."""


class DisconnectedNavigableError(ValueError):
    """The navigable region splits into more than one connected component after applying the
    shoreline clearance and no-go zones. Survey generation currently requires ONE connected
    navigable region (task PART 6); generation is blocked with a clear message rather than
    silently drawing connectors across excluded water."""


def _close(a, b, tol=JOIN_TOL_DEG):
    """True when two [lng,lat] points are the same join point (within ~1 cm)."""
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _buffer_exclusion(nogo_union, no_go_clearance_m):
    """The forbidden no-go EXCLUSION geometry: the drawn zone union expanded outward by the
    operator's no-go clearance. `None` in → `None` out (no zones is a real answer, not an
    empty polygon). A clearance of 0 returns the original union unchanged, so "no clearance"
    means exactly the geometry the operator drew.

    Round joins (shapely's default) on purpose — see _NavGrid's docstring: the round buffer is
    the set of points within N metres of the polygon, which is the minimum-distance semantic
    the parameter promises. A mitre join would silently push corners out by N·√2."""
    if nogo_union is None:
        return None
    if not no_go_clearance_m or no_go_clearance_m <= TOLERANCE:
        return nogo_union
    try:
        buffered = nogo_union.buffer(abs(no_go_clearance_m))
    except Exception:
        return nogo_union
    if buffered.is_empty or not buffered.is_valid:
        buffered = buffered.buffer(0) if not buffered.is_empty else nogo_union
    return buffered if not buffered.is_empty else nogo_union


class _NavGrid:
    """The shared navigable model + safe point-to-point connector for one generation.

    Built ONCE per generate_survey call so every connector (coverage lane turns, no-go-split
    sections, pass transition, survey-entry/return connectors) is judged against, and routed
    inside, the identical navigable geometry. The navigable region is the survey boundary
    inset by the shoreline clearance MINUS the no-go EXCLUSION (the drawn zones expanded by the
    no-go clearance) — the exact space a coverage/inter-coverage connector must stay within.

    ONE strategy, deterministic: a direct segment is accepted only when it is covered by the
    navigable polygon (within COVER_TOL_M) and clears no-go interiors; otherwise a bounded
    4-neighbour grid A* (adapted from the ported compute_return_path) finds an orthogonal
    safe path. No diagonal shortcuts, no second planner.

    NO-GO CLEARANCE. `no_go_clearance` is the minimum routing distance the operator requires
    between generated geometry and a drawn no-go polygon. It is applied HERE, once, as an
    OUTWARD buffer of the zone union (`nogo_original` → `nogo`), so every consumer of this
    grid — coverage repair, every connector kind, the LOS compression safety predicate, the
    fleet survey-line clip, validation — judges against the same exclusion. Round joins are
    deliberate: a round outward buffer is exactly the set of points within `no_go_clearance`
    metres of the polygon, which is exactly the semantic "stay at least N m away".

    The ORIGINAL polygon is kept (`nogo_original`) and is what the Plan/Map draw and what the
    mission record stores. The buffered exclusion is derived routing geometry and never
    replaces it."""

    def __init__(self, boundary, clearance, zones, step_m, no_go_clearance=0.0):
        self.to_proj, self.to_deg = _utm_for(boundary)
        poly_deg = Polygon([(c[0], c[1]) for c in boundary])
        if not poly_deg.is_valid:
            poly_deg = poly_deg.buffer(0)
        inset = transform(self.to_proj.transform, poly_deg)
        if clearance and clearance > TOLERANCE:
            inset = inset.buffer(-abs(clearance), join_style=2)
        self.inset = inset  # projected; may be empty

        zpolys = []
        for z in zones or []:
            zp = Polygon([(c[0], c[1]) for c in z])
            if not zp.is_valid:
                zp = zp.buffer(0)
            try:
                zpolys.append(transform(self.to_proj.transform, zp))
            except Exception:
                continue
        # The operator's drawn zones, unmodified — provenance, never routed against directly.
        self.nogo_original = unary_union(zpolys) if zpolys else None
        self.no_go_clearance_m = float(abs(no_go_clearance or 0.0))
        self.nogo = _buffer_exclusion(self.nogo_original, self.no_go_clearance_m)
        self.buffer_valid = self.nogo is None or (self.nogo.is_valid and not self.nogo.is_empty)

        nav = inset
        if self.nogo is not None and not nav.is_empty:
            nav = nav.difference(self.nogo)
        self.navigable = nav

        # THE REGION GENERATION BUILDS IN. `navigable` is the approved region and is what the
        # mission ships and what validation proves against; `buildable` is that same region
        # pulled in by WIRE_MARGIN_M, and it is what every `require_inside=True` decision here
        # is made against. The two are separated because geometry built FLUSH against the
        # approved edge does not survive the 7-decimal wire round trip — see WIRE_MARGIN_M.
        # This only ever makes generation more conservative: `buildable ⊂ navigable`, so a leg
        # this grid calls safe is inside the approved region with room to spare, and no
        # clearance the operator configured is reinterpreted.
        buildable = nav
        if not nav.is_empty:
            shrunk = nav.buffer(-WIRE_MARGIN_M)
            if not shrunk.is_empty:
                buildable = shrunk
        self.buildable = buildable

        # WHERE COVERAGE LANES ARE CLIPPED — see COVERAGE_EDGE_INSET_M. Strictly inside
        # `buildable`, so the survey-frame transition joining two lane ends is certifiable and
        # stays aligned instead of losing to the generic A* fallback.
        coverage = buildable
        if not nav.is_empty:
            shrunk = nav.buffer(-COVERAGE_EDGE_INSET_M)
            if not shrunk.is_empty:
                coverage = shrunk
        self.coverage = coverage

        # Connected components with real area (a sliver from a buffer artefact is ignored).
        if isinstance(nav, MultiPolygon):
            comps = [g for g in nav.geoms if g.area > 1.0]
        elif nav.is_empty:
            comps = []
        else:
            comps = [nav]
        self.components = comps

        self.step = step_m if step_m and step_m > 1e-6 else 10.0
        self._grid = None
        self._bounds = None

        # Safe-connector caching + route-quality instrumentation (PART 7/PART 12). The cache
        # memoises the safety predicate by normalized endpoint pair so the O(n²) line-of-sight
        # compression re-checks nothing twice within one generation. The counters record raw
        # (grid) vs simplified connector waypoints/length so improvements are measurable.
        self._safe_cache = {}
        self.raw_connector_pts = 0
        self.final_connector_pts = 0
        self.connector_len_before_m = 0.0
        self.connector_len_after_m = 0.0
        self.astar_connector_count = 0
        # F4 (bounded shortest-safe transit) instrumentation. Counted on the grid for the same
        # reason the connector counters are: one generation, one place, and the route-quality
        # layer reads them at the end rather than threading a return value through every tier.
        self.shortest_safe_count = 0
        self.shortest_safe_len_m = 0.0
        self.shortest_safe_saved_m = 0.0
        self.f4_candidate_vertices = 0     # summed local candidates offered, for the perf budget
        self.f4_edge_probes = 0            # summed safety probes the local graph asked for
        self.f4_invocations = 0
        self._f4_corners_proj = None       # lazily built once per generation

    @property
    def empty(self):
        return self.navigable is None or self.navigable.is_empty

    @property
    def disconnected(self):
        return len(self.components) > 1

    def exclusion_rings_deg(self):
        """The BUFFERED no-go exclusion as `[[lng, lat], ...]` rings — the forbidden geometry
        handed to the ported lawnmower/return generators (which take zones in degrees) and
        offered to the Plan page as an optional dashed overlay. Derived routing geometry: the
        operator's own rings stay untouched in `planning_inputs.no_go_zones`."""
        if self.nogo is None or self.nogo.is_empty:
            return []
        polys = self.nogo.geoms if isinstance(self.nogo, MultiPolygon) else [self.nogo]
        rings = []
        for poly in polys:
            if poly.is_empty or not hasattr(poly, "exterior"):
                continue
            deg = transform(self.to_deg.transform, poly)
            rings.append([[round(x, 7), round(y, 7)] for x, y in deg.exterior.coords])
        return rings

    def point_clears_nogo(self, pt_deg):
        """True when a [lng, lat] point lies outside the buffered no-go exclusion. Shrunk by
        COVER_TOL_M so a point routed exactly ALONG the exclusion edge (which the avoidance
        generator legitimately produces) is not reported as inside it."""
        if self.nogo is None or self.nogo.is_empty:
            return True
        try:
            probe = self.nogo.buffer(-COVER_TOL_M)
            if probe.is_empty:
                return True
            return not probe.contains(Point(*self.to_proj.transform(pt_deg[0], pt_deg[1])))
        except Exception:
            return True

    def _build_grid(self):
        if self._grid is not None:
            return
        minx, miny, maxx, maxy = self.navigable.bounds
        span_x, span_y = maxx - minx, maxy - miny
        step = self.step
        cols = int(span_x / step) + 1
        rows = int(span_y / step) + 1
        if cols > MAX_GRID_CELLS_PER_AXIS or rows > MAX_GRID_CELLS_PER_AXIS:
            raise ConnectorError(
                "A safe connector could not be computed at the required resolution — the "
                "navigable area is too large for the chosen lane spacing. Increase the lane "
                "spacing or reduce the survey area.")
        # A cell is free (0) when its centre is inside the region generation may build in
        # (already excludes no-go and shore). This is `buildable`, NOT `navigable.buffer(+tol)`:
        # a free cell whose centre sat half a metre outside the approved region put raw A*
        # vertices there, and LOS compression keeps a vertex it cannot shortcut past — which is
        # how a connector came to hold a point 30 cm outside the geometry it ships with.
        nav = self.buildable
        grid = []
        for r in range(rows):
            y = miny + r * step
            row = [0 if nav.contains(Point(minx + c * step, y)) else 1 for c in range(cols)]
            grid.append(row)
        self._grid = grid
        self._bounds = (minx, miny, maxx, maxy, cols, rows)

    def _seg_covered(self, line_proj):
        """True when a projected segment stays inside the region generation may build in.

        Held to `buildable` (navigable pulled in by WIRE_MARGIN_M) with NO length slack. The
        old `navigable.buffer(+COVER_TOL_M)` with a CONNECTOR_EPS_M slack accepted a leg up to
        half a metre outside the approved region and up to a metre of it outside altogether;
        the operator never saw it, and Scout — which proves containment exactly — rejected the
        finished package. A candidate this rejects is not repaired here: it simply loses to the
        next candidate, or falls through to the A* connector, or fails closed."""
        outside = line_proj.difference(self.buildable)
        return outside.is_empty or outside.length < TOLERANCE

    def _seg_clears_nogo(self, line_proj):
        """True when a projected SEGMENT clears the buffered no-go exclusion (`self.nogo`,
        already expanded by the operator's no-go clearance). This is what makes the clearance a
        segment guarantee and not just a waypoint filter: two individually-clear endpoints whose
        straight leg cuts the corner of the exclusion fail here."""
        if self.nogo is None:
            return True
        try:
            return line_proj.intersection(self.nogo.buffer(-COVER_TOL_M)).length < CONNECTOR_EPS_M
        except Exception:
            return True

    def segment_is_safe(self, a_deg, b_deg, require_inside=True):
        """Is the straight segment a→b (both [lng,lat]) an acceptable connector?

        require_inside=True (coverage-internal turns, pass transition, survey-entry / return
        connectors): the whole segment must lie inside the navigable region — which already
        excludes no-go. require_inside=False (operator approach/return legs, home connectors,
        which legitimately run to/from shore outside the inset): only the no-go interiors
        must be cleared, never assuming a manually drawn line is safe."""
        try:
            line = transform(self.to_proj.transform, LineString([a_deg, b_deg]))
        except Exception:
            return False
        if require_inside:
            return self._seg_covered(line)
        return self._seg_clears_nogo(line)

    def _seg_safe_cached(self, a_deg, b_deg, require_inside):
        """segment_is_safe memoised by normalized endpoint pair (PART 12) — the compression /
        cleanup passes probe the same candidate shortcuts repeatedly, so caching keeps the
        whole route-quality layer inside the bounded planning budget."""
        key = (_rk(a_deg), _rk(b_deg), require_inside)
        v = self._safe_cache.get(key)
        if v is None:
            v = self.segment_is_safe(a_deg, b_deg, require_inside=require_inside)
            self._safe_cache[key] = v
        return v

    def _proj_len_m(self, coords_deg):
        """Projected (metric) length of a [[lng,lat],...] path — for connector before/after
        length metrics, using the same UTM projection as every other metre in this grid."""
        if len(coords_deg) < 2:
            return 0.0
        pts = [self.to_proj.transform(c[0], c[1]) for c in coords_deg]
        return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                   for i in range(len(pts) - 1))

    def _compress_los(self, coords_deg, require_inside):
        """Safety-checked line-of-sight compression of an already-safe path (PART 4).

        Keeps P0; from the current kept point Pi it takes the FURTHEST later Pj whose direct
        segment Pi→Pj is safe under the SAME predicate the connector was routed with; keeps Pj;
        repeats to Pn. Because every retained hop is re-verified with segment_is_safe, the safe
        corridor is never widened — a raw 4-neighbour grid staircase collapses to its genuine
        turn points (measured 33→3 on the notch fixture) without any generic RDP trust."""
        coords = _dedup(coords_deg)
        if len(coords) <= 2:
            return coords
        out = [list(coords[0])]
        i, n = 0, len(coords)
        while i < n - 1:
            j = i + 1
            for cand in range(n - 1, i + 1, -1):  # furthest reachable first
                if self._seg_safe_cached(coords[i], coords[cand], require_inside):
                    j = cand
                    break
            out.append(list(coords[j]))
            i = j
        return out

    def _merge_near_dups(self, coords, protected, min_spacing_m=CLEANUP_MIN_SPACING_M):
        """Drop a non-protected point closer than min_spacing_m to the previous kept point."""
        if len(coords) <= 2:
            return coords
        proj = [self.to_proj.transform(c[0], c[1]) for c in coords]
        keep = [0]
        for k in range(1, len(coords)):
            if k == len(coords) - 1 or _rk(coords[k]) in protected:
                keep.append(k)
                continue
            px, py = proj[keep[-1]]
            qx, qy = proj[k]
            if math.hypot(qx - px, qy - py) < min_spacing_m:
                continue  # near-duplicate of the last kept point
            keep.append(k)
        return [coords[k] for k in keep]

    def _drop_collinear(self, coords, protected, require_inside, tol_deg=CLEANUP_COLLINEAR_DEG):
        """Remove a middle point whose turn is under tol_deg AND whose bypass segment is safe.
        Provably geometry-preserving: an under-tol turn means the point already sits (within
        tol) on the straight leg between its neighbours, and the bypass is re-checked safe."""
        if len(coords) <= 2:
            return coords
        proj = [self.to_proj.transform(c[0], c[1]) for c in coords]
        keep = [0]
        for m in range(1, len(coords) - 1):
            if _rk(coords[m]) in protected:
                keep.append(m)
                continue
            prev = keep[-1]
            ang = _turn_angle_deg(proj[prev], proj[m], proj[m + 1])
            if ang <= tol_deg and self._seg_safe_cached(coords[prev], coords[m + 1], require_inside):
                continue  # collinear middle point — the direct bypass is safe
            keep.append(m)
        keep.append(len(coords) - 1)
        return [coords[k] for k in keep]

    def _compress_los_anchored(self, coords, protected, require_inside):
        """Line-of-sight compression that never shortcuts PAST a protected point (operator
        approach/return waypoint) — it compresses each generated run between anchors only."""
        if len(coords) <= 2:
            return coords
        out = [list(coords[0])]
        i, n = 0, len(coords)
        while i < n - 1:
            upper = n - 1
            for k in range(i + 1, n):  # nearest protected index strictly after i, else the end
                if _rk(coords[k]) in protected:
                    upper = k
                    break
            j = i + 1
            for cand in range(upper, i + 1, -1):
                if self._seg_safe_cached(coords[i], coords[cand], require_inside):
                    j = cand
                    break
            out.append(list(coords[j]))
            i = j
        return out

    def clean_path(self, coords_deg, require_inside, anchors=None, aggressive=False):
        """One shared, deterministic cleanup for a generated segment (PART 5/PART 6).

        Order: exact-dedup → near-duplicate merge → safety-checked collinear removal → (only
        for aggressive connector kinds) line-of-sight compression → collinear removal again.
        The first and last point and every anchor (operator approach/return waypoint) are
        ALWAYS preserved; nothing is smoothed through unapproved water — every shortcut and
        bypass is re-verified with the same safety predicate the segment was routed under."""
        coords = _dedup(coords_deg)
        if len(coords) <= 2:
            return coords
        protected = {_rk(coords[0]), _rk(coords[-1])}
        for a in anchors or []:
            protected.add(_rk(a))
        coords = self._merge_near_dups(coords, protected)
        coords = self._drop_collinear(coords, protected, require_inside)
        if aggressive:
            coords = self._compress_los_anchored(coords, protected, require_inside)
            coords = self._drop_collinear(coords, protected, require_inside)
        return _dedup(coords)

    def safe_connector(self, a_deg, b_deg, require_inside=True):
        """A safe [[lng,lat],...] path from a to b. The direct segment when it is safe;
        otherwise a bounded grid A* path inside the navigable region, then line-of-sight
        simplified (PART 4). Raises ConnectorError when neither the direct segment nor any
        bounded safe path exists."""
        import heapq
        if self.segment_is_safe(a_deg, b_deg, require_inside=require_inside):
            return [list(a_deg), list(b_deg)]

        self._build_grid()
        minx, miny, maxx, maxy, cols, rows = self._bounds
        grid, step = self._grid, self.step

        def to_grid(pt):
            x, y = self.to_proj.transform(pt[0], pt[1])
            return (int(round((y - miny) / step)), int(round((x - minx) / step)))

        def to_coord(rc):
            r, c = rc
            lng, lat = self.to_deg.transform(minx + c * step, miny + r * step)
            return [lng, lat]

        def is_free(r, c):
            if r < 0 or r >= rows or c < 0 or c >= cols:
                return False
            return grid[r][c] == 0

        def stitch_clears(anchor_deg, rc):
            """Does the snap leg anchor→cell clear the no-go exclusion?

            The route's real endpoint is a planning coordinate, not a grid cell: `to_grid`
            rounds it to a cell centre up to half a diagonal (step·√2/2) away, and that stitch
            leg is part of the emitted connector even though the A* never routed it. Snapping to
            the merely-nearest free cell can therefore cut an exclusion corner the A* carefully
            went around — and with a no-go clearance in play the endpoint routinely sits exactly
            ON the exclusion edge, which is precisely where it happens.

            Deliberately the NO-GO predicate, not the full `require_inside` one: several
            connector kinds legitimately begin at an operator waypoint OUTSIDE the inset
            (approach/return/home legs, the survey-entry connector), so demanding containment of
            the snap leg would refuse routes that have always been valid. The no-go exclusion is
            the constraint that must hold for every leg regardless."""
            try:
                return self._seg_clears_nogo(transform(
                    self.to_proj.transform, LineString([anchor_deg, to_coord(rc)])))
            except Exception:
                return False

        def nearest_free(r, c, anchor_deg):
            if is_free(r, c) and stitch_clears(anchor_deg, (r, c)):
                return (r, c)
            for dist in range(1, max(rows, cols)):
                for dr in range(-dist, dist + 1):
                    for dc in range(-dist, dist + 1):
                        rc = (r + dr, c + dc)
                        if is_free(*rc) and stitch_clears(anchor_deg, rc):
                            return rc
            return None

        start = nearest_free(*to_grid(a_deg), a_deg)
        goal = nearest_free(*to_grid(b_deg), b_deg)
        if start is None or goal is None:
            raise ConnectorError(
                "No safe connector exists between two approved route points — the navigable "
                "region does not reach one of them.")

        def h(a, b):
            return abs(a[0] - b[0]) + abs(a[1] - b[1])

        open_set = [(h(start, goal), 0, start, None)]
        came = {}
        gscore = {start: 0}
        closed = set()
        found = False
        while open_set:
            _, g, node, parent = heapq.heappop(open_set)
            if node in closed:
                continue
            came[node] = parent
            if node == goal:
                found = True
                break
            closed.add(node)
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                nr, nc = node[0] + dr, node[1] + dc
                if is_free(nr, nc):
                    ng = g + 1
                    if (nr, nc) not in gscore or ng < gscore[(nr, nc)]:
                        gscore[(nr, nc)] = ng
                        heapq.heappush(open_set, (ng + h((nr, nc), goal), ng, (nr, nc), node))
        if not found:
            raise ConnectorError(
                "No safe connector could be routed between two approved route points inside "
                "the navigable region.")

        cells, cur = [], goal
        while cur is not None:
            cells.append(cur)
            cur = came[cur]
        cells.reverse()

        path = _dedup([list(a_deg)] + [to_coord(rc) for rc in cells] + [list(b_deg)])
        # PART 4: compress the raw grid staircase to its turn points, each shortcut re-verified
        # safe. Instrument raw-vs-simplified so the route-quality metrics can report the gain.
        simplified = self._compress_los(path, require_inside)
        # FINAL NO-GO GUARANTEE: every emitted hop is CHECKED against the no-go exclusion, not
        # assumed clear. _compress_los falls back to the adjacent point when no longer shortcut
        # is safe, and that fallback is the one step it does not verify — so a leg clipping the
        # exclusion could otherwise leave here silently. This module's contract is to RAISE
        # rather than emit an invalid connector. (Containment keeps its existing tolerance
        # semantics; only the no-go constraint is tightened here.)
        for u, w in zip(simplified, simplified[1:]):
            try:
                clear = self._seg_clears_nogo(
                    transform(self.to_proj.transform, LineString([u, w])))
            except Exception:
                clear = True
            if not clear:
                raise ConnectorError(
                    "No safe connector could be routed between two approved route points — "
                    "every path found crosses the no-go exclusion (the no-go zones plus the "
                    "no-go clearance).")
        self.astar_connector_count += 1
        self.raw_connector_pts += len(path)
        self.final_connector_pts += len(simplified)
        self.connector_len_before_m += self._proj_len_m(path)
        self.connector_len_after_m += self._proj_len_m(simplified)
        return simplified

    # ── F4: bounded shortest-safe transit ────────────────────────────────────────────────
    def _f4_corners(self):
        """The corner set the local detour search may route through, in PROJECTED metres.

        Built ONCE per generation, from `buildable` shrunk by F4_VERTEX_INSET_M. That single
        shrink yields exactly the two families of corners a detour ever needs, and yields them
        already PROVEN to be strictly inside the region every leg is judged against:

          * the OUTER ring pulled inward  — the corners of a concave shoreline;
          * the INNER rings pushed outward — the corners of the buffered no-go exclusion,
            because `buildable` carries the exclusion as a hole.

        Deriving both from the same shrink is what makes the offset trustworthy: there is no
        centroid-direction heuristic that a concave vertex could defeat, and no candidate can be
        rejected later for being marginally outside. The ring is simplified first — a buffered
        no-go is round, and its arc vertices would otherwise fill the whole candidate budget
        while describing a single corner."""
        if self._f4_corners_proj is not None:
            return self._f4_corners_proj
        pts = []
        region = self.buildable
        if region is not None and not region.is_empty:
            try:
                shrunk = region.buffer(-F4_VERTEX_INSET_M)
                if not shrunk.is_empty:
                    shrunk = shrunk.simplify(F4_VERTEX_SIMPLIFY_M, preserve_topology=True)
                    polys = (shrunk.geoms if isinstance(shrunk, MultiPolygon) else [shrunk])
                    for poly in polys:
                        if poly.is_empty:
                            continue
                        for ring in [poly.exterior] + list(poly.interiors):
                            pts.extend((float(x), float(y)) for x, y in list(ring.coords)[:-1])
            except Exception:
                pts = []
        # Deterministic order, de-duplicated at centimetre resolution.
        seen, out = set(), []
        for x, y in pts:
            k = (round(x, 2), round(y, 2))
            if k in seen:
                continue
            seen.add(k)
            out.append((x, y))
        out.sort(key=lambda q: (round(q[0], 3), round(q[1], 3)))
        self._f4_corners_proj = out
        return out

    def f4_local_vertices(self, a_deg, b_deg):
        """The corners LOCAL to one a→b transit: inside the a/b bounding box grown by
        F4_LOCAL_MARGIN_M, nearest to the a–b line first, capped at F4_MAX_LOCAL_VERTICES.

        Bounded by construction and independent of how large the survey area is — a corner on
        the far side of the polygon can never enter this set. A detour that would need to reach
        further than the margin is not the short local detour this tier exists for; it is left
        to the bypass / A* tiers that already handle it."""
        ax, ay = self.to_proj.transform(a_deg[0], a_deg[1])
        bx, by = self.to_proj.transform(b_deg[0], b_deg[1])
        m = F4_LOCAL_MARGIN_M
        lo_x, hi_x = min(ax, bx) - m, max(ax, bx) + m
        lo_y, hi_y = min(ay, by) - m, max(ay, by) + m
        seg = LineString([(ax, ay), (bx, by)])
        near = []
        for (x, y) in self._f4_corners():
            if x < lo_x or x > hi_x or y < lo_y or y > hi_y:
                continue
            near.append((round(seg.distance(Point(x, y)), 4), round(x, 3), round(y, 3), (x, y)))
        near.sort()
        return [q for _d, _rx, _ry, q in near[:F4_MAX_LOCAL_VERTICES]]

    def shortest_safe_transit(self, a_deg, b_deg, limit_m=None):
        """The shortest path a→b through the LOCAL corner set whose every leg is accepted by
        the authoritative predicate, or None.

        Deterministic Dijkstra over {a, b} ∪ local corners. An edge exists only when
        `_seg_safe_cached(u, v, True)` accepts it — the SAME predicate, the same region
        (`buildable` = shoreline-inset MINUS buffered no-go, minus the wire margin) and the same
        tolerances every other tier is held to. No new geometry test exists and nothing is
        relaxed; this tier can only ever propose a path that the existing predicate already
        calls safe.

        `limit_m` prunes the search: a partial path already longer than the aligned candidate it
        would have to beat cannot win, so it is dropped rather than expanded.

        The result is then run through the grid's own `_compress_los`, which re-proves every
        retained hop, so a corner the graph routed through but does not need is removed."""
        self.f4_invocations += 1
        corners = self.f4_local_vertices(a_deg, b_deg)
        self.f4_candidate_vertices += len(corners)
        if not corners:
            return None
        nodes = [[float(a_deg[0]), float(a_deg[1])], [float(b_deg[0]), float(b_deg[1])]]
        nodes.extend(list(self.to_deg.transform(x, y)) for (x, y) in corners)
        n = len(nodes)
        proj = [self.to_proj.transform(c[0], c[1]) for c in nodes]

        def leg_m(i, j):
            return math.hypot(proj[j][0] - proj[i][0], proj[j][1] - proj[i][1])

        dist = [float("inf")] * n
        prev = [None] * n
        dist[0] = 0.0
        # (cost, node) — the node index breaks cost ties, and neighbours are always scanned in
        # index order, so an identical input yields an identical path.
        pq = [(0.0, 0)]
        done = [False] * n
        while pq:
            d, i = heapq.heappop(pq)
            if done[i]:
                continue
            done[i] = True
            if i == 1:
                break
            for j in range(n):
                if j == i or done[j]:
                    continue
                nd = d + leg_m(i, j)
                if nd >= dist[j] or (limit_m is not None and nd > limit_m):
                    continue
                self.f4_edge_probes += 1
                if not self._seg_safe_cached(nodes[i], nodes[j], True):
                    continue
                dist[j] = nd
                prev[j] = i
                heapq.heappush(pq, (nd, j))
        if dist[1] == float("inf"):
            return None
        path, k = [], 1
        while k is not None:
            path.append(nodes[k])
            k = prev[k]
        path = _dedup(list(reversed(path)))
        if len(path) < 2:
            return None
        # Re-proved shortcutting: drop any corner the straight hop can skip safely.
        path = self._compress_los(path, True)
        if not all(self._seg_safe_cached(p, q, True) for p, q in zip(path, path[1:])):
            return None          # defensive: never emit a path this grid does not accept
        return path

    def repair_path(self, coords_deg):
        """Walk an ordered coverage path and replace every UNSAFE straight hop with a safe
        connector, leaving safe hops (the scan lines themselves) untouched. This is what
        removes the classic failure: on an asymmetric/concave polygon a lane-to-lane turn or
        a no-go-split bridge can leave the navigable polygon even when each scan line is
        individually valid. Raises ConnectorError if any hop cannot be made safe."""
        if len(coords_deg) < 2:
            return list(coords_deg)
        out = [list(coords_deg[0])]
        for a, b in zip(coords_deg, coords_deg[1:]):
            if self.segment_is_safe(a, b, require_inside=True):
                out.append(list(b))
            else:
                conn = self.safe_connector(a, b, require_inside=True)
                out.extend(conn[1:])  # a already present as out[-1]
        return _dedup(out)


_GEOD = None


def _geod():
    global _GEOD
    if _GEOD is None:
        _GEOD = pyproj.Geod(ellps="WGS84")
    return _GEOD


def _ring(coords):
    """Normalise a boundary/zone input to a closed [[lng,lat],...] ring, or None."""
    pts = _coords_of(coords)
    if not pts or len(pts) < 3:
        return None
    ring = [[float(p[0]), float(p[1])] for p in pts]
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def _coords_of(geom):
    """Accept a GeoJSON Polygon, a bare ring [[lng,lat],...], or a Feature and return the
    outer ring as a list of [lng,lat]. Returns [] on anything unrecognised."""
    if geom is None:
        return []
    if isinstance(geom, dict):
        t = str(geom.get("type", "")).lower()
        if t == "feature":
            return _coords_of(geom.get("geometry"))
        if t == "polygon":
            rings = geom.get("coordinates") or []
            return rings[0] if rings else []
        if t == "point":
            c = geom.get("coordinates")
            return [c] if c else []
        return []
    if isinstance(geom, (list, tuple)):
        return list(geom)
    return []


def _point_of(pt):
    """A [lng, lat] pair from a GeoJSON Point / Feature / bare pair, or None."""
    c = _coords_of(pt) if isinstance(pt, dict) else pt
    if isinstance(pt, dict) and str(pt.get("type", "")).lower() == "point":
        c = pt.get("coordinates")
    if isinstance(c, (list, tuple)) and len(c) >= 2 and _num(c[0]) is not None and _num(c[1]) is not None:
        return [float(c[0]), float(c[1])]
    return None


def _first_present(*vals):
    """First value that is not None (used to merge candidate field spellings, e.g. the
    approach/return migration accepting old `transit_waypoints`)."""
    for v in vals:
        if v is not None:
            return v
    return None


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _path_length_m(coords):
    """Geodesic length of a [[lng,lat],...] path, in metres."""
    if not coords or len(coords) < 2:
        return 0.0
    total = 0.0
    g = _geod()
    for (lng1, lat1), (lng2, lat2) in zip(coords, coords[1:]):
        _, _, d = g.inv(lng1, lat1, lng2, lat2)
        total += d
    return total


def _polygon_area_m2(ring):
    if not ring or len(ring) < 4:
        return 0.0
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    area, _ = _geod().polygon_area_perimeter(lons, lats)
    return abs(area)


def _seg(kind, coords):
    return {"kind": kind, "coordinates": [[float(c[0]), float(c[1])] for c in coords],
            "length_m": round(_path_length_m(coords), 2)}


def _dedup(coords):
    """Drop consecutive near-identical points (7-dp)."""
    out = []
    for c in coords:
        p = [round(float(c[0]), 7), round(float(c[1]), 7)]
        if not out or out[-1] != p:
            out.append([float(c[0]), float(c[1])])
    return out


def _rk(pt):
    """A rounded (lng,lat) key (~1 cm) used to mark a point as 'protected' by VALUE during
    cleanup — semantic points are exact waypoint coordinates, so value membership is stable
    even as intermediate points are removed and indices shift."""
    return (round(float(pt[0]), 7), round(float(pt[1]), 7))


def _turn_angle_deg(a, b, c):
    """Turn angle at b for the polyline a→b→c, in degrees (0 = straight through, 180 = full
    reversal). Operates in projected metres so the angle is metric, not lon/lat-distorted."""
    v1x, v1y = b[0] - a[0], b[1] - a[1]
    v2x, v2y = c[0] - b[0], c[1] - b[1]
    m1 = math.hypot(v1x, v1y)
    m2 = math.hypot(v2x, v2y)
    if m1 < TOLERANCE or m2 < TOLERANCE:
        return 0.0
    cos = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (m1 * m2)))
    return math.degrees(math.acos(cos))


def _route_quality(proj_pts):
    """Objective, inspectable route-quality diagnostics from the FINAL projected route (PART 7):
    backtracking events (single-vertex near-reversals that a clean boustrophedon never needs) and
    the minimum leg length. No vague score — every number is directly re-derivable.

    Fragment counting and sweep ordering are NOT derived here: the coverage generator reports the
    lane fragments it actually emitted (see _survey_frame_coverage), which is exact — inferring
    them from turn angles only worked while every lane turn was a ~180° reversal."""
    backtracks = 0
    min_leg = None
    for i in range(len(proj_pts) - 1):
        ax, ay = proj_pts[i]
        bx, by = proj_pts[i + 1]
        d = math.hypot(bx - ax, by - ay)
        if min_leg is None or d < min_leg:
            min_leg = d
    for i in range(1, len(proj_pts) - 1):
        if _turn_angle_deg(proj_pts[i - 1], proj_pts[i], proj_pts[i + 1]) >= BACKTRACK_ANGLE_DEG:
            ax, ay = proj_pts[i - 1]
            cx, cy = proj_pts[i + 1]
            if math.hypot(cx - ax, cy - ay) < BACKTRACK_RETURN_M:  # returns to ~origin: a spike
                backtracks += 1
    return {"backtracking_events": backtracks,
            "minimum_segment_length_m": round(min_leg, 2) if min_leg is not None else None}


# ═══════════════════════════════════════════════════════════════════════════════════════
# SURVEY-FRAME COVERAGE GENERATION
#
# WHY THIS EXISTS (and what it replaced)
# --------------------------------------
# The ported Scout generator handles a no-go zone by cutting the coverage polyline where it
# enters the obstacle and then BRIDGING the two cut ends ALONG THE OBSTACLE BOUNDARY
# (_create_augmented_obstacle_tracks + _find_bridging_path + _stitch_segments). With an
# operator no-go clearance in play the obstacle handed to it is the ROUND buffered exclusion, so
# the bridge is a chain of ~1 m chords tracing a buffer arc: geometrically safe, but a rounded,
# constantly-turning "coverage" leg with no stable sonar heading.
#
# This generator produces the same lane family, at the same spacing, in the same survey frame —
# but it treats the exclusion as what it is: space REMOVED from the surveyable region. Each
# nominal lane is clipped against the approved region, the surviving pieces stay STRAIGHT and
# parallel to U, and the transitions between them are built from U/V-parallel legs. Nothing ever
# follows an exclusion boundary for coverage.
#
# The safety geometry is untouched: every candidate leg is proven with the SAME
# _NavGrid.segment_is_safe against the SAME `grid.navigable` (shoreline-inset boundary MINUS the
# buffered no-go exclusion). Survey-frame alignment is a PREFERENCE ORDER over candidates, never
# a relaxation of a check — an aligned leg that cuts the clearance is rejected like any other.
# ═══════════════════════════════════════════════════════════════════════════════════════

def _min_useful_fragment_m(lane_spacing_m):
    """The minimum USEFUL coverage-fragment length for a given lane spacing — see
    MIN_FRAGMENT_LANE_FRACTION for why the rule is `max(cleanup spacing, spacing/4)` and not a
    free-standing constant."""
    return max(CLEANUP_MIN_SPACING_M,
               MIN_FRAGMENT_LANE_FRACTION * abs(float(lane_spacing_m or 0.0)))


def _survey_align_class(a_proj, b_proj, angle_deg, tol_deg=SURVEY_ALIGN_TOL_DEG):
    """Classify one projected leg against the survey frame of `angle_deg`:

        "U"      parallel to the survey angle (a coverage lane)
        "V"      perpendicular to it (a cross-lane transition)
        "short"  below ALIGN_MIN_LEG_M — no meaningful heading, not classified either way
        "other"  an arbitrary-angle leg

    Bearings are metric (projected UTM, not lon/lat) and compared modulo 180°, because a lane
    flown in either direction lies on the same axis."""
    dx, dy = b_proj[0] - a_proj[0], b_proj[1] - a_proj[1]
    if math.hypot(dx, dy) < ALIGN_MIN_LEG_M:
        return "short"
    brg = math.degrees(math.atan2(dx, dy)) % 180.0
    for axis, ref in (("U", float(angle_deg) % 180.0),
                      ("V", (float(angle_deg) + 90.0) % 180.0)):
        d = abs(brg - ref) % 180.0
        if min(d, 180.0 - d) <= tol_deg:
            return axis
    return "other"


def _rotator(angle_deg_ccw, origin):
    """A point rotator with shapely.affinity.rotate's exact convention (counter-clockwise, in
    degrees, about `origin`), so the metre arithmetic below happens in EXACTLY the frame the
    polygon clipping is done in."""
    ca = math.cos(math.radians(angle_deg_ccw))
    sa = math.sin(math.radians(angle_deg_ccw))
    ox, oy = origin

    def fn(x, y):
        dx, dy = x - ox, y - oy
        return (ox + ca * dx - sa * dy, oy + sa * dx + ca * dy)
    return fn


class _SurveyFrame:
    """One coverage pass's survey frame: the rotated coordinate system in which the survey angle
    lies on the axes, so rot-frame +x is U (along a lane) and rot-frame +y is V (across lanes).

    Constructed exactly as the ported _generate_boustrophedon_path does — rotate by -(90° −
    survey_angle) about the shoreline-inset polygon's centroid — so lane placement and the
    meaning of `lane_spacing_m` are unchanged. Everything here is derived from the `grid`, so the
    frame can never disagree with the geometry the safety checks use."""

    def __init__(self, grid, angle_deg):
        self.grid = grid
        self.angle_deg = float(angle_deg)
        self.math_angle = (90.0 - self.angle_deg) % 360.0
        c = grid.inset.centroid
        self.origin = (c.x, c.y)
        self._to_rot = _rotator(-self.math_angle, self.origin)
        self._from_rot = _rotator(self.math_angle, self.origin)
        # The inset (shoreline-clearance) polygon defines the lane family's extent, and
        # `grid.coverage` (inset MINUS the buffered exclusion, pulled in far enough that a lane
        # end is strictly interior to the region `segment_is_safe` proves against) defines what
        # survives the clip.
        self.inset_rot = rotate(grid.inset, -self.math_angle, origin=c)
        self.nav_rot = rotate(grid.coverage, -self.math_angle, origin=c)
        # Per-exclusion-body survey-frame extents, for local bypass candidates. Per BODY, not one
        # union box: two zones at opposite ends of the survey must not fuse into one huge box.
        self.exclusion_boxes = []
        if grid.nogo is not None and not grid.nogo.is_empty:
            bodies = (grid.nogo.geoms if isinstance(grid.nogo, MultiPolygon) else [grid.nogo])
            for body in bodies:
                if body.is_empty:
                    continue
                self.exclusion_boxes.append(
                    rotate(body, -self.math_angle, origin=c).bounds)

    # ── coordinate plumbing ──────────────────────────────────────────────────────────────
    def deg_to_rot(self, pt_deg):
        return self._to_rot(*self.grid.to_proj.transform(pt_deg[0], pt_deg[1]))

    def rot_to_proj(self, pt_rot):
        return self._from_rot(*pt_rot)

    def rot_to_deg(self, pt_rot):
        x, y = self._from_rot(*pt_rot)
        return list(self.grid.to_deg.transform(x, y))

    def bypass_boxes_for(self, a_rot, b_rot):
        """The exclusion bodies whose survey-frame extent is local to the a→b transition, nearest
        first and capped at MAX_BYPASS_BODIES. Bounded by construction — this is local candidate
        generation, not a search over the whole region."""
        lo_x, hi_x = min(a_rot[0], b_rot[0]), max(a_rot[0], b_rot[0])
        lo_y, hi_y = min(a_rot[1], b_rot[1]), max(a_rot[1], b_rot[1])
        m = BYPASS_MARGIN_M
        near = []
        for (bx0, by0, bx1, by1) in self.exclusion_boxes:
            if bx1 < lo_x - m or bx0 > hi_x + m or by1 < lo_y - m or by0 > hi_y + m:
                continue  # not local to this transition
            cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
            near.append((math.hypot(cx - a_rot[0], cy - a_rot[1]), (bx0, by0, bx1, by1)))
        near.sort(key=lambda e: (round(e[0], 3), e[1]))
        return [b for _, b in near[:MAX_BYPASS_BODIES]]


def _chain_reversals(dirs):
    """How many near-reversals (≥ BACKTRACK_ANGLE_DEG) a sequence of unit heading vectors makes.
    Used only to RANK equally-safe, equally-aligned transition candidates: given a choice between
    two orthogonal bend orders of identical length, the one that does not double back is the one
    a survey vessel should fly."""
    n = 0
    for (x1, y1), (x2, y2) in zip(dirs, dirs[1:]):
        cos = max(-1.0, min(1.0, x1 * x2 + y1 * y2))
        if math.degrees(math.acos(cos)) >= BACKTRACK_ANGLE_DEG:
            n += 1
    return n


def _unit(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    m = math.hypot(dx, dy)
    return (dx / m, dy / m) if m > TOLERANCE else None


def _rot_path_len(pts_rot):
    return sum(math.hypot(pts_rot[i + 1][0] - pts_rot[i][0], pts_rot[i + 1][1] - pts_rot[i][1])
               for i in range(len(pts_rot) - 1))


def _aligned_transition(frame, a_deg, b_deg, in_dir=None, out_dir=None,
                       optimize_transit=True):
    """The transition a→b between two coverage fragments. Returns (coords_deg, category).

    CONNECTOR PRIORITY (each tier's candidates are proven before the next tier is considered):

      0.  "direct_transit"        the straight segment whenever it is SAFE, at ANY heading. See
                       DIRECT-SAFE-FIRST below for why a transition is not held to the
                       survey-frame alignment the coverage lanes are;
      0b. "shortest_safe_transit" when the straight segment is NOT safe: the shortest path
                       through a BOUNDED LOCAL corner set, taken only when it beats the best
                       aligned candidate by F4_MIN_GAIN_M. See SHORTEST-SAFE below;
      1. "direct"      the straight segment, when it is ALREADY U- or V-aligned and safe;
      2. "orthogonal"  a two-leg L in the survey frame. BOTH bend orders are generated (U→V and
                       V→U) and both are validated, because one can cut the buffered exclusion or
                       leave the inset while the other does not — the order is never assumed;
      3. "bypass"      bounded families of survey-frame staircases stepping around a LOCAL
                       exclusion body's survey-frame extent — three legs (V→U→V, U→V→U) first,
                       then five legs ("around the block") for a body that is tilted in this
                       frame. This is what replaces the old arc-following bridge: every leg is
                       U- or V-parallel, so a lane the exclusion split is rejoined with straight
                       survey-frame geometry instead of a traced buffer arc;
      4. "fallback"    the existing generic _NavGrid.safe_connector (bounded grid A* + safe
                       line-of-sight compression), used ONLY when no aligned candidate is safe;
      5. fail closed   safe_connector raises ConnectorError, which generation does not swallow.

    Within a tier, safe candidates are ranked by (near-reversals, length, generation order) — so
    the choice is deterministic and identical inputs always produce an identical route.

    EVERY candidate leg is checked with the grid's own segment_is_safe(require_inside=True)
    (memoised): the same predicate, the same tolerances and the same `grid.navigable`
    (shoreline-inset MINUS buffered no-go exclusion) the previous generator was held to. Alignment
    only decides which safe candidate is PREFERRED; it never lets an unsafe leg through.

    DIRECT-SAFE-FIRST (tier 0)
    --------------------------
    A COVERAGE LANE must be parallel to the survey angle: it is the sonar pass, and a stable
    heading at a fixed lane spacing is what makes the swaths overlap. A TRANSITION is not a
    sonar pass — it is the vessel moving from the end of one finished fragment to the start of
    the next — so holding it to the same axes buys nothing acoustically and costs real distance.

    Measured on the operator's own large planning polygon, forcing transitions onto U/V made the
    route fly BACK ALONG a lane it had just surveyed (154–241 m of exact retrace per survey
    angle) and emit two 90° corners where a straight leg needs none, while a straight leg between
    the same two fragment ends was already provably safe in 17–25 of every ~22–29 transitions.

    So the straight segment is now tried FIRST, at whatever heading it happens to have. Nothing
    about safety changes: acceptance is `grid._seg_safe_cached(a, b, True)` — the SAME
    authoritative predicate every other tier is held to, against the same `buildable` region
    (shoreline-inset MINUS the buffered no-go exclusion, pulled in by the wire margin). No new
    geometry predicate exists, and no tolerance is relaxed. When the straight segment is not safe
    this returns nothing and tiers 1–4 run exactly as before, so no fallback is removed.

    The category is DELIBERATELY DISTINCT from "direct": an arbitrary-angle transit leg must be
    reportable as transit, never as an arbitrary-angle coverage lane. See the coverage/transition
    split in the route-quality alignment metrics.

    SHORTEST-SAFE (tier 0b)
    -----------------------
    Tier 0 leaves one case open: the straight leg is unsafe, so a survey-frame L or staircase
    replaces it — and that replacement is often far longer than the obstruction requires.
    Measured on the operator's own large planning polygon, three transits accounted for most of
    the remaining visible detour: 110.1 m for a 78.2 m gap, 160.6 m for 114.5 m, 162.6 m for
    109.5 m. In two of the three the straight leg missed by leaving the approved region at a
    shoreline concavity, once by barely two metres.

    So when tier 0 declines, the aligned tiers below are evaluated FIRST — exactly as before,
    producing exactly the candidate they produce today — and only then is a bounded local detour
    searched, with that candidate's length as the bar to beat. The aligned candidate is retained
    unless the detour wins by F4_MIN_GAIN_M, so a near-tie never churns the route hash.

    The search is `_NavGrid.shortest_safe_transit`: Dijkstra over {a, b} plus at most
    F4_MAX_LOCAL_VERTICES corners drawn from the a/b bounding box grown by F4_LOCAL_MARGIN_M.
    It is local and capped by construction, so its cost does not grow with the survey area, and
    every edge is admitted only by the same `_seg_safe_cached(..., True)` as every other tier.

    `optimize_transit=False` restores the pre-F2 aligned-only behaviour by suppressing BOTH
    optimisation tiers. It exists so the aligned-only baseline stays reachable and comparable —
    the regression suites generate against it to prove the coverage CONTENT is independent of how
    transits are drawn. It is not used in generation.

    An earlier BCD cell-entry probe did use it, to ask "could this entry be BUILT from
    survey-frame legs at all?" while the entry bit was decided on buildability. The cell order and
    orientation are now decided on measured DISTANCE (see _bcd_cell_plan), and the honest distance
    is the one the vessel really travels — so the ordering costs candidates through the full tier
    hierarchy, deliberately. What replaced the old invariant is stronger and is what always
    mattered: the coverage content cannot depend on the transit policy, only the ORDER may."""
    grid = frame.grid
    a_rot, b_rot = frame.deg_to_rot(a_deg), frame.deg_to_rot(b_deg)
    du, dv = b_rot[0] - a_rot[0], b_rot[1] - a_rot[1]

    # ── tier 0: the straight segment at ANY heading, when the authoritative predicate accepts it ─
    if optimize_transit and grid._seg_safe_cached(a_deg, b_deg, True):
        pts = _dedup([list(a_deg), list(b_deg)])
        if len(pts) == 2:
            return pts, "direct_transit"

    # ── tier 1: the direct segment, but only when it is already an axis of the survey frame ──
    # `_survey_align_class` is the ONE authority on whether a leg is survey-frame aligned, and it
    # is the same predicate the route-quality contract counts violations with. This gate used to
    # ALSO admit any segment whose du or dv fell under ALIGN_MIN_LEG_M, which is a different and
    # much weaker test: a 1 m sideways step across a 5 m lane gap is 11.3° off the V axis, well
    # outside the 5° tolerance, yet the shortcut let it through as "direct". That admitted legs
    # the contract counts as arbitrary-angle coverage — a latent inconsistency that stayed hidden
    # only because the previous traversal happened never to pair lane ends in the offending band.
    #
    # A genuinely degenerate hop is still accepted: the classifier answers "short" below
    # ALIGN_MIN_LEG_M, which is exactly the case the shortcut existed to cover. Everything else
    # now falls through to the orthogonal / bypass tiers. Strictly narrower than before — this can
    # only reject candidates it used to accept, never accept one it used to reject.
    direct_ok = _survey_align_class(frame.rot_to_proj(a_rot), frame.rot_to_proj(b_rot),
                                    frame.angle_deg) in ("U", "V", "short")
    tiers = [[("direct", [a_rot, b_rot])] if direct_ok else []]

    # ── tier 2: two-leg orthogonal L, both bend orders ───────────────────────────────────────
    # Generated whenever a real L exists (both offsets non-degenerate). The guard used to demand
    # both offsets exceed ALIGN_MIN_LEG_M, which was the mirror image of the tier-1 shortcut: a
    # transition with a sub-metre du was expected to have been swallowed by tier 1 as "direct".
    # Now that tier 1 only accepts genuinely aligned segments, that same transition must have an
    # L to fall through to, or it would skip to the bypass/A* tiers and emit exactly the
    # arbitrary-angle geometry this change removes. The short leg of such an L classifies as
    # "short", so the alignment contract still holds.
    ortho = []
    if abs(du) > TOLERANCE and abs(dv) > TOLERANCE:
        ortho.append(("orthogonal", [a_rot, (b_rot[0], a_rot[1]), b_rot]))   # U first, then V
        ortho.append(("orthogonal", [a_rot, (a_rot[0], b_rot[1]), b_rot]))   # V first, then U
    tiers.append(ortho)

    # ── tier 3: bounded survey-frame staircases around a local exclusion body ────────────────
    # Two families, tried in order of cost. Both are generated from the body's survey-frame
    # bounding extent only — a couple of arithmetic candidates per body, no search.
    stairs = []
    around = []
    for (bx0, by0, bx1, by1) in frame.bypass_boxes_for(a_rot, b_rot):
        m = BYPASS_MARGIN_M
        # (a) three legs. V→U→V: step across V clear of the body, run the long leg along U, step
        # back. This is the shape that rejoins the two halves of a lane an exclusion cut in two,
        # and U→V→U is the same shape with the axes swapped for a body that blocks the V step.
        for yo in sorted([by1 + m, by0 - m], key=lambda y: abs(y - a_rot[1])):
            stairs.append(("bypass", [a_rot, (a_rot[0], yo), (b_rot[0], yo), b_rot]))
        for xo in sorted([bx1 + m, bx0 - m], key=lambda x: abs(x - a_rot[0])):
            stairs.append(("bypass", [a_rot, (xo, a_rot[1]), (xo, b_rot[1]), b_rot]))
        # (b) five legs, "around the block". A fragment ends ON the exclusion boundary, and that
        # boundary is almost never parallel to the survey frame — a no-go zone is drawn at
        # whatever angle the operator drew it, and the round buffer curves at the corners. So the
        # (a) step that leaves A along V at A's own U coordinate can clip the body a few metres
        # further along, which is exactly what happens when the body is tilted in this frame.
        # This family backs each V step off to a U coordinate OUTSIDE the body's whole U extent
        # first, which is clear of it for every V — still five straight U/V-parallel legs.
        xa = bx1 + m if a_rot[0] >= (bx0 + bx1) / 2.0 else bx0 - m
        xb = bx1 + m if b_rot[0] >= (bx0 + bx1) / 2.0 else bx0 - m
        for yo in sorted([by1 + m, by0 - m], key=lambda y: abs(y - a_rot[1])):
            around.append(("bypass", [a_rot, (xa, a_rot[1]), (xa, yo),
                                      (xb, yo), (xb, b_rot[1]), b_rot]))
    tiers.append(stairs)
    tiers.append(around)

    # The aligned tiers are evaluated EXACTLY as before and produce EXACTLY the candidate they
    # produce today. Nothing here is skipped or reordered — the result is simply held rather than
    # returned immediately, so tier 0b knows what it would have to beat before it searches.
    chosen = None
    for tier in tiers:
        best = None
        for idx, (category, pts_rot) in enumerate(tier):
            pts_deg = _dedup([frame.rot_to_deg(p) for p in pts_rot])
            if len(pts_deg) < 2:
                continue
            # The memoised predicate: candidate variants share legs (the same V step appears in
            # several staircases), so the same probe is asked for repeatedly within one transition.
            if not all(grid._seg_safe_cached(p, q, True)
                       for p, q in zip(pts_deg, pts_deg[1:])):
                continue
            dirs = [d for d in (_unit(pts_rot[i], pts_rot[i + 1])
                                for i in range(len(pts_rot) - 1)) if d]
            if in_dir:
                dirs.insert(0, in_dir)
            if out_dir:
                dirs.append(out_dir)
            key = (_chain_reversals(dirs), round(_rot_path_len(pts_rot), 3), idx)
            if best is None or key < best[0]:
                best = (key, category, pts_deg)
        if best is not None:
            chosen = (best[2], best[1])
            break

    # ── tier 0b: a bounded LOCAL detour, but only if it beats what the aligned tiers found ───
    # Run before the generic A* so the priority order reads 0 → 0b → aligned → A*, and so the
    # A* is not paid for and then discarded (which would also colour the connector counters).
    if optimize_transit:
        bar = None if chosen is None else grid._proj_len_m(chosen[0]) - F4_MIN_GAIN_M
        if bar is None or bar > 0:
            detour = grid.shortest_safe_transit(a_deg, b_deg, limit_m=bar)
            if detour is not None:
                d_len = grid._proj_len_m(detour)
                if chosen is None or d_len <= bar:
                    grid.shortest_safe_count += 1
                    grid.shortest_safe_len_m += d_len
                    if chosen is not None:
                        grid.shortest_safe_saved_m += grid._proj_len_m(chosen[0]) - d_len
                    return detour, "shortest_safe_transit"
    if chosen is not None:
        return chosen

    # ── tier 4/5: the existing generic safe connector, or a hard ConnectorError ───────────────
    # Unchanged, including its fail-closed behaviour: if no safe connector exists, generation
    # still refuses rather than emitting something unproven.
    return grid.safe_connector(a_deg, b_deg, require_inside=True), "fallback"


def _lane_fragments(frame, spacing):
    """The nominal lawnmower lane family, CLIPPED to the approved region, as STRAIGHT fragments.

    The lane family itself is the ported generator's, unchanged — `max(1, int(extent/spacing))`
    lanes spaced `spacing` metres apart along V across the shoreline-inset polygon's survey-frame
    extent — so `lane_spacing_m` keeps its exact current meaning and a plan with no no-go zone
    generates the same lanes it did before.

    What changed is the CLIP. Each nominal lane is intersected with `grid.coverage` (the inset
    polygon MINUS the buffered no-go exclusion, pulled in by the wire margin), so an exclusion
    simply REMOVES surveyable space and leaves one or more straight, U-parallel pieces on that
    lane. A piece is reduced to its two extreme endpoints, which is exact: the intersection of a
    line with a polygon is a set of disjoint COLLINEAR runs, so a run's endpoints describe it
    completely and no interior vertex from an exclusion arc can survive into the coverage
    geometry.

    Clipping flush against `grid.navigable` would put every lane endpoint EXACTLY on the approved
    boundary, and the cross-lane transition between two such endpoints then runs ALONG that
    boundary — which serializes to a leg lying millimetres OUTSIDE the polygon it was clipped
    from, and that is what Scout rejects. See WIRE_MARGIN_M.

    Returns (fragments, skipped) where `skipped` records the fragments dropped by the
    minimum-useful-fragment rule (count + total length, so the loss is reported, not hidden)."""
    minx, miny, maxx, maxy = frame.inset_rot.bounds
    width = maxx - minx
    height = maxy - miny
    sep = abs(spacing) if spacing > 1e-6 else 0.1
    min_useful = _min_useful_fragment_m(sep)

    # The ported lane offsets, verbatim (numpy included) — lane COUNT and POSITIONS must not move.
    y_coords = np.linspace(miny + sep / 2, maxy - sep / 2, num=max(1, int(height / sep)))
    if not y_coords.size:
        y_coords = np.array([(miny + maxy) / 2])

    nav = frame.nav_rot.buffer(0)
    fragments = []
    skipped = {"count": 0, "length_m": 0.0}
    row = -1
    for y in y_coords:
        row += 1
        scan = LineString([(minx - width * 1.1, y), (maxx + width * 1.1, y)])
        try:
            clipped = nav.intersection(scan)
        except Exception:
            continue
        if clipped.is_empty:
            continue
        pieces = []
        if isinstance(clipped, LineString):
            pieces = [clipped]
        elif isinstance(clipped, (MultiLineString, GeometryCollection)):
            pieces = [g for g in clipped.geoms if isinstance(g, LineString)]
        runs = []
        for piece in pieces:
            xs = [c[0] for c in piece.coords]
            if not xs:
                continue
            x0, x1 = min(xs), max(xs)
            if x1 - x0 <= TOLERANCE:
                continue
            runs.append((x0, x1))
        runs.sort()
        keep = []
        for (x0, x1) in runs:
            if x1 - x0 < min_useful:
                skipped["count"] += 1
                skipped["length_m"] += (x1 - x0)
                continue
            keep.append((x0, x1))
        for along, (x0, x1) in enumerate(keep):
            fragments.append({
                "row": row, "along_index": along, "sweep": float(y),
                "rot": [(x0, float(y)), (x1, float(y))],
                "length_m": x1 - x0,
            })
    return fragments, skipped


def _fragments_overlap_in_u(f, g):
    """Do two lane fragments overlap along U (the along-lane axis)?

    This is the SLICE CONNECTIVITY test of the boustrophedon decomposition: two fragments on
    ADJACENT sweep rows belong to the same connected piece of surveyable water exactly when
    their U intervals overlap. A shared endpoint is not an overlap (two fragments that merely
    touch at a corner are not a continuation), so the comparison is strict."""
    fu0, fu1 = f["rot"][0][0], f["rot"][1][0]
    gu0, gu1 = g["rot"][0][0], g["rot"][1][0]
    return (min(fu1, gu1) - max(fu0, gu0)) > TOLERANCE


def _bcd_cells(fragments):
    """Boustrophedon CELLULAR DECOMPOSITION of the clipped lane fragments (Choset).

    WHY THIS EXISTS
    ---------------
    The traversal this replaced ordered fragments by the flat key (row, along_index): EVERY
    fragment of lane N had to be flown before lane N+1. Where an exclusion splits a lane in two
    that is a demand the geometry cannot satisfy cheaply — the route has to leave the left
    fragment, travel all the way AROUND the exclusion, fly the right fragment, and come back
    around again on the next lane. The result was one long bypass staircase per split lane, its
    cross-lane legs sweeping repeatedly up and down beside the obstacle and crossing water that
    was already surveyed. The bypasses were individually safe and individually shortest; the
    ORDER that demanded them was the defect.

    THE RULE
    --------
    Fragments are grouped into CELLS at the sweep's critical points. Walking the sweep rows in
    increasing V, a fragment CONTINUES its predecessor's cell when the connection is
    one-to-one — it overlaps exactly one fragment on the previous row, and that fragment
    overlaps exactly one fragment on this row. Anything else is a critical point:

        1 fragment  -> 2+ fragments   an exclusion (or a concavity) SPLITS the free space
        2+ fragments -> 1 fragment    the free space MERGES again past the obstacle
        0 fragments -> 1+             a new region opens (also when the sweep row index is not
                                      contiguous, i.e. a whole lane fell outside the region)

    and every fragment involved opens a new cell. Each cell is therefore a maximal run of
    surveyable water whose sweep topology never changes — precisely the region a plain
    boustrophedon covers without ever needing to cross an obstacle.

    WHAT THIS IS NOT
    ----------------
    Not a nearest-neighbour heuristic, not a TSP, not an optimizer: it is a topological
    grouping followed by the SAME lane-monotonic alternating traversal as before, applied per
    cell. Cell membership depends only on the clipped geometry, so identical inputs always
    produce identical cells.

    Returns a list of cells, each a list of the fragment dicts belonging to it, with the cells
    ordered deterministically by (first sweep row, then lowest U) — stable geometric keys, no
    tie broken by iteration order."""
    by_row = {}
    for f in fragments:
        by_row.setdefault(f["row"], []).append(f)
    for r in by_row:
        by_row[r].sort(key=lambda f: f["rot"][0][0])
    rows = sorted(by_row)

    cells = []          # every cell ever opened, in the order it was opened
    open_cell = {}      # (row, along_index) of a fragment on the PREVIOUS row -> its cell

    def fid(f):
        return (f["row"], f["along_index"])

    for k, r in enumerate(rows):
        cur = by_row[r]
        # Only a CONTIGUOUS previous sweep row can continue a cell. A gap in the row indices
        # means a whole nominal lane produced no coverage (fully excluded, or outside the
        # region), which is itself a topology break — the fragments after the gap start fresh.
        prev = by_row[rows[k - 1]] if k > 0 and rows[k - 1] == r - 1 else []
        back = {fid(f): [p for p in prev if _fragments_overlap_in_u(p, f)] for f in cur}
        fwd = {fid(p): [f for f in cur if _fragments_overlap_in_u(p, f)] for p in prev}

        new_open = {}
        for f in cur:
            conns = back[fid(f)]
            if len(conns) == 1 and len(fwd[fid(conns[0])]) == 1:
                cell = open_cell.get(fid(conns[0]))
                if cell is None:            # defensive: predecessor never opened a cell
                    cell = []
                    cells.append(cell)
            else:
                cell = []                   # split, merge, or a newly opened region
                cells.append(cell)
            cell.append(f)
            new_open[fid(f)] = cell
        open_cell = new_open

    cells = [c for c in cells if c]
    cells.sort(key=lambda c: (min(f["row"] for f in c),
                              min(f["rot"][0][0] for f in c)))
    return cells


def _cell_key(cell):
    """The stable geometric sort key of a cell: lowest sweep row, then lowest U."""
    return (min(f["row"] for f in cell), min(f["rot"][0][0] for f in cell))


def _bcd_adjacency(cells):
    """The BCD TOPOLOGY GRAPH: which cells meet each other in the decomposition.

    Two cells are ADJACENT when a fragment of one and a fragment of the other sit on
    CONSECUTIVE sweep rows and overlap along U — i.e. they are two sides of the same critical
    point. That is exactly the split/merge relation `_bcd_cells` used to close one cell and open
    another: the cell below an obstacle is adjacent to both columns beside it, each column is
    adjacent to the cell above where the free space merges again, and the two columns are NOT
    adjacent to each other, because between them lies the obstacle rather than a shared sweep
    boundary.

    Deliberately not "every cell is a neighbour of every other". The graph exists to say which
    hand-overs are a lane-to-lane hop across a shared boundary and which cross the survey; a
    complete graph would say nothing. `_fragments_overlap_in_u` is the same slice-connectivity
    predicate the decomposition itself is built from, so the topology can never disagree with
    the cells it describes.

    Returns [frozenset(neighbour cell ids), ...] indexed by cell id (position in `cells`)."""
    by_row = {}
    for ci, cell in enumerate(cells):
        for f in cell:
            by_row.setdefault(f["row"], []).append((ci, f))
    adj = [set() for _ in cells]
    for row in sorted(by_row):
        nxt = by_row.get(row + 1)
        if not nxt:
            continue
        for ci, f in by_row[row]:
            for cj, g in nxt:
                if ci != cj and _fragments_overlap_in_u(f, g):
                    adj[ci].add(cj)
                    adj[cj].add(ci)
    return [frozenset(s) for s in adj]


def _cell_traversal_states(cell):
    """The legal boustrophedon traversals of ONE cell — the same coverage, flown four ways.

    A cell is covered lane by lane through the sweep, the along-U direction flipping on every
    lane. That pattern has exactly two free bits:

        ascending          which end of the sweep the cell is entered at (lowest row or highest)
        first_row_forward  which way the FIRST lane of the cell is flown

    and the two bits together fix everything else, including which corner the cell is LEFT at
    (the alternation runs to the end: with an odd number of rows the cell exits on the side it
    was entered from, with an even number on the opposite side). Four combinations, four
    entry/exit corners, one identical set of coverage fragments.

    The four states are two REVERSE PAIRS: reversing a state's fragment sequence end to end, and
    swapping each fragment's entry and exit, produces another of the four. FORWARD and REVERSE in
    the task's sense are those two members of a pair; both bits are enumerated so that all four
    corners are available to the optimiser, which is strictly more choice than reversal alone and
    never less.

    NOTHING GEOMETRIC IS CREATED HERE. Each state reuses the fragment dicts `_lane_fragments`
    produced — same `rot` endpoints, same lengths, same rows — and only records which end of each
    is the entry and which the exit. Coverage distance is therefore identical across all four
    states, and is returned so the caller can assert it rather than assume it.

    THE LANE-TURN LADDER IS NOT. What DOES differ between states is the repositioning BETWEEN
    consecutive lanes (`internal_transit_m`). The alternation makes each turn at one end of the
    lane pair or the other, so the two members of a reverse pair turn at the SAME set of ends and
    the other pair turns at the COMPLEMENTARY set — the value therefore takes at most TWO distinct
    values over the four states, one per pair. On a cell whose two U-boundaries mirror each other
    (a plain rectangle) the two sets are equal and all four agree; on a slanted, concave or
    obstacle-cut cell they do not. Measured across the regression matrix and every operator draft
    the spread reaches 51.1 m on one cell. See `_bcd_cell_plan` for why the objective
    deliberately does not spend it.

    Returns four state dicts in a fixed order: (ascending, first_row_forward) =
    (T,T), (T,F), (F,T), (F,F)."""
    states = []
    for ascending in (True, False):
        rows_in_cell = sorted({f["row"] for f in cell}, reverse=not ascending)
        for first_row_forward in (True, False):
            seq = []
            forward = first_row_forward
            for row in rows_in_cell:
                row_frags = sorted([f for f in cell if f["row"] == row],
                                   key=lambda f: f["rot"][0][0])
                for frag in (row_frags if forward else list(reversed(row_frags))):
                    a, b = frag["rot"][0], frag["rot"][1]
                    seq.append({**frag,
                                "cell_ascending": ascending,
                                "entry_rot": a if forward else b,
                                "exit_rot": b if forward else a,
                                "dir": (1.0, 0.0) if forward else (-1.0, 0.0)})
                forward = not forward
            states.append({
                "ascending": ascending,
                "first_row_forward": first_row_forward,
                "fragments": seq,
                "entry_rot": seq[0]["entry_rot"],
                "exit_rot": seq[-1]["exit_rot"],
                "in_dir": seq[0]["dir"],
                "out_dir": seq[-1]["dir"],
                "coverage_m": sum(f["length_m"] for f in seq),
                # The in-cell repositioning between consecutive fragments, measured straight in
                # the survey frame — NOT the emitted ladder, which F2/F4 draw and which appears in
                # route_quality as in_coverage_transition_length_m. DIAGNOSTIC ONLY: nothing in
                # `_bcd_cost_model` reads it, so it can never move the objective. It is NOT
                # invariant across the four states (see the ladder note above); the cell plan
                # reports the chosen value and the omitted spread so the choice stays auditable.
                "internal_transit_m": sum(
                    math.hypot(seq[k + 1]["entry_rot"][0] - seq[k]["exit_rot"][0],
                               seq[k + 1]["entry_rot"][1] - seq[k]["exit_rot"][1])
                    for k in range(len(seq) - 1)),
            })
    return states


# Every counter a costing probe can touch. A probe measures a candidate that mostly will NOT be
# built, so it must leave the grid's accounting exactly as it found it — the connector metrics
# describe the route that was emitted, not the search that chose it.
_GRID_PROBE_COUNTERS = ("raw_connector_pts", "final_connector_pts", "connector_len_before_m",
                        "connector_len_after_m", "astar_connector_count", "shortest_safe_count",
                        "shortest_safe_len_m", "shortest_safe_saved_m", "f4_candidate_vertices",
                        "f4_edge_probes", "f4_invocations")


def _transition_oracle(frame):
    """Costed access to the transition policy the route is actually drawn with.

    A candidate hand-over is costed by ASKING `_aligned_transition` for it — the same tiers, in
    the same order, against the same authoritative safety predicate: F2 direct-safe first, then
    F4's bounded local detour, then the survey-frame aligned tiers, then the generic A*
    fallback, then a hard refusal. So the number the optimiser minimises is the number of metres
    the vessel will really travel, not a straight-line stand-in that would be wrong exactly where
    it matters (the straight line between two cells beside an obstacle is the one path that is
    NOT available).

    Two properties this must have, and does:

      * COUNTER-NEUTRAL. Reaching the fallback tier runs the grid A*, and F4 bumps the detour
        counters; both feed route_quality. Only one of the many candidates costed is ever built,
        so every counter is snapshotted and restored around the probe. The safety cache is
        deliberately left warm — it is a pure memo of the same predicate, and warming it is what
        keeps the search affordable.
      * MEMOISED. The DP asks for the same (exit corner → entry corner) pair from many partial
        sequences; the underlying tiers are pure, so the first answer is the only one computed.

    Returns (cost_fn, stats) where cost_fn(a_deg, b_deg, in_dir, out_dir) → (metres, category,
    point_count) and an unroutable pair answers (inf, "unroutable", 0)."""
    grid = frame.grid
    memo = {}
    stats = {"evaluations": 0, "cache_hits": 0}

    def cost(a_deg, b_deg, in_dir, out_dir):
        key = (_rk(a_deg), _rk(b_deg), in_dir, out_dir)
        hit = memo.get(key)
        if hit is not None:
            stats["cache_hits"] += 1
            return hit
        # TIER 0, SHORT-CIRCUITED. When the straight segment is safe, `_aligned_transition`
        # returns exactly that segment — so asking the same authoritative predicate directly is
        # the SAME decision at a fraction of the cost, and it skips building the aligned
        # candidate families and F4's local graph for the many hand-overs that never needed
        # them. Only a genuinely blocked pair pays for the full policy.
        if grid._seg_safe_cached(a_deg, b_deg, True):
            value = (grid._proj_len_m([list(a_deg), list(b_deg)]), "direct_transit", 2)
            stats["evaluations"] += 1
            memo[key] = value
            return value
        saved = {name: getattr(grid, name) for name in _GRID_PROBE_COUNTERS}
        try:
            path, category = _aligned_transition(frame, a_deg, b_deg,
                                                 in_dir=in_dir, out_dir=out_dir)
            value = (grid._proj_len_m(path), category, len(path))
        except ConnectorError:
            value = (float("inf"), "unroutable", 0)
        finally:
            for name, was in saved.items():
                setattr(grid, name, was)
        stats["evaluations"] += 1
        memo[key] = value
        return value

    return cost, stats


def _boundary_oracle(grid):
    """Costed access to the SURVEY-ENTRY and RETURN connectors, for the ordering's boundary
    conditions.

    The first cell is not free to be chosen on inter-cell distance alone: the route has to REACH
    it from the end of the approach, and that leg is a real, executed connector. Likewise the
    last cell's exit corner decides how far the return connector has to run. Both are costed with
    `_NavGrid.safe_connector` — the very function `generate_survey` builds those two segments
    with — so the boundary term is the same metres the route will pay, not an approximation.

    The approach and return geometry themselves are untouched by this: their waypoints are an
    INPUT here, read only to say where the survey is entered from and left towards.

    Counter-neutral for the same reason as `_transition_oracle`, and memoised."""
    memo = {}
    stats = {"evaluations": 0}

    def cost(a_deg, b_deg):
        if a_deg is None or b_deg is None:
            return 0.0
        key = (_rk(a_deg), _rk(b_deg))
        hit = memo.get(key)
        if hit is not None:
            return hit
        saved = {name: getattr(grid, name) for name in _GRID_PROBE_COUNTERS}
        try:
            value = grid._proj_len_m(grid.safe_connector(a_deg, b_deg, require_inside=True))
        except ConnectorError:
            value = float("inf")
        finally:
            for name, was in saved.items():
                setattr(grid, name, was)
        stats["evaluations"] += 1
        memo[key] = value
        return value

    return cost, stats


def _bcd_cost_model(frame, cells, spacing, entry_anchor=None, exit_anchor=None):
    """Everything the cell-ordering search minimises, as one bundle of callables.

    Separated from the search so that WHAT is optimised and HOW it is searched for cannot drift
    apart — the exact search, the heuristic and the tests all cost a candidate sequence through
    this one definition. Returns:

        adjacency / states  the BCD topology graph and the four traversals of each cell
        step_detail(i,si,j,sj)  -> (cost, real metres, the tier that drew it)
        step / enter / leave    the measured costs the objective is the sum of
        step_lb / enter_lb / leave_lb
                            admissible LOWER BOUNDS on those three, computed from the corner
                            geometry alone: straight-line distance (no safe path is shorter)
                            plus the topology and reversal penalties, which are exactly known
                            from the corners and so are not under-estimated at all. The fallback
                            penalty is deliberately left out — it cannot be known without drawing
                            the transition, and omitting it only weakens the bound, never breaks
                            it. `lb <= true` everywhere is the single property the exact search
                            relies on to probe fewer candidates without changing its answer.
        transition_stats / boundary_stats
                            how many distinct measurements were actually taken."""
    lane = abs(float(spacing or 0.0)) or 1.0
    topology_penalty = BCD_TOPOLOGY_PENALTY_LANES * lane
    reversal_penalty = BCD_REVERSAL_PENALTY_LANES * lane
    adjacency = _bcd_adjacency(cells)
    states = [_cell_traversal_states(cell) for cell in cells]
    transition_cost, tstats = _transition_oracle(frame)
    boundary_cost, bstats = _boundary_oracle(frame.grid)

    def deg(pt_rot):
        return frame.rot_to_deg(pt_rot)

    def penalties(i, si, j, sj, category):
        penalty = 0.0
        if category == "fallback":
            penalty += BCD_FALLBACK_PENALTY_M
        if j not in adjacency[i]:
            penalty += topology_penalty
        heading = _unit(states[i][si]["exit_rot"], states[j][sj]["entry_rot"])
        if heading is not None:
            penalty += reversal_penalty * _chain_reversals(
                [states[i][si]["out_dir"], heading, states[j][sj]["in_dir"]])
        return penalty

    def step_detail(i, si, j, sj):
        """Leaving cell i in state si and picking cell j up in state sj:
        (cost, real metres, the tier that drew it)."""
        a, b = states[i][si], states[j][sj]
        metres, category, _pts = transition_cost(deg(a["exit_rot"]), deg(b["entry_rot"]),
                                                 a["out_dir"], b["in_dir"])
        if math.isinf(metres):
            return float("inf"), metres, category
        return metres + penalties(i, si, j, sj, category), metres, category

    def step(i, si, j, sj):
        return step_detail(i, si, j, sj)[0]

    def step_lb(i, si, j, sj):
        ax, ay = states[i][si]["exit_rot"]
        bx, by = states[j][sj]["entry_rot"]
        return math.hypot(bx - ax, by - ay) + penalties(i, si, j, sj, None)

    def enter(i, si):
        return boundary_cost(entry_anchor, deg(states[i][si]["entry_rot"]))

    def leave(i, si):
        return boundary_cost(deg(states[i][si]["exit_rot"]), exit_anchor)

    def _anchor_lb(anchor, pt_rot):
        if anchor is None:
            return 0.0
        return frame.grid._proj_len_m([list(anchor), deg(pt_rot)])

    def enter_lb(i, si):
        return _anchor_lb(entry_anchor, states[i][si]["entry_rot"])

    def leave_lb(i, si):
        return _anchor_lb(exit_anchor, states[i][si]["exit_rot"])

    return {"adjacency": adjacency, "states": states,
            "step_detail": step_detail, "step": step, "step_lb": step_lb,
            "enter": enter, "leave": leave, "enter_lb": enter_lb, "leave_lb": leave_lb,
            "transition_stats": tstats, "boundary_stats": bstats}


def _bcd_cell_plan(frame, cells, spacing, entry_anchor=None, exit_anchor=None):
    """WHICH CELL IS COVERED WHEN, AND WHICH WAY ROUND. Returns (plan, diagnostics).

    THE PROBLEM THIS SOLVES
    -----------------------
    `_bcd_cells` says what the cells ARE. Until this existed, the order they were covered in was
    their geometric sort key — lowest sweep row, then lowest U — and each cell's sweep direction
    was picked from local terms one cell at a time. On the operator's large polygon that produced
    east region → column south of the obstacle → column north of it → west region: a sequence
    whose hand-overs are 119.8 m + 29.0 m + 78.3 m, entered by a 196.7 m survey-entry connector
    from an approach that ended beside a completely different cell. Every leg of that was
    individually the shortest safe path available; the ORDER is what made them long.

    WHAT IS OPTIMISED
    -----------------
    Exactly two things, and nothing else:

        * the order the cells are visited in — each exactly once;
        * which of its four legal boustrophedon traversals each cell is flown with
          (see _cell_traversal_states).

    Coverage geometry is an INPUT. The fragments, their coordinates, their lengths, the lane
    spacing, the survey angle and cell membership are all fixed before this runs and are not
    consulted for anything except their endpoints. A cell is still covered completely, lane by
    lane, alternating, before the route moves on: this chooses among whole-cell traversals, never
    among fragments.

    THE COST
    --------
        Σ safe transition distance between consecutive cells
      + safe distance from the survey entry anchor into the first cell
      + safe distance out of the last cell to the return anchor
      + a small deterministic penalty for a hand-over between cells the decomposition does not
        make adjacent, and for one that has to double back on the heading the cell was left on
      + a large penalty for a hand-over that can only be drawn by the generic A* fallback

    Distances come from the real tiers (`_transition_oracle`, `_boundary_oracle`), so this
    minimises metres the mission actually flies. The penalties are lane-spacing multiples — one
    or two lane widths against transitions of tens to hundreds of metres — so distance dominates
    by construction; the fallback term is the exception and is deliberately lexicographic,
    because arbitrary-angle A* geometry in a hand-over is a contract violation rather than a
    slightly worse route.

    WHAT THE COST DELIBERATELY LEAVES OUT
    -------------------------------------
    The IN-CELL lane-turn ladder. A cell's four traversals cover identical fragments but do not
    all turn at the same lane ends, so the repositioning inside a cell varies between them (see
    `_cell_traversal_states`). That variation is real and is flown, but it is not spent here, for
    two reasons. It is a WITHIN-cell property, so folding it in would let a cheaper turn ladder
    buy a longer hand-over or a longer survey-entry connector — trading a term the cell plan owns
    against terms it was created to fix. And measured over the whole regression matrix and every
    operator draft it does not pay: on all eight operator drafts the plan chosen WITHOUT it is
    already optimal for total route WITH it counted (regret 0.00 m, the accepted 150° draft
    included); only anchorless synthetic fixtures, where the boundary terms vanish and the
    objective has nothing left to discriminate on, show a gap (worst 55.3 m, 3.3% of route).
    `in_cell_transit_spread_m` in the diagnostics reports how much was on the table, so the
    omission is auditable rather than assumed harmless.

    TOPOLOGY, NOT AN UNRESTRICTED TSP
    ---------------------------------
    The adjacency graph enters twice: as the penalty above, and — above the exact threshold — as
    the candidate successor set each cell offers. It is a strong preference rather than a hard
    constraint on purpose. Making it hard would forbid the one hand-over the decomposition
    genuinely needs, the single crossing from one column beside an obstacle to the other, and
    would reproduce the very sequence this replaces.

    EXACT, OR BOUNDED
    -----------------
    Up to BCD_EXACT_MAX_CELLS the search is an exact Held-Karp DP over
    (visited set, current cell, current orientation) — globally optimal for the cost above and
    reproducible. Beyond it, a deterministic topology-aware construction (cheapest admissible
    successor over both cells and orientations) followed by bounded segment-reversal improvement.
    Observed cell counts on every fixture and every operator draft are 1–7, and cell count does
    not grow with lane spacing, so the exact path is the one that runs in practice.

    `plan` is [(cell_id, state), ...] with cell_id indexing `cells`."""
    n = len(cells)
    model = _bcd_cost_model(frame, cells, spacing, entry_anchor, exit_anchor)
    adjacency, states = model["adjacency"], model["states"]
    step_detail, step, enter, leave = (model["step_detail"], model["step"],
                                       model["enter"], model["leave"])
    step_lb, enter_lb, leave_lb = model["step_lb"], model["enter_lb"], model["leave_lb"]
    tstats, bstats = model["transition_stats"], model["boundary_stats"]

    if n <= BCD_EXACT_MAX_CELLS:
        order, combo, mode = _bcd_exact_plan(n, step, enter, leave,
                                             step_lb, enter_lb, leave_lb)
    else:
        order, combo, mode = _bcd_heuristic_plan(n, cells, adjacency, step, enter, leave, step_lb)

    plan = [(order[k], states[order[k]][combo[k]]) for k in range(n)]
    legs = []
    inter_m = 0.0
    non_adjacent = 0
    for k in range(n - 1):
        i, j = order[k], order[k + 1]
        _cost, metres, category = step_detail(i, combo[k], j, combo[k + 1])
        adjacent = j in adjacency[i]
        non_adjacent += 0 if adjacent else 1
        inter_m += metres
        legs.append({"from_cell": i, "to_cell": j, "adjacent": adjacent,
                     "length_m": round(metres, 2), "category": category})
    entry_m = enter(order[0], combo[0])
    exit_m = leave(order[-1], combo[-1])
    diag = {
        "mode": mode,
        "cell_count": n,
        "orientation_states_per_cell": 4,
        "adjacency": [sorted(adjacency[i]) for i in range(n)],
        "cell_order": list(order),
        "cell_orientations": [{"cell_id": order[k],
                               "ascending": states[order[k]][combo[k]]["ascending"],
                               "first_row_forward":
                                   states[order[k]][combo[k]]["first_row_forward"]}
                              for k in range(n)],
        "handovers": legs,
        # The in-cell lane-turn ladder: what the chosen traversals spend on repositioning
        # between consecutive lanes, and the total range the four traversals of each cell offer.
        # Straight-line in the survey frame (the EMITTED ladder is drawn by F2/F4 and is
        # route_quality.in_coverage_transition_length_m - inter_cell_transit_length_m). Neither
        # is an input to the search — they record the size of the term the cost leaves out, so a
        # geometry where it grows is visible instead of silent. See THE COST above.
        "in_cell_transit_m": round(
            sum(states[order[k]][combo[k]]["internal_transit_m"] for k in range(n)), 2),
        "in_cell_transit_spread_m": round(
            sum(max(st["internal_transit_m"] for st in states[i])
                - min(st["internal_transit_m"] for st in states[i]) for i in range(n)), 2),
        "inter_cell_transit_m": round(inter_m, 2),
        "largest_inter_cell_transit_m": round(max((l["length_m"] for l in legs), default=0.0), 2),
        "non_adjacent_handover_count": non_adjacent,
        "entry_boundary_m": round(entry_m, 2) if math.isfinite(entry_m) else None,
        "return_boundary_m": round(exit_m, 2) if math.isfinite(exit_m) else None,
        "transition_evaluations": tstats["evaluations"],
        "transition_cache_hits": tstats["cache_hits"],
        "boundary_evaluations": bstats["evaluations"],
    }
    return plan, diag


def _held_karp(n, step, enter, leave, bound=None, improve=None):
    """Held-Karp over (visited set, current cell, current orientation), with an optional bound.

    `dp[mask][(i, si)]` is the cheapest way to have covered exactly the cells in `mask`, finishing
    inside cell i flown in state si. The boundary conditions sit at the two ends: the entry
    connector is the seed of every one-cell state, and the return connector is added once at the
    full mask. Both are therefore optimised over rather than bolted on, which is the whole point
    of including them — the first cell is chosen knowing where the vehicle enters the survey, and
    the last knowing where it must leave for.

    `bound(cost_so_far, mask, i, si, j, sj)` may answer True to say a partial sequence provably
    cannot beat a known achievable tour; the caller supplies it and owns its admissibility, and
    `improve(total)` is called back with every complete tour found so that bound can tighten as
    the search runs. States
    are expanded in sorted (cell, orientation) order and ties are kept at the first sequence
    found, so equal-cost plans always resolve to the same one and the route hash is stable.

    Returns (order, combo, best_total) or (None, None, inf) when no complete tour survives."""
    full = (1 << n) - 1
    dp = [{} for _ in range(1 << n)]
    for i in range(n):
        for si in range(4):
            c = enter(i, si)
            if math.isfinite(c):
                dp[1 << i][(i, si)] = (c, None)
    for mask in range(1 << n):
        cur = dp[mask]
        if not cur:
            continue
        rest = [j for j in range(n) if not (mask >> j) & 1]
        if not rest:
            continue
        for (i, si), (cost_so_far, _back) in sorted(cur.items()):
            for j in rest:
                nmask = mask | (1 << j)
                nxt = dp[nmask]
                for sj in range(4):
                    if bound is not None and bound(cost_so_far, mask, i, si, j, sj):
                        continue
                    add = step(i, si, j, sj)
                    if not math.isfinite(add):
                        continue
                    total = cost_so_far + add
                    key = (j, sj)
                    have = nxt.get(key)
                    if have is None or total < have[0] - 1e-9:
                        nxt[key] = (total, (mask, i, si))
                        # A complete sequence is an ACHIEVABLE tour, so it tightens the bound
                        # the rest of the search is pruned against — the earlier the incumbent
                        # improves, the fewer candidates ever have to be measured.
                        if improve is not None and nmask == full:
                            improve(total + leave(j, sj))
    best_key, best_total = None, None
    for (i, si), (cost_so_far, _back) in sorted(dp[full].items()):
        total = cost_so_far + leave(i, si)
        if not math.isfinite(total):
            continue
        if best_total is None or total < best_total - 1e-9:
            best_total, best_key = total, (i, si)
    if best_key is None:
        return None, None, float("inf")
    order, combo = [], []
    mask, key = full, best_key
    while key is not None:
        i, si = key
        order.append(i)
        combo.append(si)
        back = dp[mask][key][1]
        mask = None if back is None else back[0]
        key = None if back is None else (back[1], back[2])
    order.reverse()
    combo.reverse()
    return order, combo, best_total


def _bcd_completion_bounds(n, step_lb, leave_lb):
    """`hlb[mask][(i, si)]` — the cheapest LOWER-BOUND cost of covering every cell NOT in `mask`,
    starting from cell i flown in state si, and then leaving the survey.

    A backward Held-Karp over the bound alone: pure arithmetic, not one safety probe, not one
    transition drawn. It is what makes the exact search cheap — for any partial sequence it says
    precisely how little the REST could conceivably cost, so a branch whose measured cost so far
    plus that remainder already loses to a known achievable tour can be abandoned before its next
    hand-over is ever measured. Because every term under-estimates, no branch that could have won
    is ever abandoned.

    The same table also yields the bound-optimal tour itself (each entry keeps the successor that
    achieved it), which is measured once to seed the incumbent."""
    full = (1 << n) - 1
    hlb = [dict() for _ in range(1 << n)]
    for i in range(n):
        for si in range(4):
            hlb[full][(i, si)] = (leave_lb(i, si), None)
    for mask in range(full - 1, -1, -1):
        rest = [j for j in range(n) if not (mask >> j) & 1]
        cur = hlb[mask]
        for i in range(n):
            if not (mask >> i) & 1:
                continue                     # you can only stand in a cell already covered
            for si in range(4):
                best = None
                for j in rest:
                    nxt = hlb[mask | (1 << j)]
                    for sj in range(4):
                        ahead = nxt.get((j, sj))
                        if ahead is None:
                            continue
                        v = step_lb(i, si, j, sj) + ahead[0]
                        if best is None or v < best[0] - 1e-12:
                            best = (v, (j, sj))
                if best is not None:
                    cur[(i, si)] = best
    return hlb


def _bcd_exact_plan(n, step, enter, leave, step_lb, enter_lb, leave_lb):
    """The globally optimal cell order + orientation for the cost in `_bcd_cell_plan`.

    THE SEARCH IS EXACT; THE BOUND ONLY DECIDES WHAT GETS MEASURED. A plain Held-Karp would ask
    the transition oracle for every one of the n·(n-1)·16 cell/orientation pairs, and each answer
    is a real safety-proven path (F2, or F4's local Dijkstra, or an aligned staircase) — on a
    seven-cell survey that is 672 measurements and about a second, for a decision that ends up
    using six of them. So the search runs in three stages, and its answer is identical to the
    plain one:

      1. A backward Held-Karp on the LOWER BOUND alone (`_bcd_completion_bounds`) — straight-line
         distance plus the exactly-known topology and reversal penalties. No oracle calls at all.
         It gives both a bound-optimal sequence and, for every partial sequence, the least the
         remainder could cost.
      2. That bound-optimal sequence is MEASURED for real. Its true cost is achievable, so it is a
         valid upper bound on the optimum and becomes the incumbent to beat. On the eight
         regression geometries the bound is within a metre of the truth, so this is usually
         already the answer — it just has not been proved yet.
      3. Held-Karp on the TRUE measured costs, with a branch abandoned as soon as
         `measured cost so far + bound on the next hop + bound on everything still to come`
         exceeds the incumbent, which tightens further every time a complete sequence is found.
         Nothing that could have won is discarded, and what is never reached is never measured.

    The result is whichever of the incumbent and stage 3 is cheaper, which is the true optimum.

    A survey where no complete sequence can be routed at all falls back to the stable geometric
    cell order rather than refusing: generation still has to produce a route, and every transition
    it emits is proven safe by `_aligned_transition` regardless of the order it is asked for."""
    hlb = _bcd_completion_bounds(n, step_lb, leave_lb)
    seed = None
    for i in range(n):
        for si in range(4):
            head = hlb[1 << i].get((i, si))
            if head is None:
                continue
            v = enter_lb(i, si) + head[0]
            if seed is None or v < seed[0] - 1e-12:
                seed = (v, i, si)
    if seed is None:                                     # not even the bound admits a sequence
        return list(range(n)), [0] * n, "geometric-fallback"
    lb_order, lb_combo = [seed[1]], [seed[2]]
    mask, key = 1 << seed[1], (seed[1], seed[2])
    while True:
        nxt = hlb[mask][key][1]
        if nxt is None:
            break
        lb_order.append(nxt[0])
        lb_combo.append(nxt[1])
        mask |= 1 << nxt[0]
        key = nxt

    def true_total(order, combo):
        total = enter(order[0], combo[0])
        for k in range(n - 1):
            total += step(order[k], combo[k], order[k + 1], combo[k + 1])
        return total + leave(order[-1], combo[-1])

    lb_true = true_total(lb_order, lb_combo)
    incumbent = [lb_true]

    def improve(total):
        if total < incumbent[0]:
            incumbent[0] = total

    def bound(cost_so_far, mask, i, si, j, sj):
        if not math.isfinite(incumbent[0]):
            return False
        ahead = hlb[mask | (1 << j)].get((j, sj))
        if ahead is None:
            return True                                  # no way to finish from there at all
        return cost_so_far + step_lb(i, si, j, sj) + ahead[0] > incumbent[0] + 1e-9

    order, combo, total = _held_karp(n, step, enter, leave, bound=bound, improve=improve)
    if order is None or total > lb_true - 1e-9:
        return lb_order, lb_combo, "exact-held-karp"
    return order, combo, "exact-held-karp"


def _bcd_heuristic_plan(n, cells, adjacency, step, enter, leave, step_lb):
    """Deterministic topology-aware construction + bounded improvement, for surveys with more
    cells than the exact search is affordable for.

    CONSTRUCTION. Start from the (cell, orientation) the survey is cheapest to enter at, then
    repeatedly take the cheapest admissible successor, scored with a one-step LOOKAHEAD: the
    measured hand-over into the candidate PLUS the cheapest LOWER BOUND on a hand-over out of it
    to anything still unvisited (or the return connector, when it would be last). Lookahead is
    what stops the construction walking into a corner it then has to cross the survey to escape;
    bounding it rather than measuring it keeps the construction linear in oracle calls.

    ADMISSIBLE SUCCESSORS are the candidate's BCD neighbours plus the
    BCD_HEURISTIC_EXTRA_NEIGHBOURS nearest remaining cells by centroid, so the branching factor
    is bounded and the topology is what mostly decides where the route goes next. If that set is
    empty (every neighbour already covered) the whole remaining set is admissible, so the
    construction can never dead-end.

    IMPROVEMENT. Bounded passes of segment reversal (2-opt on the cell order) and per-cell
    re-orientation, each accepted only on a strict improvement, so it terminates and is
    reproducible."""
    centroids = []
    for cell in cells:
        us = [f["rot"][e][0] for f in cell for e in (0, 1)]
        vs = [f["sweep"] for f in cell]
        centroids.append((sum(us) / len(us), sum(vs) / len(vs)))

    def near(i, remaining):
        pool = sorted(remaining, key=lambda j: (round(math.hypot(centroids[j][0] - centroids[i][0],
                                                                 centroids[j][1] - centroids[i][1]),
                                                      3), j))
        allowed = [j for j in pool if j in adjacency[i]]
        allowed += [j for j in pool if j not in adjacency[i]][:BCD_HEURISTIC_EXTRA_NEIGHBOURS]
        return allowed or pool

    start = min(((enter(i, si), i, si) for i in range(n) for si in range(4)),
                key=lambda t: (round(t[0], 6), t[1], t[2]))
    order, combo = [start[1]], [start[2]]
    remaining = set(range(n)) - {start[1]}
    while remaining:
        i, si = order[-1], combo[-1]
        best = None
        for j in near(i, remaining):
            for sj in range(4):
                add = step(i, si, j, sj)
                if not math.isfinite(add):
                    continue
                after = remaining - {j}
                if after:
                    ahead = min((step_lb(j, sj, k, sk) for k in near(j, after)
                                 for sk in range(4)), default=float("inf"))
                else:
                    ahead = leave(j, sj)
                if not math.isfinite(ahead):
                    ahead = 0.0
                key = (round(add + ahead, 6), round(add, 6), j, sj)
                if best is None or key < best[0]:
                    best = (key, j, sj)
        if best is None:
            j = min(remaining)
            best = ((0.0, 0.0, j, 0), j, 0)
        order.append(best[1])
        combo.append(best[2])
        remaining.discard(best[1])

    def total(order_, combo_):
        t = enter(order_[0], combo_[0])
        for k in range(n - 1):
            t += step(order_[k], combo_[k], order_[k + 1], combo_[k + 1])
        return t + leave(order_[-1], combo_[-1])

    best_total = total(order, combo)
    for _ in range(BCD_LOCAL_IMPROVEMENT_PASSES):
        improved = False
        for a in range(n - 1):
            for b in range(a + 1, n):
                cand_order = order[:a] + order[a:b + 1][::-1] + order[b + 1:]
                cand_combo = combo[:a] + combo[a:b + 1][::-1] + combo[b + 1:]
                for pos in range(n):
                    for s in range(4):
                        trial = list(cand_combo)
                        trial[pos] = s
                        t = total(cand_order, trial)
                        if t < best_total - 1e-6:
                            order, combo, best_total, improved = cand_order, trial, t, True
        if not improved:
            break
    return order, combo, "topology-aware-heuristic"


def _survey_frame_coverage(grid, spacing, angle_deg, pass_kind="primary",
                           entry_anchor=None, exit_anchor=None):
    """Generate ONE coverage pass: straight, survey-angle-aligned lane fragments joined by
    survey-frame-aligned transitions. Returns (coords_deg, diagnostics).

    Traversal is a boustrophedon over the CELLS of the sweep (see _bcd_cells): the fragments are
    grouped into maximal regions whose sweep topology does not change, each cell is covered
    completely before the route moves to the next, and WITHIN a cell the order is exactly what it
    always was — lanes in sweep order, fragments within a lane in increasing U order, traversal
    direction alternating per lane. Coverage advances monotonically through the sweep WITHIN EACH
    CELL rather than globally, because global row monotonicity is what forced the route to bridge
    around an exclusion on every lane it split.

    WHICH cell is covered when, and which of the four legal traversals of it is flown, is chosen
    by `_bcd_cell_plan` from the real safe transition distances between the cells. That is the one
    semantic that changed most recently, and it changes ORDER ONLY — the lane family, the lane
    spacing, the survey angle, the clipping, the fragments themselves, cell membership, the
    survey-frame alignment of every coverage leg and the safety predicate every transition must
    satisfy are all untouched.

    `entry_anchor` / `exit_anchor` are the boundary conditions of that ordering: the planning
    coordinate the survey is ENTERED from (the end of the approach chain, or the planning Home)
    and the one it is left TOWARDS (the first return waypoint, or Home). They are read only — no
    approach or return geometry is generated, moved or reordered here — and they exist so the
    first cell is not chosen in ignorance of where the vehicle arrives from, nor the last in
    ignorance of where it must leave for. Omitting them simply drops those two terms.

    Fragments an exclusion split apart are NOT joined straight through the forbidden gap: they are
    separate coverage pieces, and the transition between them is built by _aligned_transition,
    which must prove every leg safe or fail closed.

    Raises ConnectorError (via the generic fallback) when a transition cannot be made safe at
    all — generation refuses rather than emitting an unsafe leg."""
    frame = _SurveyFrame(grid, angle_deg)
    fragments, skipped = _lane_fragments(frame, spacing)
    diag = {
        "pass_kind": pass_kind,
        "angle_deg": float(angle_deg) % 360.0,
        "fragment_count": len(fragments),
        "cell_count": 0,
        "same_lane_bridge_count": 0,
        "cell_handover_count": 0,
        "transition_vertices": [],
        "transition_counts": {"direct_transit": 0, "shortest_safe_transit": 0, "direct": 0,
                              "orthogonal": 0, "bypass": 0, "fallback": 0},
        "skipped_short_fragment_count": skipped["count"],
        "skipped_short_fragment_length_m": round(skipped["length_m"], 2),
        "minimum_useful_fragment_m": round(_min_useful_fragment_m(spacing), 2),
        "fragments": [],
    }
    if not fragments:
        return [], diag

    # WHICH CELL, WHEN, AND WHICH WAY ROUND — chosen by _bcd_cell_plan.
    #
    # WITHIN a cell nothing changed: the fragments are flown lane by lane through the sweep, the
    # along-U direction flipping on every lane that actually PRODUCED coverage (not on every
    # nominal lane offset — a lane offset falling outside the region contributes nothing and must
    # not consume a direction flip). That is exactly the ported generator's boustrophedon, and it
    # is what keeps the survey Manhattan: straight parallel passes at the survey angle, at the
    # configured lane spacing, alternating deterministically.
    #
    # What the plan decides is the two whole-cell bits the sweep leaves free — the ORDER the cells
    # are covered in, and which of the four legal traversals of that same pattern each cell is
    # flown with — and it decides them on the REAL safe transition distances between the cells
    # plus the survey-entry and return connectors the route will actually pay for. Coverage
    # fragments are an input to that choice and are never altered by it.
    #
    # The along-U alternation therefore no longer has to CARRY ACROSS a cell boundary to make a
    # hand-over cheap: every cell's entry corner is chosen outright, which is strictly more
    # freedom than inheriting the running direction, and the hand-over is measured rather than
    # assumed.
    cells = _bcd_cells(fragments)
    cell_plan, plan_diag = _bcd_cell_plan(frame, cells, spacing,
                                          entry_anchor=entry_anchor, exit_anchor=exit_anchor)
    ordered = []
    for cell_id, state in cell_plan:
        for frag in state["fragments"]:
            ordered.append({**frag, "cell_index": cell_id})
    diag["cell_count"] = len(cells)
    diag["cell_plan"] = plan_diag

    coords = []
    base_sweep = min(f["sweep"] for f in ordered)
    for i, frag in enumerate(ordered):
        entry = frame.rot_to_deg(frag["entry_rot"])
        exit_ = frame.rot_to_deg(frag["exit_rot"])
        if not coords:
            coords.append(entry)
        else:
            path, category = _aligned_transition(
                frame, coords[-1], entry,
                in_dir=ordered[i - 1]["dir"], out_dir=frag["dir"])
            diag["transition_counts"][category] += 1
            coords.extend(path[1:])
            # The BEND POINTS of a transition are semantic turn points in exactly the sense a
            # fragment boundary is: each one is the corner where a proven U leg becomes a proven
            # V leg. Cleanup must not merge one into its neighbour — a fragment end and the bend
            # a fraction of a metre from it (fragment ends sit COVERAGE_EDGE_INSET_M inside the
            # region, so that gap is routinely sub-metre) would collapse to a single point, and
            # the L it belonged to would become one arbitrary-angle diagonal. Recorded here and
            # handed to cleanup as anchors, so the survey-frame alignment the transition was
            # built to satisfy survives the cleanup that follows.
            for p in path[1:-1]:
                diag["transition_vertices"].append([round(p[0], 7), round(p[1], 7)])
            # A transition between two fragments of the SAME nominal lane means the route left one
            # half of a split lane, went around the exclusion and came back to the other half.
            # WITHIN a cell that is the oscillation the decomposition exists to remove, and the
            # count must be zero. BETWEEN cells it is the single, intended hand-over from one
            # column beside an obstacle to the other — the sweep has to cross the obstacle exactly
            # once, and doing it at a shared lane end is the cheapest place. The two are counted
            # separately so the guarantee is measured on the real route and not blurred.
            if ordered[i - 1]["row"] == frag["row"]:
                if ordered[i - 1]["cell_index"] == frag["cell_index"]:
                    diag["same_lane_bridge_count"] += 1
                else:
                    diag["cell_handover_count"] += 1
        coords.append(exit_)
        diag["fragments"].append({
            "pass_kind": pass_kind, "fragment_index": i, "point_count": 2,
            "cell_index": frag["cell_index"],
            "cell_ascending": frag["cell_ascending"],
            "sweep_coordinate": round(frag["sweep"] - base_sweep, 2),
            "length_m": round(frag["length_m"], 2),
            "start": [round(entry[0], 7), round(entry[1], 7)],
            "end": [round(exit_[0], 7), round(exit_[1], 7)],
        })

    # row_index ranks fragments by sweep coordinate across the WHOLE pass. It is a description of
    # where a fragment sits in the sweep, not of when it is flown — with a cellular decomposition
    # those are deliberately no longer the same thing.
    for rank, idx in enumerate(sorted(range(len(diag["fragments"])),
                                      key=lambda k: diag["fragments"][k]["sweep_coordinate"])):
        diag["fragments"][idx]["row_index"] = rank
    # `fragment_reorders` counts fragments visited out of sweep order WITHIN A CELL, in that
    # cell's own sweep direction. Global sweep monotonicity is no longer the correct semantic:
    # moving to a lower sweep row to begin the next cell is the whole point of the decomposition
    # and is not a scramble, and a cell entered from above is legitimately swept downwards.
    # Inside a cell, coverage must still advance monotonically — that guarantee is unchanged.
    reorders = 0
    prev_by_cell = {}
    for f in diag["fragments"]:
        c = f["cell_index"]
        prev = prev_by_cell.get(c)
        if prev is not None:
            delta = f["sweep_coordinate"] - prev
            if not f["cell_ascending"]:
                delta = -delta
            if delta < -CLEANUP_MIN_SPACING_M:
                reorders += 1
        prev_by_cell[c] = f["sweep_coordinate"]
    diag["fragment_reorders"] = reorders
    return _dedup(coords), diag


def _coverage_leg_split(segments, coverage_diags, grid, pass_angle):
    """Classify every leg of the COVERAGE segments as a survey FRAGMENT leg or a TRANSITION leg,
    and return the survey-frame alignment tallies for each, separately.

    WHY THE SPLIT EXISTS
    --------------------
    A `primary`/`secondary` segment is not all coverage. It is the concatenation of the clipped
    lane fragments — the actual sonar passes — and the inter-fragment transitions that carry the
    vessel from one to the next. Tallying alignment over the whole polyline therefore charged a
    transit leg to the COVERAGE contract: a straight, provably safe, arbitrary-angle hop between
    two finished fragments was reported as an arbitrary-angle survey lane, which it is not.

    The split is exact, not inferred from geometry. `_survey_frame_coverage` records the two
    endpoints of every fragment it emitted, and those endpoints are cleanup ANCHORS (see
    _fragment_anchors), so they survive into the final coordinates unchanged and at full
    precision. A leg is a fragment leg exactly when its endpoint pair IS a recorded fragment's
    (start, end) — everything else in the segment is, by construction, transition.

    Returns {"coverage_aligned", "coverage_unaligned", "transition_aligned",
             "transition_unaligned", "coverage_fragment_leg_m", "transition_leg_m"}."""
    pairs = set()
    for d in coverage_diags or []:
        for f in d.get("fragments") or []:
            pairs.add((_rk(f["start"]), _rk(f["end"])))
    out = {"coverage_aligned": 0, "coverage_unaligned": 0,
           "transition_aligned": 0, "transition_unaligned": 0,
           "coverage_fragment_leg_m": 0.0, "transition_leg_m": 0.0}
    for seg in segments:
        if seg["kind"] not in _COVERAGE_KINDS:
            continue
        cs = seg["coordinates"]
        angle = pass_angle[seg["kind"]]
        for i in range(len(cs) - 1):
            a, b = cs[i], cs[i + 1]
            ap = grid.to_proj.transform(a[0], a[1])
            bp = grid.to_proj.transform(b[0], b[1])
            fragment = (_rk(a), _rk(b)) in pairs
            # Geodesic, via the same _path_length_m every other length metric uses, so these two
            # numbers add up to `metrics.coverage_length_m` exactly rather than approximately.
            out["coverage_fragment_leg_m" if fragment else "transition_leg_m"] +=                 _path_length_m([a, b])
            cls = _survey_align_class(ap, bp, angle)
            if cls == "short":
                continue        # no meaningful heading — counted in neither bucket
            key = ("coverage_" if fragment else "transition_") + (
                "aligned" if cls in ("U", "V") else "unaligned")
            out[key] += 1
    out["coverage_fragment_leg_m"] = round(out["coverage_fragment_leg_m"], 2)
    out["transition_leg_m"] = round(out["transition_leg_m"], 2)
    return out


def _fragment_anchors(diag):
    """The semantic points of a coverage pass, as cleanup anchors.

    Two kinds, and both must survive cleanup intact:

      * LANE-FRAGMENT ENDPOINTS — a survey line begins or ends there, frequently right on the
        exclusion edge;
      * TRANSITION BEND POINTS — the corner where a proven U leg becomes a proven V leg. A
        fragment end and the bend next to it are routinely less than a metre apart (fragment ends
        sit COVERAGE_EDGE_INSET_M inside the region), so without this the near-duplicate merge
        would drop the bend and turn the L into one arbitrary-angle diagonal.

    So cleanup may still remove genuinely redundant points, but never a point that carries the
    survey-frame geometry the generator proved safe."""
    if not diag:
        return None
    pts = []
    for f in diag.get("fragments") or []:
        pts.append(f["start"])
        pts.append(f["end"])
    pts.extend(diag.get("transition_vertices") or [])
    return pts or None


def _route_waypoints(coords):
    """[[lng,lat],...] execution path -> mission-contract route waypoints (route only)."""
    return [{"latitude": round(float(c[1]), 7), "longitude": round(float(c[0]), 7),
             "loiter_time_s": 0} for c in _dedup(coords)]


def _route_hash(route_waypoints):
    """The canonical route content hash — the SAME calculator the Pixhawk upload path uses
    (mission_contract.route_content_hash), so the finalized package's identity is byte-for-
    byte the one the MISSION_UPLOAD command is verified against. Not a second hash."""
    return mission_contract.route_content_hash(route_waypoints)


def _input_revision(inp):
    """A stable digest over EVERY generation-affecting normalized input. Lets validation
    confirm a submitted route was generated from the inputs it is being validated against
    (mirrors the frontend's inputRevision), and lets a stored draft know its route is stale.
    Deterministic: identical inputs → identical revision."""
    def rd(pt):
        return [round(float(pt[0]), 7), round(float(pt[1]), 7)] if pt else None
    canonical = {
        "b": [rd(p) for p in (inp.get("boundary") or [])],
        "z": [[rd(p) for p in z] for z in (inp.get("no_go_zones") or [])],
        "h": rd(inp.get("home")),
        "a": [rd(p) for p in (inp.get("approach_waypoints") or [])],
        "r": [rd(p) for p in (inp.get("return_waypoints") or [])],
        "c": inp.get("shoreline_clearance_m"),
        # No-go clearance is generation-affecting: it moves the exclusion the whole route is
        # routed around, so a change to it must outdate an existing route exactly like a
        # shoreline-clearance or lane-spacing change does.
        "ngc": inp.get("no_go_clearance_m"),
        "s": inp.get("lane_spacing_m"),
        "pa": inp.get("primary_angle_deg"),
        "d": inp.get("dual_pass"),
        "sa": inp.get("secondary_angle_deg"),
        "m": inp.get("route_start_mode"),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return "rev:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def normalize_generate_inputs(raw):
    """Validate + normalise a /api/planning/generate request body. Returns a dict of clean
    inputs; raises ValueError(list_of_errors joined) for anything unusable. Deterministic
    and network-free — the same inputs always produce the same errors."""
    raw = raw if isinstance(raw, dict) else {}
    errors = []

    boundary = _ring(raw.get("boundary"))
    if boundary is None:
        errors.append("A survey boundary polygon with at least 3 vertices is required.")

    clearance = _num(raw.get("shoreline_clearance_m", 0))
    if clearance is None or clearance < 0:
        errors.append("Shoreline clearance must be a number >= 0 metres.")
        clearance = 0.0

    # No-go clearance: the minimum routing distance from every drawn no-go polygon. ABSENT
    # (an older draft, an older caller) means "not stated" and takes the 5 m default; a
    # SUPPLIED value is validated, so an explicit 0 stays 0 (original geometry exclusion only).
    if raw.get("no_go_clearance_m") is None:
        no_go_clearance = DEFAULT_NO_GO_CLEARANCE_M
    else:
        no_go_clearance = _num(raw.get("no_go_clearance_m"))
        if no_go_clearance is None or no_go_clearance < 0:
            errors.append("No-go clearance must be a number >= 0 metres.")
            no_go_clearance = DEFAULT_NO_GO_CLEARANCE_M

    # Lane spacing: absent takes the 10 m default (same migration rule); a supplied value must
    # still be a positive number — an explicit 0 or -1 remains an error, never a silent 10.
    if raw.get("lane_spacing_m") is None:
        spacing = DEFAULT_LANE_SPACING_M
    else:
        spacing = _num(raw.get("lane_spacing_m"))
        if spacing is None or spacing <= 0:
            errors.append("Lane spacing must be a positive number of metres.")

    primary = _num(raw.get("primary_angle_deg", 0))
    if primary is None:
        errors.append("Primary survey angle must be a number of degrees.")
        primary = 0.0
    primary = primary % 360.0

    dual = bool(raw.get("dual_pass"))
    secondary = _num(raw.get("secondary_angle_deg"))
    if secondary is None:
        secondary = (primary + 90.0) % 360.0
    else:
        secondary = secondary % 360.0

    speed = _num(raw.get("survey_speed_mps"))
    if speed is not None and speed <= 0:
        errors.append("Survey speed, when supplied, must be a positive number of m/s.")
        speed = None

    zones = []
    raw_zones = raw.get("no_go_zones") or []
    if not isinstance(raw_zones, list):
        errors.append("no_go_zones must be a list of polygons.")
        raw_zones = []
    for i, z in enumerate(raw_zones):
        r = _ring(z)
        if r is None:
            errors.append(f"No-go zone {i + 1} is not a valid polygon (needs >= 3 vertices).")
        else:
            zones.append(r)

    home = _point_of(raw.get("home")) if raw.get("home") is not None else None

    # Approach waypoints (renamed from "transit"): operator-approved route INTO the survey,
    # visited in numbered order before coverage begins. MIGRATION: old drafts/callers that
    # still send `transit_waypoints` / `transit` are accepted transparently so a saved plan
    # never silently breaks — the new field wins when both are present.
    raw_approach = _first_present(
        raw.get("approach_waypoints"), raw.get("transit_waypoints"), raw.get("transit"))
    approach = []
    for i, p in enumerate(raw_approach or []):
        pt = _point_of(p)
        if pt is None:
            errors.append(f"Approach waypoint {i + 1} is not a valid [lng, lat] point.")
        else:
            approach.append(pt)

    # Return waypoints: operator-approved route OUT of the survey, back toward planning home.
    # A separate list — never an implicit reversal of the approach (the operator asks for a
    # reversed copy explicitly on the page). Migration keeps old spelling working too.
    raw_return = _first_present(raw.get("return_waypoints"), raw.get("return_wps"))
    ret = []
    for i, p in enumerate(raw_return or []):
        pt = _point_of(p)
        if pt is None:
            errors.append(f"Return waypoint {i + 1} is not a valid [lng, lat] point.")
        else:
            ret.append(pt)

    # Where the executed route begins. Default planning_home (prototype preference); if the
    # requested mode cannot apply (no home, or first_approach with no approach WPs) the route
    # falls back at generation time, and a warning is emitted there — not an error here.
    start_mode = str(raw.get("route_start_mode") or "planning_home").lower()
    if start_mode not in ROUTE_START_MODES:
        start_mode = "planning_home"

    if errors:
        raise ValueError("; ".join(errors))

    return {
        "boundary": boundary,
        "shoreline_clearance_m": float(clearance),
        "no_go_clearance_m": float(no_go_clearance),
        "lane_spacing_m": float(spacing),
        "primary_angle_deg": float(primary),
        "dual_pass": dual,
        "secondary_angle_deg": float(secondary),
        "survey_speed_mps": speed,
        "no_go_zones": zones,
        "home": home,
        "route_start_mode": start_mode,
        "approach_waypoints": approach,
        "return_waypoints": ret,
    }


def _flatten_segments(segments):
    """Concatenate typed segments into one ordered route, dropping the duplicate join point
    where a segment's start coincides with the previous segment's end, and stamp each segment
    with the execution-sequence range it occupies. Guarantees, by construction, that the flat
    route equals the segment flattening and that segment[i].end is segment[i+1].start — no
    invisible straight jump between arrays.

    Returns (route_coords, original_execution_order). Segments are mutated in place with
    start_execution_seq / end_execution_seq."""
    route = []                 # [[lng,lat], ...]
    order = []                 # [{execution_seq, latitude, longitude, source_segment_id, ...}]
    for seg in segments:
        seg["start_execution_seq"] = None
        for idx, c in enumerate(seg["coordinates"]):
            if route and _close(route[-1], c):
                # Shared join with the previous segment's end — no new route point, but this
                # segment still STARTS at that shared execution index.
                if seg["start_execution_seq"] is None:
                    seg["start_execution_seq"] = len(route) - 1
                continue
            if seg["start_execution_seq"] is None:
                seg["start_execution_seq"] = len(route)
            route.append([float(c[0]), float(c[1])])
            order.append({
                "execution_seq": len(route) - 1,
                "latitude": round(float(c[1]), 7),
                "longitude": round(float(c[0]), 7),
                "source_segment_id": seg["segment_id"],
                "source_segment_kind": seg["kind"],
                "source_index": idx,
            })
        seg["end_execution_seq"] = max(0, len(route) - 1)
    return route, order


def generate_survey(raw_inputs, max_route_waypoints=None):
    """Generate ONE safe, unambiguous, fully segmented survey mission from the operator's
    planning inputs.

    Execution order (each gap is an EXPLICIT connector segment, never an implicit jump):
      START/APPROACH → APPROACH WAYPOINTS → SURVEY ENTRY CONNECTOR → PRIMARY COVERAGE →
      PASS TRANSITION (dual) → SECONDARY COVERAGE → RETURN CONNECTOR → RETURN APPROACH →
      FINAL HOME CONNECTOR.

    Every connector is produced by the shared _NavGrid.safe_connector, so a coverage lane
    turn, a no-go-split bridge or a pass transition can never leave the navigable region on
    an asymmetric/concave boundary. Returns the finalized operator-survey-plan-v1 package:
    typed `segments` (with execution-seq ranges), the flat `route_waypoints`, the canonical
    `route_hash`, `original_execution_order`, echoed `planning_inputs`, `navigable_boundary`,
    `metrics`, `intersections` and `warnings`. Deterministic. Raises ValueError /
    ConnectorError / DisconnectedNavigableError for anything unroutable, never a silent bad
    connector."""
    _require_available()
    inp = normalize_generate_inputs(raw_inputs)

    boundary = inp["boundary"]
    clearance = inp["shoreline_clearance_m"]
    no_go_clearance = inp["no_go_clearance_m"]
    spacing = inp["lane_spacing_m"]
    zones = inp["no_go_zones"]
    home = inp["home"]
    approach = inp["approach_waypoints"]
    returns = inp["return_waypoints"]
    warnings = []

    inset = _inset_polygon(boundary, clearance)
    if inset is None or inset.is_empty:
        raise ValueError(
            "The survey boundary is empty after applying the shoreline clearance — "
            "reduce the clearance or enlarge the boundary.")

    grid = _NavGrid(boundary, clearance, zones, step_m=spacing, no_go_clearance=no_go_clearance)
    if not grid.buffer_valid:
        raise ValueError(
            f"The no-go clearance of {no_go_clearance} m could not be applied — buffering the "
            f"no-go zones produced invalid geometry. Check the drawn zones or reduce the "
            f"no-go clearance.")
    if grid.empty:
        raise ValueError(
            "The navigable area is empty after applying the shoreline clearance and the "
            "no-go zones with their no-go clearance — reduce the clearance(s)/zones or "
            "enlarge the boundary.")
    if grid.disconnected:
        raise DisconnectedNavigableError(
            f"The shoreline clearance and the no-go zones (with a {no_go_clearance} m no-go "
            f"clearance) split the survey into {len(grid.components)} disconnected navigable "
            f"regions. Survey generation currently requires one connected navigable region — "
            f"split the survey into separate missions, or adjust the clearance / no-go zones.")

    # The BUFFERED exclusion as drawable rings. Derived ONCE from the grid, so the exclusion the
    # coverage lanes are clipped against, the exclusion every connector is validated against and
    # the exclusion the Plan page draws are the same geometry. The operator's ORIGINAL rings are
    # untouched and are what `planning_inputs.no_go_zones` carries.
    exclusion_rings = grid.exclusion_rings_deg()

    # ── Coverage passes ──────────────────────────────────────────────────────────────────
    # Survey-frame coverage: the ported lane family, clipped to the approved region into straight
    # U-parallel fragments, joined by survey-frame-aligned transitions (see _survey_frame_coverage).
    # `repair_path` still runs behind it as defence in depth — it re-checks every emitted hop and
    # repairs (or fails on) anything unsafe, exactly as before.
    #
    # DUAL PASS: each pass builds its OWN _SurveyFrame from its own angle, so the secondary pass's
    # fragments and transitions are orthogonal in the secondary frame (survey_angle + 90° by
    # default). No first-pass orientation can leak into the second pass's connector geometry.
    # THE ORDERING'S BOUNDARY CONDITIONS. Where the survey is entered from, and where it is left
    # towards, are what the survey-entry and return connectors are built between — so the cell
    # ORDER has to know them or the first cell can be chosen on the far side of the survey from
    # the approach (196.7 m of survey-entry connector on the operator's own large polygon, for a
    # cell that could have been entered in 40.5 m). These are read-only inputs: no approach or
    # return waypoint is generated, moved or reordered by the ordering.
    survey_entry_anchor = (approach[-1] if approach else home)
    survey_exit_anchor = (returns[0] if returns else home)
    primary_raw, primary_diag = _survey_frame_coverage(
        grid, spacing, inp["primary_angle_deg"], pass_kind="primary",
        entry_anchor=survey_entry_anchor,
        # DUAL PASS: the primary hands over to the SECONDARY pass, not to the return chain, and
        # where the secondary begins is not known until the primary has been ordered. Rather than
        # guess, the primary is ordered on its entry boundary alone and the secondary picks up the
        # primary's real end point below — no circular dependency, and no invented anchor.
        exit_anchor=None if inp["dual_pass"] else survey_exit_anchor)
    if len(primary_raw) < 2:
        raise ValueError(
            "No coverage route could be generated — the navigable area may be too small "
            "for the chosen lane spacing, or fully blocked by the no-go zones and their "
            "no-go clearance.")
    primary_coords = grid.repair_path(primary_raw)

    secondary_coords = None
    secondary_diag = None
    intersections = []
    if inp["dual_pass"]:
        secondary_raw, secondary_diag = _survey_frame_coverage(
            grid, spacing, inp["secondary_angle_deg"], pass_kind="secondary",
            entry_anchor=primary_coords[-1], exit_anchor=survey_exit_anchor)
        if len(secondary_raw) < 2:
            warnings.append("Dual pass requested, but the secondary pass produced no route; "
                            "only the primary pass was generated.")
            secondary_diag = None
        else:
            secondary_coords = grid.repair_path(secondary_raw)
            intersections = _pass_intersections(primary_coords, secondary_coords)

    coverage_diags = [d for d in (primary_diag, secondary_diag) if d]
    # The minimum-useful-fragment rule is a COVERAGE decision, so it is stated to the operator
    # rather than only counted — see MIN_FRAGMENT_LANE_FRACTION.
    skipped_n = sum(d["skipped_short_fragment_count"] for d in coverage_diags)
    if skipped_n:
        skipped_m = round(sum(d["skipped_short_fragment_length_m"] for d in coverage_diags), 2)
        warnings.append(
            f"{skipped_n} coverage fragment(s) shorter than "
            f"{primary_diag['minimum_useful_fragment_m']} m ({skipped_m} m of survey line in "
            f"total) were left out where the no-go clearance or the shoreline clipped a lane to a "
            f"sliver — they would have cost more turns than sonar coverage.")

    survey_entry = primary_coords[0]
    coverage_end = (secondary_coords[-1] if secondary_coords else primary_coords[-1])

    # ── Resolve the route-start mode (with honest fallbacks + warnings) ──────────────────
    start_mode = inp["route_start_mode"]
    if start_mode == "planning_home" and home is None:
        start_mode = "first_approach"
        warnings.append("Route start was set to planning home, but no planning home is set — "
                        "the route begins at the first approach waypoint (or survey entry).")
    if start_mode == "first_approach" and not approach and home is not None:
        # No approach points to start from, but a home exists — use it as the start anchor.
        start_mode = "planning_home"

    # ── EXECUTION ROUTE vs APPROVED TRANSIT GEOMETRY ─────────────────────────────────────
    # `segments` is the EXECUTION route: the ordered geometry that is flattened, hashed,
    # uploaded and flown. It is the only thing `_flatten_segments` ever sees.
    #
    # `planning_only_segments` is APPROVED TRANSIT geometry the plan contains but the execution
    # route deliberately does not carry. Today that is exactly one leg — planning Home → first
    # approach waypoint under `route_start_mode: first_approach`, where the operator has said
    # the mission STARTS at A1. The leg is generated and safety-checked identically to an
    # executed connector; it is what the Home corridor is anchored on and what proves Home is
    # connected to the survey. It is NEVER concatenated into the route and so can never move a
    # byte of the route hash.
    #
    # Choosing the route-start mode therefore changes WHERE EXECUTION BEGINS, and nothing about
    # which geometry is approved (see ROUTE_START_MODES).
    segments = []
    planning_only_segments = []
    seq = [0]
    pseq = [0]
    raw_segment_pts = [0]   # summed pre-cleanup segment points, for the route-quality metric

    def new_seg(kind, coords, planning_only=False, anchors=None):
        raw = _dedup(coords)
        if planning_only:
            pseq[0] += 1
            segment_id = f"pln-{pseq[0]:02d}-{kind}"
        else:
            seq[0] += 1
            segment_id = f"seg-{seq[0]:02d}-{kind}"
            # Route-quality counts the EXECUTION route only, so a planning-only leg is not
            # summed here (and its connector counters are restored below).
            raw_segment_pts[0] += len(raw)
        # PART 6 segment-specific policy: aggressive LOS for generated connectors, operator
        # waypoints preserved as anchors on approach/return, conservative dedup+collinear only
        # for coverage. require_inside matches the segment's own connector safety policy.
        # Coverage passes supply their lane-fragment endpoints as anchors: a fragment boundary is
        # a semantic point (a survey line starts or ends there, often on the exclusion edge), so
        # cleanup must never merge or shortcut across it.
        if anchors is None:
            anchors = (approach if kind == "approach"
                       else returns if kind == "return_approach" else None)
        cleaned = grid.clean_path(
            raw, require_inside=(kind in _REQUIRE_INSIDE_KINDS),
            anchors=anchors, aggressive=(kind in _AGGRESSIVE_KINDS))
        seg = {"segment_id": segment_id, "kind": kind,
               "coordinates": [[float(c[0]), float(c[1])] for c in cleaned],
               "length_m": round(_path_length_m(cleaned), 2),
               "raw_point_count": len(raw), "final_point_count": len(cleaned)}
        if planning_only:
            # Stated on the segment itself, so a consumer reading one segment in isolation can
            # never mistake approved planning geometry for something the vehicle will fly.
            seg["planning_only"] = True
        return seg

    def build_connector(a, b, kind, require_inside, planning_only=False):
        """The safe connector segment a→b, or None when a and b are the same point."""
        if _close(a, b):
            return None
        path = grid.safe_connector(a, b, require_inside=require_inside)
        if len(path) < 2:
            return None
        return new_seg(kind, path, planning_only=planning_only)

    def connect(a, b, kind, require_inside):
        """Append a connector segment a→b to the EXECUTION route."""
        seg = build_connector(a, b, kind, require_inside)
        if seg is not None:
            segments.append(seg)

    def chain_broken(code, message, exc):
        """A required leg of the approved Home↔survey network could not be routed. A hard,
        coded failure — never a widened region, an invented corridor or a silent omission."""
        return GeometryConsistencyError([
            {"code": code, "message": message, "detail": str(exc)}])

    # 1. HOME → APPROACH + APPROACH + SURVEY ENTRY CONNECTOR
    #
    # The Home → A1 connector is derived whenever a planning Home and approach waypoints both
    # exist, in BOTH route-start modes, because it is the approved geometry that joins Home to
    # the survey. The mode only decides whether it is EXECUTED:
    #   planning_home   → an executed `start_connector` (execution route + corridor source)
    #   first_approach  → a planning-only `home_transit_connector` (corridor source only)
    if approach:
        if home is not None:
            executed_start = (start_mode == "planning_home")
            kind = "start_connector" if executed_start else "home_transit_connector"
            # A planning-only leg must not colour the route-quality diagnostics, which describe
            # the uploaded route; the grid's connector counters are restored around it.
            counters = (grid.raw_connector_pts, grid.final_connector_pts,
                        grid.connector_len_before_m, grid.connector_len_after_m)
            try:
                seg = build_connector(home, approach[0], kind, require_inside=False,
                                      planning_only=not executed_start)
            except ConnectorError as exc:
                raise chain_broken(
                    "HOME_TO_APPROACH_DISCONNECTED",
                    "no safe route could be found between the planning Home and approach "
                    "waypoint A1 — the approved transit network does not reach the survey from "
                    "Home, so no Home corridor may be derived and the mission is refused. Move "
                    "the Home or A1, or adjust the no-go zones / no-go clearance between them",
                    exc)
            if seg is not None:
                if executed_start:
                    segments.append(seg)
                else:
                    planning_only_segments.append(seg)
                    (grid.raw_connector_pts, grid.final_connector_pts,
                     grid.connector_len_before_m, grid.connector_len_after_m) = counters
        # The approach polyline, each hop validated (a manually drawn line is not assumed
        # safe): safe hops kept straight, unsafe hops routed around no-go interiors.
        approach_path = [approach[0]]
        for a, b in zip(approach, approach[1:]):
            if grid.segment_is_safe(a, b, require_inside=False):
                approach_path.append(b)
            else:
                approach_path.extend(grid.safe_connector(a, b, require_inside=False)[1:])
        # A single approach WP is not a drawable polyline — it is already threaded as the
        # shared endpoint of the start/survey-entry connectors, so only emit the approach
        # SEGMENT when there are >= 2 points to draw. The A1 marker still renders from the
        # echoed planning_inputs.approach_waypoints regardless.
        if len(approach_path) >= 2:
            segments.append(new_seg("approach", approach_path))
        try:
            connect(approach[-1], survey_entry, "survey_entry_connector", require_inside=True)
        except ConnectorError as exc:
            raise chain_broken(
                "APPROACH_TO_SURVEY_DISCONNECTED",
                "no safe route could be found between the last approach waypoint and the survey "
                "entry inside the navigable area — the approach chain does not reach the survey "
                "geometry", exc)
    elif home is not None:
        # No approach WPs: planning home → survey entry is the single entry connector, and it is
        # executed in both modes (first_approach has no approach waypoint to start from and has
        # already fallen back to planning_home above).
        connect(home, survey_entry, "survey_entry_connector", require_inside=True)
    else:
        warnings.append("No planning home and no approach waypoints — the route begins "
                        "directly at the survey entry.")

    # 2. PRIMARY COVERAGE
    segments.append(new_seg("primary", primary_coords,
                            anchors=_fragment_anchors(primary_diag)))

    # 3. PASS TRANSITION + SECONDARY COVERAGE
    if secondary_coords:
        connect(primary_coords[-1], secondary_coords[0], "pass_transition", require_inside=True)
        segments.append(new_seg("secondary", secondary_coords,
                                anchors=_fragment_anchors(secondary_diag)))

    # 4. RETURN CONNECTOR + RETURN APPROACH + FINAL HOME CONNECTOR
    #
    # The return chain is the operator's own list and is INDEPENDENT of the approach — it is
    # never derived by reversing it (the operator asks for a reversed copy explicitly on the
    # Plan page, which populates `return_waypoints` and is then just an ordinary return list).
    # It is executed in both route-start modes: `first_approach` moves the START of execution,
    # not its end, so the uploaded route still finishes at the planning Home.
    if returns:
        try:
            connect(coverage_end, returns[0], "return_connector", require_inside=True)
        except ConnectorError as exc:
            raise chain_broken(
                "SURVEY_TO_RETURN_DISCONNECTED",
                "no safe route could be found between the end of the survey and return waypoint "
                "R1 inside the navigable area — the survey does not reach the return chain", exc)
        return_path = [returns[0]]
        for a, b in zip(returns, returns[1:]):
            if grid.segment_is_safe(a, b, require_inside=False):
                return_path.append(b)
            else:
                return_path.extend(grid.safe_connector(a, b, require_inside=False)[1:])
        if len(return_path) >= 2:
            segments.append(new_seg("return_approach", return_path))
        if home is not None:
            try:
                connect(returns[-1], home, "final_home_connector", require_inside=False)
            except ConnectorError as exc:
                raise chain_broken(
                    "RETURN_TO_HOME_DISCONNECTED",
                    "no safe route could be found between the last return waypoint and the "
                    "planning Home — the return chain does not reach Home, so the mission has no "
                    "approved way back", exc)
    elif home is not None:
        # No explicit return WPs: a safe generated connector straight back to planning home.
        try:
            connect(coverage_end, home, "return_connector", require_inside=True)
        except ConnectorError:
            warnings.append("No safe return route to the planning home could be found inside "
                            "the navigable area — the route ends at the last coverage waypoint.")
    else:
        warnings.append("No planning home is set — no return route was generated.")

    if home is not None and _point_in_any_zone(home, zones):
        warnings.append("The planning home lies inside a no-go zone.")
    if not approach:
        warnings.append("No approach waypoints are defined — the route enters the survey "
                        "directly.")

    # ── Flatten to the ordered route + provenance (the single source of the upload route) ──
    route_coords, original_execution_order = _flatten_segments(segments)
    route_coords = _dedup(route_coords)
    route_waypoints = _route_waypoints(route_coords)
    n = len(route_waypoints)

    if max_route_waypoints is not None and n > max_route_waypoints:
        warnings.append(
            f"Route has {n} waypoints, above the supported mission limit of "
            f"{max_route_waypoints} — it cannot be uploaded until made smaller "
            f"(increase lane spacing or shrink the survey area).")
    elif max_route_waypoints is not None and n > int(max_route_waypoints * 0.9):
        warnings.append(
            f"Route has {n} waypoints, approaching the supported mission limit of "
            f"{max_route_waypoints}.")

    coverage_kinds = ("primary", "secondary")
    connector_kinds = ("start_connector", "approach", "survey_entry_connector",
                       "pass_transition", "return_connector", "return_approach",
                       "final_home_connector")
    coverage_len = sum(s["length_m"] for s in segments if s["kind"] in coverage_kinds)
    transit_len = sum(s["length_m"] for s in segments if s["kind"] in connector_kinds)
    total_len = round(coverage_len + transit_len, 2)

    speed = inp["survey_speed_mps"] or DEFAULT_PLANNING_SPEED_MPS
    duration_s = round(total_len / speed, 1) if speed > 0 else None

    navigable_boundary = _navigable_rings_deg(boundary, clearance)
    metrics = {
        "boundary_area_m2": round(_polygon_area_m2(boundary), 2),
        "navigable_area_m2": round(abs(inset.area) if hasattr(inset, "area") else 0.0, 2),
        "waypoint_count": n,
        "total_length_m": total_len,
        "coverage_length_m": round(coverage_len, 2),
        "transit_length_m": round(transit_len, 2),
        "lane_spacing_m": spacing,
        "shoreline_clearance_m": clearance,
        "no_go_clearance_m": no_go_clearance,
        "primary_angle_deg": inp["primary_angle_deg"],
        "secondary_angle_deg": inp["secondary_angle_deg"] if inp["dual_pass"] else None,
        "dual_pass": inp["dual_pass"],
        "no_go_zone_count": len(zones),
        "approach_waypoint_count": len(approach),
        "return_waypoint_count": len(returns),
        "route_start_mode": start_mode,
        "survey_speed_mps": speed,
        "survey_speed_is_default": inp["survey_speed_mps"] is None,
        "estimated_duration_s": duration_s,
    }

    # ── Route-quality diagnostics (PART 7) — objective, inspectable, no vague score ─────────
    route_proj = [grid.to_proj.transform(c[0], c[1]) for c in route_coords]
    full_q = _route_quality(route_proj)
    # Coverage fragments come from the GENERATOR, which knows exactly which lane pieces it emitted
    # and at what sweep offset — more precise than re-deriving them from turn angles in the
    # flattened route, and unaffected by the survey-frame turns now being 90° rather than 180°.
    coverage_fragment_count = sum(d["fragment_count"] for d in coverage_diags)
    fragment_reorders = sum(d["fragment_reorders"] for d in coverage_diags)
    coverage_cell_count = sum(d["cell_count"] for d in coverage_diags)
    same_lane_bridges = sum(d["same_lane_bridge_count"] for d in coverage_diags)
    cell_handovers = sum(d["cell_handover_count"] for d in coverage_diags)
    coverage_fragments = [f for d in coverage_diags for f in d["fragments"]]
    # ── Survey-frame alignment of the FINAL coverage geometry (post-cleanup) ─────────────────
    # Each coverage segment is classified against ITS OWN pass angle. Sub-metre legs carry no
    # meaningful heading and are counted in neither bucket (see ALIGN_MIN_LEG_M).
    pass_angle = {"primary": inp["primary_angle_deg"], "secondary": inp["secondary_angle_deg"]}
    align = _coverage_leg_split(segments, coverage_diags, grid, pass_angle)
    aligned_n = align["coverage_aligned"]
    unaligned_n = align["coverage_unaligned"]
    transition_totals = {k: sum(d["transition_counts"][k] for d in coverage_diags)
                         for k in ("direct_transit", "shortest_safe_transit", "direct",
                                   "orthogonal", "bypass", "fallback")}
    final_seg_pts = sum(s.get("final_point_count", len(s["coordinates"])) for s in segments)
    los_removed = grid.raw_connector_pts - grid.final_connector_pts
    seg_removed = raw_segment_pts[0] - final_seg_pts
    raw_waypoint_count = n + max(0, los_removed) + max(0, seg_removed)
    route_quality = {
        "raw_waypoint_count": raw_waypoint_count,
        "final_waypoint_count": n,
        "removed_waypoint_count": raw_waypoint_count - n,
        "raw_connector_waypoint_count": grid.raw_connector_pts,
        "final_connector_waypoint_count": grid.final_connector_pts,
        "connector_length_before_m": round(grid.connector_len_before_m, 2),
        "connector_length_after_m": round(grid.connector_len_after_m, 2),
        "coverage_fragment_count": coverage_fragment_count,
        "fragment_reorders": fragment_reorders,
        # Boustrophedon cellular decomposition (see _bcd_cells). `coverage_cell_count` is how many
        # topologically distinct regions the sweep was split into — 1 whenever nothing splits a
        # lane. `same_lane_obstacle_bridge_count` is the property the decomposition exists to
        # remove: a same-lane trip around an exclusion and back, WITHIN one cell.
        # `coverage_cell_handover_count` is the separate, intended crossing from one column beside
        # an obstacle to the other. Both are measured on the generated route, not assumed.
        "coverage_cell_count": coverage_cell_count,
        "same_lane_obstacle_bridge_count": same_lane_bridges,
        "coverage_cell_handover_count": cell_handovers,
        # WHICH CELL WAS COVERED WHEN, AND WHY (see _bcd_cell_plan). Per pass: the adjacency
        # graph the ordering was constrained by, the order and orientation chosen, and every
        # inter-cell hand-over with the distance and the tier that drew it. Reported so the
        # sequence is inspectable and re-derivable rather than an unexplained property of the
        # emitted route — the whole point of ordering by measured cost is that the measurements
        # can be shown.
        "coverage_cell_plans": [
            {"pass_kind": d["pass_kind"], **{k: v for k, v in d["cell_plan"].items()}}
            for d in coverage_diags if d.get("cell_plan")],
        "inter_cell_transit_length_m": round(
            sum(d["cell_plan"]["inter_cell_transit_m"]
                for d in coverage_diags if d.get("cell_plan")), 2),
        "largest_inter_cell_transit_m": round(
            max((d["cell_plan"]["largest_inter_cell_transit_m"]
                 for d in coverage_diags if d.get("cell_plan")), default=0.0), 2),
        "non_adjacent_cell_handover_count": sum(
            d["cell_plan"]["non_adjacent_handover_count"]
            for d in coverage_diags if d.get("cell_plan")),
        "backtracking_events": full_q["backtracking_events"],
        "minimum_segment_length_m": full_q["minimum_segment_length_m"],
        "cleanup_applied": True,
        # ── Survey-frame alignment: COVERAGE FRAGMENTS and TRANSITIONS, counted SEPARATELY ──
        # A `primary`/`secondary` segment holds both the sonar passes and the transits between
        # them, and only the passes are bound by the survey-frame contract — a transit is the
        # vessel repositioning, at whatever heading is shortest and safe. Counting the two
        # together charged a legitimate transit to the coverage contract, so they are now split
        # by `_coverage_leg_split` using the generator's own recorded fragment endpoints (exact,
        # not inferred). Every number is still a plain count over emitted geometry, not a score.
        #
        # `non_survey_aligned_coverage_segment_count` is THE one to watch: arbitrary-angle SURVEY
        # geometry, which is what the rounded no-go bypasses used to produce, and which must be 0.
        "survey_aligned_coverage_segment_count": aligned_n,
        "non_survey_aligned_coverage_segment_count": unaligned_n,
        # The transit half. Informational, and EXPECTED to be non-zero: it counts the
        # direct-safe-first transitions (see _aligned_transition tier 0), each one proven with the
        # same authoritative predicate as every other leg. It is not a contract violation.
        "survey_aligned_transition_count": align["transition_aligned"],
        "non_survey_aligned_transition_count": align["transition_unaligned"],
        # COMPATIBILITY. Both keys keep their names and their meaning is now stated exactly:
        # COVERAGE FRAGMENTS ONLY. Before the split they summed the whole coverage polyline; the
        # difference is precisely the transition legs, which are reported above instead. Every
        # existing consumer asserts `non_survey_aligned_segment_count == 0`, which this preserves
        # and in fact tightens — it can no longer be satisfied or broken by transit geometry.
        "survey_aligned_segment_count": aligned_n,
        "non_survey_aligned_segment_count": unaligned_n,
        # How the transitions were built, by tier of _aligned_transition.
        "direct_transit_transition_count": transition_totals["direct_transit"],
        # Transits where the straight leg was unsafe and a bounded LOCAL detour beat the aligned
        # candidate (tier 0b). `saved` is measured against the aligned candidate it replaced, so
        # the improvement is stated rather than implied.
        "shortest_safe_transition_count": transition_totals["shortest_safe_transit"],
        "shortest_safe_transition_distance_m": round(grid.shortest_safe_len_m, 2),
        "shortest_safe_saved_distance_m": round(grid.shortest_safe_saved_m, 2),
        "aligned_direct_transition_count": transition_totals["direct"],
        "orthogonal_transition_count": transition_totals["orthogonal"] + transition_totals["bypass"],
        "fallback_connector_count": transition_totals["fallback"],
        # ── Coverage vs transit ACCOUNTING inside the coverage segments ──────────────────
        # `metrics.coverage_length_m` is the length of the whole primary/secondary polyline, so
        # it includes the inter-fragment transits. These two numbers say exactly how it divides,
        # so the accounting is visible rather than implied. (Reported, not yet acted on.)
        "coverage_fragment_length_m": align["coverage_fragment_leg_m"],
        "in_coverage_transition_length_m": align["transition_leg_m"],
        "skipped_short_fragment_count": skipped_n,
        "skipped_short_fragment_length_m": round(
            sum(d["skipped_short_fragment_length_m"] for d in coverage_diags), 2),
        "minimum_useful_fragment_m": round(_min_useful_fragment_m(spacing), 2),
        "coverage_fragments": coverage_fragments,
    }
    metrics["route_quality"] = route_quality

    # The approved Home corridor, derived from the APPROVED TRANSIT geometry THIS generation just
    # produced and validated — the executed transit legs PLUS any planning-only leg (the Home →
    # A1 connector under `first_approach`). Deriving it from the execution subset alone is what
    # used to leave a `first_approach` mission with an approach chain disconnected from the Home-
    # anchored return chain, hence no single-ring corridor at all. Emitted for the Plan/Map
    # overlay and, later, for the Scout package — from the same function, so what the operator
    # sees drawn is what Scout is sent. A refusal is normal and is reported with its reason
    # rather than as a warning the operator must clear.
    home_corridor, home_corridor_meta = home_corridor_ring(
        segments=segments, planning_only_transit_segments=planning_only_segments,
        navigable_geometry=navigable_boundary,
        no_go_zones=zones, planning_home=home, no_go_clearance_m=no_go_clearance)
    if home_corridor is None and home is not None:
        warnings.append(
            "No approved Home corridor could be derived: "
            f"{home_corridor_meta['reason']}. If the launch Home ends up outside the approved "
            "navigable area, the agent cannot prove a safe return and will hold in LOITER.")

    # ── THE GEOMETRY CONTRACT, PROVEN BEFORE ANYTHING IS RETURNED ───────────────────────────
    # Generation does not get to emit a package whose own route contradicts its own geometry.
    # A failure here is a hard error with a specific code, NOT a warning and NOT an excuse to
    # widen the safety region to the raw operator boundary — see check_mission_geometry.
    geometry_check = check_mission_geometry(
        segments=segments, planning_only_transit_segments=planning_only_segments,
        route_waypoints=route_waypoints,
        navigable_geometry=navigable_boundary, no_go_zones=zones,
        no_go_clearance_m=no_go_clearance, planning_home=home,
        home_corridor=home_corridor)
    if not geometry_check["ok"]:
        raise GeometryConsistencyError(geometry_check["failures"])

    input_revision = _input_revision(inp)
    planning_inputs = {
        "boundary": boundary,
        "shoreline_clearance_m": clearance,
        "navigable_boundary": navigable_boundary,
        # PROVENANCE: the operator's ORIGINAL no-go rings, plus the clearance parameter that
        # was routed against them. The buffered exclusion is deliberately NOT stored here —
        # a consumer that needs it derives it from these two, and the Map keeps drawing the
        # red polygon the operator actually drew.
        "no_go_zones": zones,
        "no_go_clearance_m": no_go_clearance,
        "lane_spacing_m": spacing,
        "primary_angle_deg": inp["primary_angle_deg"],
        "dual_pass": inp["dual_pass"],
        "secondary_angle_deg": inp["secondary_angle_deg"] if inp["dual_pass"] else None,
        "planning_home": home,
        "route_start_mode": start_mode,
        "approach_waypoints": approach,
        "return_waypoints": returns,
    }

    return {
        "ok": True,
        "mission_package_version": MISSION_PACKAGE_VERSION,
        "contract_version": ROUTE_CONTRACT_VERSION,
        "planning_inputs": planning_inputs,
        # THE EXECUTION ROUTE. Flattened, hashed, uploaded, flown.
        "segments": segments,
        # APPROVED TRANSIT GEOMETRY THAT IS NOT EXECUTED. Same segment shape, each entry flagged
        # `planning_only: true`. Empty for every mission whose approved transit is entirely
        # executed (which is every `planning_home` mission); one `home_transit_connector` under
        # `first_approach` with a planning Home and approach waypoints. It exists for corridor
        # derivation, geometric validation and safe-return provenance — it is deliberately NOT
        # part of `segments`, `original_execution_order`, `route_waypoints` or `route_hash`.
        "planning_only_transit_segments": planning_only_segments,
        "original_execution_order": original_execution_order,
        "route_waypoints": route_waypoints,
        "route_hash": _route_hash(route_waypoints),
        "metrics": metrics,
        "route_quality": route_quality,
        "generation_algorithm": GENERATION_ALGORITHM,
        # The proof, carried with the package: every route leg is inside approved geometry and
        # outside the effective no-go exclusion. Always `ok:true` here (generation raises
        # otherwise) — it is retained so finalize and the Plan page can show WHAT was proven.
        "geometry_check": geometry_check,
        "intersections": intersections,
        "navigable_boundary": navigable_boundary,
        # The buffered no-go exclusion this generation routed around, as drawable rings. A
        # DERIVED overlay for the Plan page (subtle dashed outline) — the authoritative,
        # operator-defined zone stays `planning_inputs.no_go_zones` and stays red. Empty when
        # there are no zones, or when the clearance is 0 (then it equals the drawn geometry).
        "no_go_exclusion_rings": exclusion_rings if no_go_clearance > TOLERANCE else [],
        # The approved Home corridor for the Plan/Map overlay: a single implicitly-closed
        # [[lng, lat], ...] ring, or None when none is proven (with the reason in
        # `home_corridor_meta`). Drawn distinctly from the survey boundary, the no-go zones and
        # the route — it is approved TRANSIT geometry, not survey area.
        "home_corridor": home_corridor,
        "home_corridor_meta": home_corridor_meta,
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_revision": input_revision,
    }


def _utm_for(boundary):
    """(to_proj, to_deg) UTM transformers for the boundary's centroid — the same zone the
    generator uses, so metres and geometry agree across every step."""
    c = Polygon([(p[0], p[1]) for p in boundary]).centroid
    zone = int((c.x + 180) / 6) + 1
    hemi = 6 if c.y >= 0 else 7
    crs = f"EPSG:32{hemi}{zone:02d}"
    return (pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True),
            pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True))


def _inset_polygon(boundary, clearance):
    """The boundary polygon inset by `clearance` metres, as a shapely (projected) polygon,
    or None. Uses the same UTM projection as the generator so metres mean metres."""
    ring = [(c[0], c[1]) for c in boundary]
    poly_deg = Polygon(ring)
    if not poly_deg.is_valid:
        poly_deg = poly_deg.buffer(0)
    to_proj, _ = _utm_for(boundary)
    poly = transform(to_proj.transform, poly_deg)
    if clearance and clearance > TOLERANCE:
        poly = poly.buffer(-abs(clearance), join_style=2)
    if poly.is_empty or not poly.is_valid:
        return None
    return poly


def _navigable_rings_deg(boundary, clearance):
    """The shoreline-inset navigable area as a list of exterior rings [[lng,lat],...], for
    the map's 'navigable area' overlay. Returns [] when the inset is empty (nothing to draw)
    — the UI then shows no navigable overlay rather than faking one."""
    inset = _inset_polygon(boundary, clearance)
    if inset is None or inset.is_empty:
        return []
    _, to_deg = _utm_for(boundary)
    polys = inset.geoms if isinstance(inset, MultiPolygon) else [inset]
    rings = []
    for poly in polys:
        deg = transform(to_deg.transform, poly)
        rings.append([[round(x, 7), round(y, 7)] for x, y in deg.exterior.coords])
    return rings


# ═══════════════════════════════════════════════════════════════════════════════════════
# APPROVED HOME CORRIDOR (replan-planning-package-v1 `home_corridor`)
# ═══════════════════════════════════════════════════════════════════════════════════════
# WHAT SCOUT NEEDS IT FOR, AND WHY THE OPERATOR IS THE ONLY PLACE IT CAN COME FROM
# --------------------------------------------------------------------------------
# When Scout must prove a safe return, it checks the return path against the operator's
# approved navigable geometry. A launch/recovery Home frequently sits OUTSIDE the survey
# polygon — at a jetty, a ramp, the shore — so the last leg of a return leaves the navigable
# area and Scout cannot prove it. Without proof it fails closed and holds in LOITER, which is
# the correct behaviour and must be preserved.
#
# `home_corridor` is the operator's ANSWER to that: a single approved polygon that covers the
# connector between the navigable survey area and the launch/Home area, so Scout has approved
# geometry for the one leg it otherwise cannot verify.
#
# THE RULE THAT MAKES IT SAFE: it is DERIVED FROM GEOMETRY THE OPERATOR ALREADY APPROVED, and
# from nothing else. Specifically, from the transit/connector segments this generator produced
# and validated against the navigable area and the no-go zones — the same legs the operator saw
# on the Plan page and uploaded. It is NEVER drawn from a runtime Home coordinate, never widened
# to reach one, and never emitted "just in case":
#
#   • no approved transit geometry            -> no corridor
#   • the corridor does not contain the planning Home        -> no corridor
#   • the corridor does not overlap the navigable area       -> no corridor
#   • the corridor touches a no-go zone                      -> no corridor
#
# and an ABSENT corridor is a real, correct answer: Scout then cannot prove a safe return to a
# Home outside the approved area, and fails closed in LOITER. That is the outcome, not a defect
# to engineer around. If the runtime Home later falls outside this corridor, the corridor is NOT
# expanded — the plan is what needs revisiting.
#
# `shoreline_clearance_m` is unaffected and remains what it always was: scalar metadata. The
# corridor is geometry; the clearance number is not, and neither is presented as the other.

# The connector legs that, together, are the approved path between the planning Home and the
# survey area. Coverage passes are deliberately excluded — they are the survey, not the
# connector, and buffering them would produce a "corridor" covering the whole site.
#
# `home_transit_connector` is the PLANNING-ONLY Home → A1 leg (`route_start_mode:
# first_approach`). It is approved transit geometry the execution route does not carry, and it
# is a corridor source for exactly the same reason every other kind here is: the operator
# approved it and this station validated it. It never appears in `segments`, so listing it here
# only ever matches geometry supplied as `planning_only_transit_segments`.
HOME_CORRIDOR_SOURCE_KINDS = (
    "start_connector", "home_transit_connector", "approach", "survey_entry_connector",
    "return_connector", "return_approach", "final_home_connector",
)

# Half-width of the corridor, in metres, measured either side of the approved transit centreline
# (so the corridor is 2x this wide). It is a fixed, stated number rather than a derived one on
# purpose: it must be legible on the Plan page and reproducible from the record alone. 6 m gives
# a small USV room to hold a line under wind and current without the corridor swallowing
# geometry the operator did not approve.
HOME_CORRIDOR_HALF_WIDTH_M = 6.0

# Ring simplification tolerance in metres. Buffering produces many near-collinear vertices; this
# keeps the wire ring small. Well below the half-width, so it cannot round the corridor out to
# somewhere unapproved.
HOME_CORRIDOR_SIMPLIFY_M = 0.25


def approved_transit_segments(segments, planning_only_transit_segments=None):
    """The COMPLETE approved Home↔survey transit network of a package or record: every
    transit/connector segment of the EXECUTION route, plus every PLANNING-ONLY transit segment
    the plan approved but does not execute.

    This is the authoritative corridor source and the authoritative answer to "which geometry did
    the operator approve between Home and the survey". It is a superset of the execution transit
    legs and is never a substitute for them: `segments` remains what is uploaded and flown.

    Planning-only legs come first because the only one that exists (the Home → A1 connector under
    `route_start_mode: first_approach`) precedes the execution route geometrically. A record
    without the field — every mission planned before it existed — yields the execution transit
    legs alone, which is exactly the previous behaviour.
    """
    planning_only = [s for s in (planning_only_transit_segments or []) if isinstance(s, dict)]
    executed = [s for s in (segments or [])
                if isinstance(s, dict) and s.get("kind") in HOME_CORRIDOR_SOURCE_KINDS]
    return planning_only + executed


def _corridor_lines(segments):
    """The approved transit centrelines from a record's `segments`, as [[lng,lat],...] lists."""
    out = []
    for seg in segments or []:
        if not isinstance(seg, dict) or seg.get("kind") not in HOME_CORRIDOR_SOURCE_KINDS:
            continue
        coords = [c for c in (seg.get("coordinates") or [])
                  if isinstance(c, (list, tuple)) and len(c) >= 2]
        if len(coords) >= 2:
            out.append([(float(c[0]), float(c[1])) for c in coords])
    return out


def home_corridor_ring(*, segments, navigable_geometry, no_go_zones, planning_home,
                       no_go_clearance_m=0.0, half_width_m=HOME_CORRIDOR_HALF_WIDTH_M,
                       planning_only_transit_segments=None):
    """Derive the approved Home corridor from already-approved planning geometry.

    Returns `(ring, meta)`. `ring` is a single implicitly-closed `[[lng, lat], ...]` polygon ring
    with at least 3 distinct vertices, or None when no corridor can be PROVEN — in which case
    `meta["reason"]` names which requirement failed. Refusing is a normal outcome (see the note
    above): Scout then fails closed rather than returning through unapproved water.

    NO-GO CLEARANCE. `no_go_clearance_m` is the operator's minimum routing distance from every
    drawn no-go polygon — the SAME scalar the route was generated against. The EFFECTIVE
    exclusion (zones buffered by it) is SUBTRACTED from the corridor before any requirement is
    checked, so the ring that goes on the wire is one the exclusion has already dented. That is
    what makes "a corridor may never open a legal tunnel through a no-go buffer" a property of
    the shipped geometry rather than a promise: if the exclusion splits the corridor, punches a
    hole in it, or cuts it away from Home or from the survey area, no corridor is emitted at all.
    A clearance of 0 (or a historical record that predates the parameter) means the drawn zone
    geometry itself, which is the pre-existing behaviour exactly.

    APPROVED GEOMETRY, NOT THE EXECUTION SUBSET. The source is `approved_transit_segments` — the
    execution route's transit legs PLUS `planning_only_transit_segments`. The two differ only
    under `route_start_mode: first_approach`, where the Home → A1 leg is approved but not
    executed; deriving from the execution subset alone would leave the approach chain
    disconnected from the Home-anchored return chain and refuse a corridor that the operator's
    own approved geometry does prove. Nothing here fabricates that leg: it is supplied by the
    caller only when the generator actually routed and validated it.

    Pure and deterministic: the same record always yields the same ring.
    """
    meta = {"available": False, "reason": None, "half_width_m": half_width_m,
            "no_go_clearance_m": float(abs(no_go_clearance_m or 0.0)),
            "source_segment_kinds": [], "vertex_count": 0, "planning_only_source_count": 0,
            "contains_planning_home": None, "overlaps_navigable": None,
            "covers_transit_path": None, "clears_no_go_zones": None}
    if not PLANNING_AVAILABLE:
        meta["reason"] = ("the geometry stack (shapely/pyproj) is not installed, so no corridor "
                          "can be derived or checked")
        return None, meta

    home = _point_of(planning_home)
    if home is None:
        meta["reason"] = "the mission has no planning home to build a corridor around"
        return None, meta

    approved = approved_transit_segments(segments, planning_only_transit_segments)
    lines = _corridor_lines(approved)
    if not lines:
        meta["reason"] = ("the mission has no approved transit/connector segments — there is no "
                          "operator-approved path between Home and the survey area to buffer")
        return None, meta
    meta["source_segment_kinds"] = sorted({s.get("kind") for s in approved})
    meta["planning_only_source_count"] = sum(
        1 for s in (planning_only_transit_segments or [])
        if isinstance(s, dict) and s.get("kind") in HOME_CORRIDOR_SOURCE_KINDS)

    nav_rings = [r for r in (navigable_geometry or [])
                 if isinstance(r, list) and len(r) >= 3]
    if not nav_rings:
        meta["reason"] = "the mission carries no navigable geometry to anchor a corridor to"
        return None, meta

    # ONE projection for everything, taken from the navigable area, so metres mean metres and
    # every check below happens in the same frame the generator validated the connectors in.
    to_proj, to_deg = _utm_for(nav_rings[0])

    def proj_line(coords):
        return LineString([to_proj.transform(x, y) for x, y in coords])

    def proj_poly(ring):
        return Polygon([to_proj.transform(c[0], c[1]) for c in ring]).buffer(0)

    try:
        # ROUND caps, deliberately. A flat cap ends the corridor exactly ON the transit
        # endpoint, which puts the planning Home precisely on the boundary — geometrically
        # "contained" by a hair, and pushed outside by any later simplification or by a runtime
        # Home a metre away. A round cap extends the approved half-width around the endpoint,
        # which is also the honest shape: the Home AREA is what needs covering, not a point.
        corridor = unary_union([proj_line(c) for c in lines]).buffer(
            abs(half_width_m), join_style=2, cap_style=1)
    except Exception as exc:                                  # pragma: no cover - defensive
        meta["reason"] = f"the approved transit geometry could not be buffered ({exc})"
        return None, meta
    if corridor.is_empty:
        meta["reason"] = "buffering the approved transit geometry produced an empty corridor"
        return None, meta
    if isinstance(corridor, MultiPolygon):
        # Disconnected transit legs would need MORE than one corridor, and the wire contract is
        # ONE ring. Emitting only the largest piece would ship a corridor that silently omits an
        # approved leg, so this refuses instead.
        meta["reason"] = ("the approved transit geometry is not contiguous — it would need more "
                          "than one corridor, and the contract carries a single ring")
        return None, meta

    # SIMPLIFY FIRST, THEN CHECK. The ring that goes on the wire must be the exact geometry every
    # requirement below was proven against — validating the un-simplified buffer and shipping the
    # simplified one would ship a corridor nothing ever checked. (That is not hypothetical: the
    # simplification pulls the boundary in by up to the tolerance, which is enough to move a Home
    # sitting near a cap from inside to outside.)
    simplified = corridor.simplify(HOME_CORRIDOR_SIMPLIFY_M, preserve_topology=True)
    if simplified.is_empty or isinstance(simplified, MultiPolygon) or not simplified.is_valid:
        simplified = corridor
    # The wire ring is the EXTERIOR ring only, so the exterior is what must be checked: a Home
    # inside an interior hole is not in the corridor the ring describes.
    corridor = Polygon(simplified.exterior)
    if not corridor.is_valid:
        corridor = corridor.buffer(0)
    if corridor.is_empty or isinstance(corridor, MultiPolygon):
        meta["reason"] = "the simplified corridor is not a single valid polygon"
        return None, meta

    # ── THE EXCLUSION IS SUBTRACTED, NOT MERELY TESTED AGAINST ──────────────────────────────
    # The drawn zones expanded by the operator's no-go clearance — the same effective exclusion
    # every connector in this mission was routed around (_NavGrid.nogo). Shrunk by COVER_TOL_M
    # for the identical reason `_seg_clears_nogo` shrinks it: a leg routed exactly ALONG the
    # exclusion edge is legitimate output of the avoidance generator, not a violation.
    #
    # Subtracting rather than rejecting on contact is what keeps this honest AND usable: a
    # corridor that merely grazes the exclusion is dented and stays valid, while one that only
    # "works" by passing THROUGH the exclusion is split (MultiPolygon) or holed and is refused
    # below. Taking the exterior ring of a HOLED polygon would fill the hole straight back in —
    # which is exactly the legal tunnel this must never ship — so a hole is a refusal too.
    zones = [z for z in (no_go_zones or []) if isinstance(z, list) and len(z) >= 3]
    exclusion = None
    exclusion_touched = False
    if zones:
        exclusion = _buffer_exclusion(unary_union([proj_poly(z) for z in zones]),
                                      float(abs(no_go_clearance_m or 0.0)))
    if exclusion is not None and not exclusion.is_empty:
        try:
            probe = exclusion.buffer(-COVER_TOL_M)
        except Exception:                                     # pragma: no cover - defensive
            probe = exclusion
        if not probe.is_empty:
            exclusion_touched = bool(corridor.intersects(probe))
            clipped = corridor.difference(probe)
            if clipped.is_empty:
                meta["clears_no_go_zones"] = False
                meta["reason"] = ("the no-go exclusion (the drawn zones plus the "
                                  f"{meta['no_go_clearance_m']} m no-go clearance) covers the "
                                  "whole corridor")
                return None, meta
            if isinstance(clipped, MultiPolygon):
                meta["clears_no_go_zones"] = False
                meta["reason"] = ("the no-go exclusion splits the corridor in two — the approved "
                                  "transit path only connects by passing through the no-go "
                                  "clearance, which is not a corridor that may be approved")
                return None, meta
            if list(clipped.interiors):
                meta["clears_no_go_zones"] = False
                meta["reason"] = ("the no-go exclusion lies wholly inside the corridor — the "
                                  "single-ring contract cannot express the hole, and shipping "
                                  "the outer ring would approve routing straight through it")
                return None, meta
            corridor = clipped
        meta["clears_no_go_zones"] = True
    else:
        meta["clears_no_go_zones"] = True

    home_pt = Point(*to_proj.transform(home[0], home[1]))
    meta["contains_planning_home"] = bool(corridor.buffer(COVER_TOL_M).contains(home_pt))
    if not meta["contains_planning_home"]:
        meta["reason"] = ("the corridor derived from the approved transit path does not contain "
                          "the planning Home")
        return None, meta

    navigable = unary_union([proj_poly(r) for r in nav_rings])
    meta["overlaps_navigable"] = bool(corridor.intersection(navigable).area > TOLERANCE)
    if not meta["overlaps_navigable"]:
        meta["reason"] = ("the corridor does not overlap the approved navigable area, so it "
                          "proves no connection to the survey geometry")
        return None, meta

    # The corridor must still COVER the transit path it was derived from. Without this the
    # dented corridor could ship while the very legs it exists to approve fall outside it.
    # Navigable area counts as coverage: the approach/return connectors run through the survey
    # region, which is approved geometry in its own right.
    approved = unary_union([corridor, navigable]).buffer(COVER_TOL_M)
    uncovered = []
    for line in lines:
        rest = proj_line(line).difference(approved)
        if not rest.is_empty and rest.length >= CONNECTOR_EPS_M:
            uncovered.append(round(rest.length, 2))
    meta["covers_transit_path"] = not uncovered
    if uncovered and exclusion_touched:
        # The corridor lost that coverage to the SUBTRACTION above: the transit path runs through
        # (or within the clearance of) a no-go zone, so no corridor can approve it without
        # approving the crossing. Named as the no-go refusal it actually is.
        meta["clears_no_go_zones"] = False
        meta["reason"] = ("the corridor would cross a no-go zone — the no-go exclusion (the "
                          f"drawn zones plus the {meta['no_go_clearance_m']} m no-go clearance) "
                          f"cuts {max(uncovered)} m out of the approved transit path, which no "
                          f"corridor may bridge")
        return None, meta
    if uncovered:
        meta["reason"] = ("the corridor does not cover the approved transit path it was derived "
                          f"from ({max(uncovered)} m of it falls outside)")
        return None, meta

    deg = transform(to_deg.transform, corridor)
    coords = [[round(x, 7), round(y, 7)] for x, y in deg.exterior.coords]
    # IMPLICITLY closed on the wire: shapely repeats the first vertex to close the ring, and the
    # contract does not want that repeat.
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    distinct = {tuple(c) for c in coords}
    if len(distinct) < 3:
        meta["reason"] = "the derived corridor has fewer than 3 distinct vertices"
        return None, meta

    meta.update(available=True, reason=None, vertex_count=len(coords))
    return coords, meta


# ═══════════════════════════════════════════════════════════════════════════════════════
# MISSION GEOMETRY CONSISTENCY — the single authoritative pre-finalization proof
# ═══════════════════════════════════════════════════════════════════════════════════════
# WHY THIS EXISTS. Two live E2 replanning failures came from the SAME root cause: the mission
# package was internally inconsistent. Route waypoints the operator had approved sat OUTSIDE
# the `navigable_geometry` shipped in the very same package, because generation only ever
# required the COVERAGE passes to stay inside the shoreline inset — every transit leg (start
# connector, approach, return, home leg) was checked for no-go clearance alone and was allowed
# to run anywhere. Scout then tried to reuse those approved waypoints for RETRACE_APPROVED, its
# safe-return validation correctly refused them, retries exhausted, and native RTL took over.
#
# THE INVARIANT PROVEN HERE, once, for the whole mission:
#
#     every finalized route segment is contained in APPROVED geometry
#         approved = navigable_geometry ∪ home_corridor
#     and lies outside the EFFECTIVE no-go exclusion
#         exclusion = drawn no-go zones ⊕ no_go_clearance_m
#     and the planning Home is itself inside approved geometry.
#
# THE RAW OPERATOR BOUNDARY IS NOT PART OF THIS AND NEVER BECOMES PART OF IT. When the route
# does not fit the computed navigable geometry the answer is a hard, coded failure — never a
# widened safety region. Broadening to the raw boundary would silently discard the shoreline
# clearance the operator set, which is the one thing keeping the hull off the shore.
#
# SEGMENTS, NOT JUST WAYPOINTS. Two individually-approved endpoints can be joined by a leg that
# cuts outside the inset or clips a no-go buffer corner, so every LEG is swept, not only its
# ends. Coverage kinds are held to the navigable geometry alone; transit kinds may additionally
# use the corridor, which is precisely what the corridor is for.

# Stable, matchable failure identifiers. These are the operator's own codes (the wire contract
# is unchanged); they appear in the API error body and in the raised exception so a log line
# names the actual geometric fault rather than a prose paraphrase of it.
GEOMETRY_ERROR_CODES = (
    "INVALID_NAVIGABLE_GEOMETRY",     # no usable navigable geometry to validate against
    "ROUTE_EMPTY",                    # no route waypoints at all
    "ROUTE_WAYPOINT_INVALID",         # a waypoint is not a finite in-range coordinate
    "ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY",   # a survey point/leg leaves the navigable geometry
    "TRANSIT_OUTSIDE_APPROVED_GEOMETRY",  # a transit leg is in neither navigable nor corridor
    "ROUTE_NO_GO_VIOLATION",          # a point/leg enters the effective no-go exclusion
    "HOME_OUTSIDE_APPROVED_GEOMETRY", # planning Home is in neither navigable nor corridor
    "HOME_CORRIDOR_DISCONNECTED",     # corridor is not one polygon, or misses the survey area
    "HOME_CORRIDOR_INCOMPLETE",       # corridor misses its own Home or its own transit path
    "HOME_CORRIDOR_NO_GO_VIOLATION",  # corridor overlaps the effective no-go exclusion
    # ── THE APPROVED HOME↔SURVEY CHAIN. Raised by generate_survey when a required leg of the
    # approved transit network cannot be routed at all, so the operator reads WHICH chain is
    # broken instead of a generic "no safe connector". These are refusals, never repairs: no
    # corridor is invented, no region widened, no waypoint moved, no return substituted.
    "HOME_TO_APPROACH_DISCONNECTED",     # planning Home ↛ first approach waypoint
    "APPROACH_TO_SURVEY_DISCONNECTED",   # last approach waypoint ↛ survey entry
    "SURVEY_TO_RETURN_DISCONNECTED",     # survey end ↛ first return waypoint
    "RETURN_TO_HOME_DISCONNECTED",       # last return waypoint ↛ planning Home
)

# Segment kinds whose ENTIRE geometry must lie inside the navigable survey region. Everything
# else is transit and may additionally rely on the approved Home corridor.
GEOMETRY_SURVEY_KINDS = ("primary", "secondary", "pass_transition")

# How many offending indices a single failure names before eliding. The operator needs enough to
# locate the fault, not a wall of numbers.
_MAX_OFFENDERS = 5

# Area tolerance (m²) for the corridor-vs-exclusion overlap test. A corridor derived by CLIPPING
# the exclusion out shares its boundary with the exclusion exactly, and that shared boundary
# survives a round trip through 7-decimal-place wire coordinates as a ~1 cm sliver. Testing bare
# `.area > 0` would read that rounding noise as a no-go violation. The corridor is additionally
# shrunk by COVER_TOL_M before the test — the same half-metre tolerance every other containment
# check here uses — so what is measured is real overlap, not a shared edge.
_AREA_EPS_M2 = 1.0


class GeometryConsistencyError(ValueError):
    """A mission's own geometry contradicts itself — raised INSTEAD of returning a package.

    Carries `failures` ([{code, message, ...}]) and `codes`, so the API layer can report the
    specific geometric fault. Never raised for a route that merely looks awkward: every failure
    is a containment or clearance property that was actually measured."""

    def __init__(self, failures):
        self.failures = [dict(f) for f in (failures or [])]
        self.codes = [f.get("code") for f in self.failures]
        super().__init__("; ".join(
            f"{f.get('code')}: {f.get('message')}" for f in self.failures) or
            "the mission geometry is inconsistent")


def _elide(items):
    shown = list(items)[:_MAX_OFFENDERS]
    return f"{shown}{' …' if len(items) > len(shown) else ''}"


def check_mission_geometry(*, segments, route_waypoints, navigable_geometry, no_go_zones,
                           no_go_clearance_m, planning_home, home_corridor=None,
                           planning_only_transit_segments=None):
    """Prove (or disprove) that one mission's geometry is self-consistent.

    Returns `{ok, failures, checks}` — it never repairs, never widens and never falls back to
    the raw operator boundary. `failures` entries carry a stable `code` from
    GEOMETRY_ERROR_CODES plus an actionable message. Pure, deterministic and network-free: the
    same package always yields the same verdict.

    EXECUTION ROUTE vs APPROVED GEOMETRY. `segments` + `route_waypoints` are the EXECUTED
    mission and are held to the full contract. `planning_only_transit_segments` is approved
    transit geometry that is not executed (the Home → A1 leg under `route_start_mode:
    first_approach`); it is held to the SAME containment and no-go rules, because it is what the
    Home corridor is derived from and an unchecked leg must never become approved geometry. It
    is deliberately not required to join the route: not executing a leg is the point of the mode.

    Callers: `generate_survey` (raises before returning a package), `validate_plan` (reports as
    errors), POST /api/missions/finalize (refuses to store the immutable record) and
    `fleet_planning` (per child mission). One implementation, so those four can never disagree.
    """
    _require_available()
    failures = []
    planning_only = [s for s in (planning_only_transit_segments or []) if isinstance(s, dict)]
    checks = {"navigable_ring_count": 0, "home_corridor_supplied": home_corridor is not None,
              "planning_only_transit_segment_count": len(planning_only),
              "no_go_clearance_m": float(abs(no_go_clearance_m or 0.0))}

    def fail(code, message, **extra):
        failures.append({"code": code, "message": message, **extra})

    nav_rings = [r for r in (navigable_geometry or [])
                 if isinstance(r, (list, tuple)) and len(r) >= 3]
    checks["navigable_ring_count"] = len(nav_rings)
    if not nav_rings:
        fail("INVALID_NAVIGABLE_GEOMETRY",
             "the mission carries no navigable geometry — there is nothing to validate the "
             "route against, and the raw survey boundary is not a substitute for it")
        return {"ok": False, "failures": failures, "checks": checks}

    route = route_waypoints if isinstance(route_waypoints, list) else []
    checks["waypoint_count"] = len(route)
    if not route:
        fail("ROUTE_EMPTY", "the mission has no route waypoints")
        return {"ok": False, "failures": failures, "checks": checks}

    coords = []
    bad = []
    for i, wp in enumerate(route):
        lat = _num(wp.get("latitude")) if isinstance(wp, dict) else None
        lng = _num(wp.get("longitude")) if isinstance(wp, dict) else None
        if lat is None or abs(lat) > 90 or lng is None or abs(lng) > 180:
            bad.append(i + 1)
        else:
            coords.append((lng, lat))
    checks["waypoints_finite"] = not bad
    if bad:
        fail("ROUTE_WAYPOINT_INVALID",
             f"route waypoint(s) {_elide(bad)} are not valid latitude/longitude coordinates",
             waypoints=bad[:_MAX_OFFENDERS])
        return {"ok": False, "failures": failures, "checks": checks}

    # ONE projection for the whole proof, taken from the navigable geometry — the same frame
    # `home_corridor_ring` uses, so metres mean metres and the two can never disagree.
    to_proj, _ = _utm_for(nav_rings[0])

    def proj_poly(ring):
        return Polygon([to_proj.transform(c[0], c[1]) for c in ring]).buffer(0)

    def proj_line(pts):
        return LineString([to_proj.transform(p[0], p[1]) for p in pts])

    try:
        navigable = unary_union([proj_poly(r) for r in nav_rings])
    except Exception as exc:                                  # pragma: no cover - defensive
        fail("INVALID_NAVIGABLE_GEOMETRY",
             f"the navigable geometry could not be interpreted as a polygon ({exc})")
        return {"ok": False, "failures": failures, "checks": checks}
    checks["navigable_valid"] = bool(navigable and not navigable.is_empty and navigable.is_valid)
    if not checks["navigable_valid"]:
        fail("INVALID_NAVIGABLE_GEOMETRY",
             "the navigable geometry is empty or invalid — reduce the shoreline clearance or "
             "enlarge the survey boundary")
        return {"ok": False, "failures": failures, "checks": checks}

    # The EFFECTIVE exclusion: the operator's drawn zones expanded by the no-go clearance. The
    # drawn rings stay untouched (provenance); this derived geometry is what the route is held
    # to, exactly as it is during generation.
    zones = [z for z in (no_go_zones or []) if isinstance(z, (list, tuple)) and len(z) >= 3]
    exclusion = None
    if zones:
        try:
            exclusion = _buffer_exclusion(unary_union([proj_poly(z) for z in zones]),
                                          float(abs(no_go_clearance_m or 0.0)))
        except Exception:                                     # pragma: no cover - defensive
            exclusion = None
    probe = None
    if exclusion is not None and not exclusion.is_empty:
        try:
            probe = exclusion.buffer(-COVER_TOL_M)
        except Exception:                                     # pragma: no cover - defensive
            probe = exclusion
        if probe.is_empty:
            probe = None
    checks["no_go_zone_count"] = len(zones)

    corridor = None
    if home_corridor:
        try:
            corridor = proj_poly(home_corridor)
        except Exception:
            corridor = None
        if corridor is None or corridor.is_empty or not corridor.is_valid:
            fail("HOME_CORRIDOR_DISCONNECTED",
                 "the supplied home_corridor is not a valid polygon")
            corridor = None
        elif isinstance(corridor, MultiPolygon):
            fail("HOME_CORRIDOR_DISCONNECTED",
                 "the supplied home_corridor is more than one polygon — the contract carries a "
                 "single approved ring")
            corridor = None

    approved = unary_union([navigable, corridor]) if corridor is not None else navigable
    approved_tol = approved.buffer(COVER_TOL_M)
    navigable_tol = navigable.buffer(COVER_TOL_M)

    def covered(line, region_tol):
        rest = line.difference(region_tol)
        return rest.is_empty or rest.length < CONNECTOR_EPS_M

    def clears(line):
        if probe is None:
            return True
        try:
            return line.intersection(probe).length < CONNECTOR_EPS_M
        except Exception:                                     # pragma: no cover - defensive
            return True

    # ── every route WAYPOINT is covered, and clears the exclusion ────────────────────────────
    outside = [i + 1 for i, c in enumerate(coords)
               if not approved_tol.contains(Point(*to_proj.transform(c[0], c[1])))]
    checks["waypoints_within_approved"] = not outside
    if outside:
        fail("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY",
             f"route waypoint(s) {_elide(outside)} lie outside the approved geometry "
             f"(navigable geometry"
             + (" plus the approved Home corridor)" if corridor is not None else ")")
             + " — the route and the geometry shipped with it do not agree",
             waypoints=outside[:_MAX_OFFENDERS])
    if probe is not None:
        inzone = [i + 1 for i, c in enumerate(coords)
                  if probe.contains(Point(*to_proj.transform(c[0], c[1])))]
        checks["waypoints_clear_no_go"] = not inzone
        if inzone:
            fail("ROUTE_NO_GO_VIOLATION",
                 f"route waypoint(s) {_elide(inzone)} lie inside the no-go exclusion "
                 f"(the drawn zones plus a {checks['no_go_clearance_m']} m no-go clearance)",
                 waypoints=inzone[:_MAX_OFFENDERS])
    else:
        checks["waypoints_clear_no_go"] = True

    # ── every route LEG is covered, and clears the exclusion ─────────────────────────────────
    # Independent of the segmentation on purpose: a route submitted with no segments, or with
    # segments that do not flatten to it, is still held to the full leg guarantee.
    leg_outside, leg_nogo = [], []
    for i, (a, b) in enumerate(zip(coords, coords[1:])):
        line = proj_line([a, b])
        if not covered(line, approved_tol):
            leg_outside.append(i + 1)
        if not clears(line):
            leg_nogo.append(i + 1)
    checks["route_legs_within_approved"] = not leg_outside
    checks["route_legs_clear_no_go"] = not leg_nogo
    if leg_outside:
        fail("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY",
             f"route leg(s) {_elide(leg_outside)} (waypoint N → N+1) leave the approved "
             f"geometry even though their endpoints may not — a straight leg between two "
             f"approved points is not itself approved",
             legs=leg_outside[:_MAX_OFFENDERS])
    if leg_nogo:
        fail("ROUTE_NO_GO_VIOLATION",
             f"route leg(s) {_elide(leg_nogo)} (waypoint N → N+1) cross the no-go exclusion "
             f"(the drawn zones plus a {checks['no_go_clearance_m']} m no-go clearance)",
             legs=leg_nogo[:_MAX_OFFENDERS])

    # ── SCOUT PARITY: every leg EXACTLY covered, no tolerance at all ──────────────────────────
    # The checks above are the operator-facing ones and carry COVER_TOL_M so projection noise at
    # the inset edge does not read as a fault. Scout does not have that tolerance: it must prove
    # every approved waypoint and segment is retraceable inside `navigable_geometry ∪
    # home_corridor` for a safe return to be provable, and it proves it with exact containment.
    #
    # A route that passes the tolerant checks but fails this one is EXACTLY the package Scout
    # answers with HTTP 400 ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY — after the Pixhawk upload has
    # already been verified, leaving the vehicle holding a mission Scout will not plan a return
    # for. So it is a failure HERE, before Finish & Upload, and not a warning.
    #
    # `covers` is the exact predicate, not a buffered approximation: a leg lying ON the boundary
    # IS covered (touching is inside, consistently, on both sides), a leg a millimetre outside is
    # not. There is nothing to tune — the generator's job is to produce geometry that survives
    # the 7-decimal wire round trip (see WIRE_MARGIN_M), not this check's job to absorb it.
    exact_outside = []
    for i, (a, b) in enumerate(zip(coords, coords[1:])):
        try:
            if not approved.covers(proj_line([a, b])):
                exact_outside.append(i + 1)
        except Exception:                                     # pragma: no cover - defensive
            exact_outside.append(i + 1)
    checks["route_legs_exactly_within_approved"] = not exact_outside
    if exact_outside:
        fail("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY",
             f"route segment(s) {_elide(exact_outside)} (waypoint N → N+1) are not fully "
             f"contained by the approved geometry that ships with them (navigable geometry"
             + (" plus the approved Home corridor)" if corridor is not None else ")")
             + " — every approved waypoint and segment must be retraceable inside it for a safe "
               "return to be provable, so this route cannot be uploaded",
             legs=exact_outside[:_MAX_OFFENDERS])

    # ── per-SEGMENT coverage, with the kind-specific rule ────────────────────────────────────
    # The EXECUTION segments and the PLANNING-ONLY approved transit legs are swept together and
    # to the same standard — a planning-only leg is approved geometry, and approving an unchecked
    # leg is exactly how a corridor would come to cover water nothing ever validated. They are
    # labelled distinctly so the operator can tell which of the two a fault is in.
    segs = [s for s in (segments or []) if isinstance(s, dict)]
    survey_bad, transit_bad, seg_nogo = [], [], []
    swept = ([(f"{i + 1}", s) for i, s in enumerate(segs)]
             + [(f"P{i + 1}", s) for i, s in enumerate(planning_only)])
    for tag, s in swept:
        sc = [(p[0], p[1]) for p in (s.get("coordinates") or [])
              if isinstance(p, (list, tuple)) and len(p) >= 2]
        if len(sc) < 2:
            continue
        try:
            line = proj_line(sc)
        except Exception:                                     # pragma: no cover - defensive
            continue
        label = (f"{tag} ({s.get('kind')}, planning-only)" if tag.startswith("P")
                 else f"{tag} ({s.get('kind')})")
        if s.get("kind") in GEOMETRY_SURVEY_KINDS:
            if not covered(line, navigable_tol):
                survey_bad.append(label)
        elif not covered(line, approved_tol):
            transit_bad.append(label)
        if not clears(line):
            seg_nogo.append(label)
    checks["survey_segments_within_navigable"] = not survey_bad
    checks["transit_segments_within_approved"] = not transit_bad
    checks["segments_clear_no_go"] = not seg_nogo
    if survey_bad:
        fail("ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY",
             f"survey segment(s) {_elide(survey_bad)} leave the navigable geometry — coverage "
             f"must stay inside the shoreline-offset region, and the raw survey boundary is not "
             f"a substitute for it",
             segments=survey_bad[:_MAX_OFFENDERS])
    if transit_bad:
        fail("TRANSIT_OUTSIDE_APPROVED_GEOMETRY",
             f"transit segment(s) {_elide(transit_bad)} lie outside both the navigable geometry "
             f"and the approved Home corridor"
             + ("" if corridor is not None else
                " (no Home corridor is available to approve them)"),
             segments=transit_bad[:_MAX_OFFENDERS])
    if seg_nogo:
        fail("ROUTE_NO_GO_VIOLATION",
             f"segment(s) {_elide(seg_nogo)} cross the no-go exclusion (the drawn zones plus a "
             f"{checks['no_go_clearance_m']} m no-go clearance)",
             segments=seg_nogo[:_MAX_OFFENDERS])

    # ── the planning Home is itself covered ──────────────────────────────────────────────────
    home = _point_of(planning_home) if planning_home is not None else None
    checks["home_set"] = home is not None
    if home is not None:
        home_pt = Point(*to_proj.transform(home[0], home[1]))
        checks["home_within_approved"] = bool(approved_tol.contains(home_pt))
        if not checks["home_within_approved"]:
            fail("HOME_OUTSIDE_APPROVED_GEOMETRY",
                 "the planning Home lies outside the navigable geometry and outside any approved "
                 "Home corridor — derive an approach/return transit the corridor can be built "
                 "from, or move the Home into the navigable area")
        if probe is not None and probe.contains(home_pt):
            checks["home_clears_no_go"] = False
            fail("ROUTE_NO_GO_VIOLATION",
                 "the planning Home lies inside the no-go exclusion")
        else:
            checks["home_clears_no_go"] = True
    else:
        checks["home_within_approved"] = None

    # ── the corridor itself ──────────────────────────────────────────────────────────────────
    if corridor is not None:
        checks["corridor_overlaps_navigable"] = bool(
            corridor.intersection(navigable).area > TOLERANCE)
        if not checks["corridor_overlaps_navigable"]:
            fail("HOME_CORRIDOR_DISCONNECTED",
                 "the approved Home corridor does not overlap the navigable geometry, so it "
                 "proves no connection between Home and the survey area")
        if home is not None:
            contains_home = corridor.buffer(COVER_TOL_M).contains(
                Point(*to_proj.transform(home[0], home[1])))
            checks["corridor_contains_home"] = bool(contains_home)
            if not contains_home and not checks.get("home_within_approved"):
                fail("HOME_CORRIDOR_INCOMPLETE",
                     "the approved Home corridor does not contain the planning Home it exists "
                     "to approve")
        # The corridor must cover the APPROVED transit path it claims — the legs that are not
        # already inside the navigable geometry are exactly the ones it is the only approval for.
        # Planning-only legs are included: they are corridor sources, so a corridor that does not
        # cover one was not derived from the geometry it is shipped with.
        corridor_tol = unary_union([corridor, navigable]).buffer(COVER_TOL_M)
        missed = []
        for tag, s in swept:
            if s.get("kind") not in HOME_CORRIDOR_SOURCE_KINDS:
                continue
            sc = [(p[0], p[1]) for p in (s.get("coordinates") or [])
                  if isinstance(p, (list, tuple)) and len(p) >= 2]
            if len(sc) >= 2 and not covered(proj_line(sc), corridor_tol):
                missed.append(f"{tag} ({s.get('kind')}"
                              + (", planning-only)" if tag.startswith("P") else ")"))
        checks["corridor_covers_transit"] = not missed
        if missed:
            fail("HOME_CORRIDOR_INCOMPLETE",
                 f"the approved Home corridor does not cover transit segment(s) {_elide(missed)} "
                 f"— it was not derived from the transit path it is shipped with",
                 segments=missed[:_MAX_OFFENDERS])
        if probe is not None:
            try:
                inner = corridor.buffer(-COVER_TOL_M)
            except Exception:                                 # pragma: no cover - defensive
                inner = corridor
            crosses = (not inner.is_empty) and inner.intersection(probe).area > _AREA_EPS_M2
            checks["corridor_clears_no_go"] = not crosses
            if crosses:
                fail("HOME_CORRIDOR_NO_GO_VIOLATION",
                     "the approved Home corridor overlaps the no-go exclusion (the drawn zones "
                     "plus a "
                     f"{checks['no_go_clearance_m']} m no-go clearance) — approving it would "
                     "open a legal route through geometry the operator excluded")
        else:
            checks["corridor_clears_no_go"] = True

    return {"ok": not failures, "failures": failures, "checks": checks}


def mission_geometry_arguments(package):
    """Pull `check_mission_geometry`'s inputs out of an operator-survey-plan-v1 package OR an
    immutable mission record — the two carry the same geometry under the same names, in slightly
    different places. One extractor so the generate, finalize and package paths validate exactly
    the same fields.

    Backward compatible by construction: a historical record without `no_go_clearance_m` reads
    as 0.0 (the drawn zone geometry itself, which is what it was planned against), one without
    `home_corridor` reads as None, and one without `planning_only_transit_segments` reads as []
    — its approved transit geometry simply is its execution transit geometry. Nothing is
    invented, and nothing is broadened."""
    pkg = package if isinstance(package, dict) else {}
    inputs = pkg.get("planning_inputs") if isinstance(pkg.get("planning_inputs"), dict) else {}
    metrics = pkg.get("metrics") if isinstance(pkg.get("metrics"), dict) else {}

    navigable = _first_present(pkg.get("navigable_geometry"), pkg.get("navigable_boundary"),
                               inputs.get("navigable_boundary"))
    zones = pkg.get("no_go_zones")
    if zones is None:
        zones = inputs.get("no_go_zones")
    clearance = _first_present(inputs.get("no_go_clearance_m"), metrics.get("no_go_clearance_m"))
    return {
        "segments": pkg.get("segments") or [],
        "planning_only_transit_segments": pkg.get("planning_only_transit_segments") or [],
        "route_waypoints": pkg.get("route_waypoints") or [],
        "navigable_geometry": navigable or [],
        "no_go_zones": zones or [],
        "no_go_clearance_m": float(clearance) if clearance is not None else 0.0,
        "planning_home": inputs.get("planning_home"),
        "home_corridor": pkg.get("home_corridor"),
    }


def check_package_geometry(package):
    """`check_mission_geometry` applied to a whole package/record. Returns the same report."""
    return check_mission_geometry(**mission_geometry_arguments(package))


def _point_in_any_zone(pt, zones):
    if not pt or not zones:
        return False
    p = Point(pt[0], pt[1])
    for z in zones:
        try:
            if Polygon([(c[0], c[1]) for c in z]).buffer(0).contains(p):
                return True
        except Exception:
            continue
    return False


def _pass_intersections(primary_coords, secondary_coords):
    """primary∩secondary as [[lng,lat],...] planning metadata (best-effort, deg-space)."""
    try:
        inter = LineString([(c[0], c[1]) for c in primary_coords]).intersection(
            LineString([(c[0], c[1]) for c in secondary_coords]))
    except Exception:
        return []
    pts = []
    if inter.is_empty:
        return []
    if isinstance(inter, Point):
        pts = [(inter.x, inter.y)]
    elif isinstance(inter, MultiPoint):
        pts = [(p.x, p.y) for p in inter.geoms]
    elif isinstance(inter, GeometryCollection):
        pts = [(g.x, g.y) for g in inter.geoms if isinstance(g, Point)]
    return [[round(x, 7), round(y, 7)] for x, y in pts]


# Segment kinds whose ENTIRE geometry must stay inside the navigable region: the coverage
# passes and the transition between them (both endpoints are coverage points inside the
# inset). The survey-entry and return CONNECTORS deliberately bridge operator transit points
# that sit near-shore OUTSIDE the inset, so — like the approach/return legs and home
# connectors — they are validated for no-go clearance only, never full containment.
_INSIDE_KINDS = ("primary", "secondary", "pass_transition")
_COVERAGE_KINDS = ("primary", "secondary")


def validate_plan(raw, max_route_waypoints=None, min_waypoints=2):
    """Deterministic, independent pre-upload validation — the final defence even though
    generate_survey already returns a structurally valid route. `raw` carries the planning
    inputs plus the generated `segments`, `route_waypoints` and (optionally) `route_hash` /
    `input_revision`. Returns {ok, errors, warnings, checks}; errors block upload, warnings do
    not. Never repairs geometry — it only reports, and it names the offending segment where
    it can."""
    _require_available()
    errors = []
    warnings = []
    checks = {}

    try:
        inp = normalize_generate_inputs(raw)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": [], "checks": {}}

    boundary = inp["boundary"]
    clearance = inp["shoreline_clearance_m"]
    no_go_clearance = inp["no_go_clearance_m"]
    zones = inp["no_go_zones"]

    inset = _inset_polygon(boundary, clearance)
    checks["boundary_valid"] = True
    checks["inset_nonempty"] = bool(inset and not inset.is_empty)
    if not checks["inset_nonempty"]:
        errors.append("The survey boundary is empty after applying the shoreline clearance.")

    grid = None
    checks["no_go_clearance_m"] = no_go_clearance
    try:
        grid = _NavGrid(boundary, clearance, zones, step_m=inp["lane_spacing_m"],
                        no_go_clearance=no_go_clearance)
        checks["no_go_buffer_valid"] = bool(grid.buffer_valid)
        if not grid.buffer_valid:
            errors.append(f"The no-go clearance of {no_go_clearance} m could not be applied — "
                          f"buffering the no-go zones produced invalid geometry.")
        checks["navigable_connected"] = not grid.disconnected and not grid.empty
        if grid.empty:
            errors.append("No navigable space remains after applying the shoreline clearance "
                          "and the no-go exclusion (zones + no-go clearance).")
        if grid.disconnected:
            errors.append("The navigable region is not connected — survey generation "
                          "requires one connected navigable region.")
    except Exception:
        checks["navigable_connected"] = None
        checks["no_go_buffer_valid"] = None

    for i, z in enumerate(zones):
        try:
            zp = Polygon([(c[0], c[1]) for c in z])
            if not zp.buffer(0).is_valid:
                errors.append(f"No-go zone {i + 1} is not a valid polygon.")
        except Exception:
            errors.append(f"No-go zone {i + 1} could not be interpreted as a polygon.")

    route = raw.get("route_waypoints") if isinstance(raw, dict) else None
    if not isinstance(route, list) or not route:
        errors.append("The plan has no generated route — generate a route before validating.")
        return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}

    # Every waypoint must carry a finite, in-range coordinate.
    coords = []
    for i, wp in enumerate(route):
        lat = _num(wp.get("latitude")) if isinstance(wp, dict) else None
        lng = _num(wp.get("longitude")) if isinstance(wp, dict) else None
        if lat is None or abs(lat) > 90 or lng is None or abs(lng) > 180:
            errors.append(f"Route waypoint {i + 1} has an invalid latitude/longitude.")
        else:
            coords.append((lng, lat))
    checks["waypoints_finite"] = len(coords) == len(route)

    n = len(route)
    checks["waypoint_count"] = n
    if n < min_waypoints:
        errors.append(f"Route has {n} waypoint(s); at least {min_waypoints} are required.")
    if max_route_waypoints is not None and n > max_route_waypoints:
        errors.append(f"Route has {n} waypoints, above the supported limit of {max_route_waypoints}.")

    segs = raw.get("segments") if isinstance(raw, dict) else None
    segs = segs if isinstance(segs, list) else []

    # ── Structural integrity of the segmentation ────────────────────────────────────────
    if segs:
        # Every segment has enough points, and adjacent segment endpoints coincide (there is
        # no invisible straight jump that becomes a real Pixhawk leg only after flattening).
        prev_end = None
        no_jump = True
        for i, s in enumerate(segs):
            sc = s.get("coordinates") or []
            if len(sc) < 2:
                errors.append(f"Segment {i + 1} ({s.get('kind')}) has fewer than 2 points.")
                continue
            if prev_end is not None and not _close(prev_end, sc[0]):
                no_jump = False
                errors.append(
                    f"Invisible jump between segment {i} and {i + 1} — "
                    f"{s.get('kind')} does not start where the previous segment ends.")
            prev_end = sc[-1]
        checks["no_invisible_jumps"] = no_jump

        # Segment flattening must reproduce route_waypoints EXACTLY (7-dp), so the displayed
        # segments and the uploaded flat route are provably the same geometry.
        flat, _ = _flatten_segments([{**s, "segment_id": s.get("segment_id", f"seg-{i}"),
                                      "coordinates": s.get("coordinates") or []}
                                     for i, s in enumerate(segs)])
        flat_r = _route_waypoints(_dedup(flat))
        checks["flatten_matches_route"] = (
            [(w["latitude"], w["longitude"]) for w in flat_r]
            == [(round(_num(w.get("latitude")), 7), round(_num(w.get("longitude")), 7))
                for w in route if isinstance(w, dict) and _num(w.get("latitude")) is not None])
        if checks["flatten_matches_route"] is False:
            errors.append("The segment flattening does not equal the route waypoints — "
                          "the displayed segments and the upload route disagree.")
    else:
        checks["no_invisible_jumps"] = None
        checks["flatten_matches_route"] = None

    # ── THE AUTHORITATIVE GEOMETRY CONTRACT ──────────────────────────────────────────────
    # The same single proof generation and finalization run: every route leg inside
    # navigable ∪ home_corridor, every leg outside the effective no-go exclusion, Home covered.
    # The corridor is taken from the submitted package when it carries one and is otherwise
    # RE-DERIVED from the submitted segments — never invented, never widened, and the raw
    # operator boundary is never substituted for the navigable geometry.
    #
    # It runs BEFORE the per-segment sweep below, which it subsumes: the sweep still computes
    # its long-standing `checks` keys, but its error text is suppressed for any fault this proof
    # has already named, so the operator reads each geometric fault once with its code.
    navigable_rings = _first_present(
        raw.get("navigable_geometry") if isinstance(raw, dict) else None,
        raw.get("navigable_boundary") if isinstance(raw, dict) else None,
        _navigable_rings_deg(boundary, clearance))
    #
    # The APPROVED transit geometry is the submitted execution transit legs plus any submitted
    # planning-only legs (`first_approach`'s Home → A1 connector). Validation re-derives the
    # corridor from that same authoritative set — never from the execution subset alone, which
    # would refuse a corridor the mission's own approved geometry proves.
    corridor = raw.get("home_corridor") if isinstance(raw, dict) else None
    raw_planning_only = raw.get("planning_only_transit_segments") if isinstance(raw, dict) else None
    planning_only = [s for s in (raw_planning_only or []) if isinstance(s, dict)]
    if corridor is None and (segs or planning_only) and inp["home"] is not None:
        corridor, _ = home_corridor_ring(
            segments=segs, planning_only_transit_segments=planning_only,
            navigable_geometry=navigable_rings, no_go_zones=zones,
            planning_home=inp["home"], no_go_clearance_m=no_go_clearance)
    geometry = check_mission_geometry(
        segments=segs, planning_only_transit_segments=planning_only,
        route_waypoints=route, navigable_geometry=navigable_rings,
        no_go_zones=zones, no_go_clearance_m=no_go_clearance, planning_home=inp["home"],
        home_corridor=corridor)
    geometry_codes = {f["code"] for f in geometry["failures"]}
    checks["geometry_consistent"] = geometry["ok"]
    checks["geometry_codes"] = sorted(geometry_codes)
    checks["geometry"] = geometry["checks"]
    for failure in geometry["failures"]:
        errors.append(f"{failure['code']}: {failure['message']}.")
    reported_outside = "ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY" in geometry_codes
    reported_no_go = "ROUTE_NO_GO_VIOLATION" in geometry_codes

    # ── Geometry containment (per segment, against the navigable region) ─────────────────
    if grid is not None and not grid.empty and segs:
        inside_ok = True
        clears_ok = True
        for i, s in enumerate(segs):
            sc = [(p[0], p[1]) for p in (s.get("coordinates") or [])]
            if len(sc) < 2:
                continue
            try:
                lp = transform(grid.to_proj.transform, LineString(sc))
            except Exception:
                continue
            if s.get("kind") in _INSIDE_KINDS and not grid._seg_covered(lp):
                inside_ok = False
                if not reported_outside:
                    errors.append(f"Segment {i + 1} ({s.get('kind')}) leaves the navigable "
                                  f"(shoreline-offset) area.")
            if not grid._seg_clears_nogo(lp):
                clears_ok = False
                if not reported_no_go:
                    errors.append(
                        f"Segment {i + 1} ({s.get('kind')}) crosses the no-go exclusion"
                        + (f" (no-go clearance {no_go_clearance} m around a no-go zone)."
                           if no_go_clearance > TOLERANCE else " (a no-go interior)."))
        checks["segments_within_navigable"] = inside_ok
        checks["coverage_within_navigable"] = all(
            grid._seg_covered(transform(grid.to_proj.transform,
                                        LineString([(p[0], p[1]) for p in s["coordinates"]])))
            for s in segs if s.get("kind") in _COVERAGE_KINDS and len(s.get("coordinates") or []) >= 2)
        checks["route_clears_no_go"] = clears_ok
    else:
        checks["segments_within_navigable"] = None
        checks["coverage_within_navigable"] = None
        checks["route_clears_no_go"] = None

    # ── Route WAYPOINTS clear the no-go exclusion ────────────────────────────────────────
    # Checked independently of the segment sweep above, so a route submitted WITHOUT segments
    # is still held to the operator's no-go clearance. This is the waypoint half of the
    # guarantee; the segment sweep is the leg half — a plan must pass both.
    if grid is not None and zones:
        offenders = [i + 1 for i, pt in enumerate(coords) if not grid.point_clears_nogo(pt)]
        checks["waypoints_clear_no_go"] = not offenders
        if offenders and not reported_no_go:
            shown = offenders[:5]
            errors.append(
                f"Route waypoint(s) {shown}{' …' if len(offenders) > len(shown) else ''} lie "
                f"inside the no-go exclusion"
                + (f" (no-go clearance {no_go_clearance} m around a no-go zone)."
                   if no_go_clearance > TOLERANCE else " (a no-go zone)."))
    else:
        checks["waypoints_clear_no_go"] = None

    # ── Operator approach/return points appear in the executed order ─────────────────────
    def _in_route(pt):
        return any(_close([lng, lat], pt) for (lng, lat) in coords)

    if inp["approach_waypoints"]:
        missing = [i + 1 for i, p in enumerate(inp["approach_waypoints"]) if not _in_route(p)]
        checks["approach_in_execution_order"] = not missing
        if missing:
            errors.append(f"Approach waypoint(s) {missing} are not in the executed route.")
    if inp["return_waypoints"]:
        missing = [i + 1 for i, p in enumerate(inp["return_waypoints"]) if not _in_route(p)]
        checks["return_in_execution_order"] = not missing
        if missing:
            errors.append(f"Return waypoint(s) {missing} are not in the executed route.")

    # ── Mission identity: the route must canonicalize + hash, and match a supplied hash ──
    try:
        computed_hash = _route_hash(route)
        checks["hash_ok"] = True
        supplied_hash = raw.get("route_hash") if isinstance(raw, dict) else None
        if supplied_hash and str(supplied_hash) != str(computed_hash):
            errors.append("The submitted route hash does not match the route — the plan may "
                          "have been altered after generation.")
        checks["hash_matches"] = (supplied_hash is None) or (str(supplied_hash) == str(computed_hash))
    except Exception:
        checks["hash_ok"] = False
        errors.append("The route could not be canonicalized/hashed for upload.")

    # input_revision, when supplied, must match the inputs being validated (route not stale).
    supplied_rev = raw.get("input_revision") if isinstance(raw, dict) else None
    if supplied_rev is not None:
        checks["input_revision_matches"] = (str(supplied_rev) == _input_revision(inp))
        if not checks["input_revision_matches"]:
            errors.append("The route was generated from different inputs than those being "
                          "validated — regenerate before upload.")

    checks["home_set"] = inp["home"] is not None
    if inp["home"] and _point_in_any_zone(inp["home"], zones):
        errors.append("The planning home lies inside a no-go zone.")

    if not checks.get("home_set"):
        warnings.append("No planning home is set.")
    if not inp["approach_waypoints"]:
        warnings.append("No explicit approach waypoints are defined.")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}
