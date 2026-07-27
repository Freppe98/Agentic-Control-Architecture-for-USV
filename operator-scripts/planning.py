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
import json
import math
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
# from the request — never an invented sonar-specific default baked in as truth.
DEFAULT_PLANNING_SPEED_MPS = 1.5


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


class _NavGrid:
    """The shared navigable model + safe point-to-point connector for one generation.

    Built ONCE per generate_survey call so every connector (coverage lane turns, no-go-split
    sections, pass transition, survey-entry/return connectors) is judged against, and routed
    inside, the identical navigable geometry. The navigable region is the survey boundary
    inset by the shoreline clearance MINUS the union of the no-go zones — the exact space a
    coverage/inter-coverage connector must stay within.

    ONE strategy, deterministic: a direct segment is accepted only when it is covered by the
    navigable polygon (within COVER_TOL_M) and clears no-go interiors; otherwise a bounded
    4-neighbour grid A* (adapted from the ported compute_return_path) finds an orthogonal
    safe path. No diagonal shortcuts, no second planner."""

    def __init__(self, boundary, clearance, zones, step_m):
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
        self.nogo = unary_union(zpolys) if zpolys else None

        nav = inset
        if self.nogo is not None and not nav.is_empty:
            nav = nav.difference(self.nogo)
        self.navigable = nav

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

    @property
    def empty(self):
        return self.navigable is None or self.navigable.is_empty

    @property
    def disconnected(self):
        return len(self.components) > 1

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
        # A cell is free (0) when its centre is inside the navigable region (already excludes
        # no-go and shore). A small buffer keeps cells exactly on the clipped inset edge free.
        nav = self.navigable.buffer(COVER_TOL_M)
        grid = []
        for r in range(rows):
            y = miny + r * step
            row = [0 if nav.contains(Point(minx + c * step, y)) else 1 for c in range(cols)]
            grid.append(row)
        self._grid = grid
        self._bounds = (minx, miny, maxx, maxy, cols, rows)

    def _seg_covered(self, line_proj):
        """True when a projected segment stays inside the navigable region (within tol)."""
        outside = line_proj.difference(self.navigable.buffer(COVER_TOL_M))
        return outside.is_empty or outside.length < CONNECTOR_EPS_M

    def _seg_clears_nogo(self, line_proj):
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

    def safe_connector(self, a_deg, b_deg, require_inside=True):
        """A safe [[lng,lat],...] path from a to b. The direct segment when it is safe;
        otherwise a bounded grid A* path inside the navigable region. Raises ConnectorError
        when neither the direct segment nor any bounded safe path exists."""
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

        def nearest_free(r, c):
            if is_free(r, c):
                return (r, c)
            for dist in range(1, max(rows, cols)):
                for dr in range(-dist, dist + 1):
                    for dc in range(-dist, dist + 1):
                        if is_free(r + dr, c + dc):
                            return (r + dr, c + dc)
            return None

        start = nearest_free(*to_grid(a_deg))
        goal = nearest_free(*to_grid(b_deg))
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

        path = [list(a_deg)] + [to_coord(rc) for rc in cells] + [list(b_deg)]
        return _dedup(path)

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

    grid = _NavGrid(boundary, clearance, zones, step_m=spacing)
    if grid.empty:
        raise ValueError(
            "The navigable area is empty after applying the shoreline clearance and no-go "
            "zones — reduce the clearance/zones or enlarge the boundary.")
    if grid.disconnected:
        raise DisconnectedNavigableError(
            f"The shoreline clearance and no-go zones split the survey into "
            f"{len(grid.components)} disconnected navigable regions. Survey generation "
            f"currently requires one connected navigable region — split the survey into "
            f"separate missions, or adjust the clearance / no-go zones.")

    # Coverage passes: the ported lawnmower, then every UNSAFE internal hop repaired inside
    # the navigable region (this is the fix for outside-polygon lane connectors).
    primary_raw = _dedup(run_lawnmower_with_obstacles(
        boundary, spacing, inp["primary_angle_deg"], clearance, zones or None))
    if len(primary_raw) < 2:
        raise ValueError(
            "No coverage route could be generated — the navigable area may be too small "
            "for the chosen lane spacing, or fully blocked by no-go zones.")
    primary_coords = grid.repair_path(primary_raw)

    secondary_coords = None
    intersections = []
    if inp["dual_pass"]:
        secondary_raw = _dedup(run_lawnmower_with_obstacles(
            boundary, spacing, inp["secondary_angle_deg"], clearance, zones or None))
        if len(secondary_raw) < 2:
            warnings.append("Dual pass requested, but the secondary pass produced no route; "
                            "only the primary pass was generated.")
        else:
            secondary_coords = grid.repair_path(secondary_raw)
            intersections = _pass_intersections(primary_coords, secondary_coords)

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

    segments = []
    seq = [0]

    def new_seg(kind, coords):
        seq[0] += 1
        return {"segment_id": f"seg-{seq[0]:02d}-{kind}", "kind": kind,
                "coordinates": [[float(c[0]), float(c[1])] for c in _dedup(coords)],
                "length_m": round(_path_length_m(_dedup(coords)), 2)}

    def connect(a, b, kind, require_inside):
        """Append a connector segment a→b if a and b are not already the same point."""
        if _close(a, b):
            return
        path = grid.safe_connector(a, b, require_inside=require_inside)
        if len(path) >= 2:
            segments.append(new_seg(kind, path))

    # 1. START CONNECTOR + APPROACH + SURVEY ENTRY CONNECTOR
    start_anchor = home if start_mode == "planning_home" else None
    if approach:
        if start_anchor is not None:
            connect(start_anchor, approach[0], "start_connector", require_inside=False)
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
        connect(approach[-1], survey_entry, "survey_entry_connector", require_inside=True)
    elif start_anchor is not None:
        # No approach WPs: planning home → survey entry is the single entry connector.
        connect(start_anchor, survey_entry, "survey_entry_connector", require_inside=True)
    else:
        warnings.append("No planning home and no approach waypoints — the route begins "
                        "directly at the survey entry.")

    # 2. PRIMARY COVERAGE
    segments.append(new_seg("primary", primary_coords))

    # 3. PASS TRANSITION + SECONDARY COVERAGE
    if secondary_coords:
        connect(primary_coords[-1], secondary_coords[0], "pass_transition", require_inside=True)
        segments.append(new_seg("secondary", secondary_coords))

    # 4. RETURN CONNECTOR + RETURN APPROACH + FINAL HOME CONNECTOR
    if returns:
        connect(coverage_end, returns[0], "return_connector", require_inside=True)
        return_path = [returns[0]]
        for a, b in zip(returns, returns[1:]):
            if grid.segment_is_safe(a, b, require_inside=False):
                return_path.append(b)
            else:
                return_path.extend(grid.safe_connector(a, b, require_inside=False)[1:])
        if len(return_path) >= 2:
            segments.append(new_seg("return_approach", return_path))
        if home is not None:
            connect(returns[-1], home, "final_home_connector", require_inside=False)
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

    input_revision = _input_revision(inp)
    planning_inputs = {
        "boundary": boundary,
        "shoreline_clearance_m": clearance,
        "navigable_boundary": navigable_boundary,
        "no_go_zones": zones,
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
        "segments": segments,
        "original_execution_order": original_execution_order,
        "route_waypoints": route_waypoints,
        "route_hash": _route_hash(route_waypoints),
        "metrics": metrics,
        "intersections": intersections,
        "navigable_boundary": navigable_boundary,
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
    zones = inp["no_go_zones"]

    inset = _inset_polygon(boundary, clearance)
    checks["boundary_valid"] = True
    checks["inset_nonempty"] = bool(inset and not inset.is_empty)
    if not checks["inset_nonempty"]:
        errors.append("The survey boundary is empty after applying the shoreline clearance.")

    grid = None
    try:
        grid = _NavGrid(boundary, clearance, zones, step_m=inp["lane_spacing_m"])
        checks["navigable_connected"] = not grid.disconnected and not grid.empty
        if grid.disconnected:
            errors.append("The navigable region is not connected — survey generation "
                          "requires one connected navigable region.")
    except Exception:
        checks["navigable_connected"] = None

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
                errors.append(f"Segment {i + 1} ({s.get('kind')}) leaves the navigable "
                              f"(shoreline-offset) area.")
            if not grid._seg_clears_nogo(lp):
                clears_ok = False
                errors.append(f"Segment {i + 1} ({s.get('kind')}) crosses a no-go interior.")
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
