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

import math
from datetime import datetime, timezone

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

    transit = []
    for i, p in enumerate(raw.get("transit_waypoints") or []):
        pt = _point_of(p)
        if pt is None:
            errors.append(f"Transit waypoint {i + 1} is not a valid [lng, lat] point.")
        else:
            transit.append(pt)

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
        "transit_waypoints": transit,
    }


def generate_survey(raw_inputs, max_route_waypoints=None):
    """Generate a segmented side-scan survey from the operator's planning inputs.

    Returns a dict with `segments` (typed geometry for the map overlay), `route_waypoints`
    (the flat, ordered mission-contract route), `metrics`, `intersections` (dual-pass
    primary∩secondary as planning metadata) and `warnings`. Segment purpose is preserved —
    the flat route never loses which leg is transit / primary / transition / secondary /
    return. Deterministic: the same inputs always produce the same route."""
    _require_available()
    inp = normalize_generate_inputs(raw_inputs)

    boundary = inp["boundary"]
    clearance = inp["shoreline_clearance_m"]
    spacing = inp["lane_spacing_m"]
    zones = inp["no_go_zones"]
    warnings = []

    # The navigable region: boundary inset by the shoreline clearance. A non-empty inset is
    # a hard precondition for any coverage — reported as an error, not silently empty.
    inset = _inset_polygon(boundary, clearance)
    if inset is None or inset.is_empty:
        raise ValueError(
            "The survey boundary is empty after applying the shoreline clearance — "
            "reduce the clearance or enlarge the boundary.")

    primary_coords = _dedup(run_lawnmower_with_obstacles(
        boundary, spacing, inp["primary_angle_deg"], clearance, zones or None))
    if len(primary_coords) < 2:
        raise ValueError(
            "No coverage route could be generated — the navigable area may be too small "
            "for the chosen lane spacing, or fully blocked by no-go zones.")

    segments = []
    exec_coords = []

    # 1. Transit (optional) — operator-supplied waypoints from home toward the survey start.
    #    Home is NOT emitted as a route waypoint: Scout owns Pixhawk seq 0 / Home and an
    #    upload must never silently move HOME_POSITION.
    if inp["transit_waypoints"]:
        transit_pts = list(inp["transit_waypoints"])
        segments.append(_seg("transit", transit_pts))
        exec_coords.extend(transit_pts)

    # 2. Primary coverage pass.
    segments.append(_seg("primary", primary_coords))
    exec_coords.extend(primary_coords)

    intersections = []
    if inp["dual_pass"]:
        secondary_coords = _dedup(run_lawnmower_with_obstacles(
            boundary, spacing, inp["secondary_angle_deg"], clearance, zones or None))
        if len(secondary_coords) < 2:
            warnings.append("Dual pass requested, but the secondary pass produced no route; "
                            "only the primary pass was generated.")
        else:
            # 3. Approved transition — a single straight connector from the primary pass end
            #    to the secondary pass start, kept as its OWN segment (not an arbitrary
            #    diagonal graph — exactly one connector, explicitly labelled).
            transition = [primary_coords[-1], secondary_coords[0]]
            segments.append(_seg("transition", transition))
            exec_coords.append(secondary_coords[0])
            # 4. Secondary coverage pass.
            segments.append(_seg("secondary", secondary_coords))
            exec_coords.extend(secondary_coords[1:])
            intersections = _pass_intersections(primary_coords, secondary_coords)

    # 5. Return / transit home (optional) — only when a planning home is defined.
    if inp["home"]:
        ret = _dedup(compute_return_path(
            boundary, spacing, clearance, exec_coords[-1], inp["home"], zones or None))
        if len(ret) >= 2:
            segments.append(_seg("return", ret))
            exec_coords.extend(ret[1:] if ret[0] == exec_coords[-1] else ret)
        else:
            warnings.append("No safe return route to the planning home could be found inside "
                            "the navigable area — the route ends at the last coverage waypoint.")
        if _point_in_any_zone(inp["home"], zones):
            warnings.append("The planning home lies inside a no-go zone.")
    else:
        warnings.append("No planning home is set — no return route was generated.")

    exec_coords = _dedup(exec_coords)
    route_waypoints = _route_waypoints(exec_coords)
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

    coverage_len = sum(s["length_m"] for s in segments if s["kind"] in ("primary", "secondary"))
    transit_len = sum(s["length_m"] for s in segments if s["kind"] in ("transit", "transition", "return"))
    total_len = round(coverage_len + transit_len, 2)

    speed = inp["survey_speed_mps"] or DEFAULT_PLANNING_SPEED_MPS
    duration_s = round(total_len / speed, 1) if speed > 0 else None

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
        "survey_speed_mps": speed,
        "survey_speed_is_default": inp["survey_speed_mps"] is None,
        "estimated_duration_s": duration_s,
    }

    return {
        "ok": True,
        "contract_version": "mission-contract-v1",
        "segments": segments,
        "route_waypoints": route_waypoints,
        "metrics": metrics,
        "intersections": intersections,
        "navigable_boundary": _navigable_rings_deg(boundary, clearance),
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
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


def validate_plan(raw, max_route_waypoints=None, min_waypoints=2):
    """Deterministic pre-upload validation of a generated plan. `raw` carries the planning
    inputs plus the generated `route_waypoints` (as produced by generate_survey). Returns
    {ok, errors, warnings, checks} — errors block upload; warnings do not. Never repairs
    geometry, only reports."""
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

    # Coverage must stay inside the navigable (inset) region; the whole route must clear
    # no-go interiors. Transit/return legs legitimately leave the navigable area (they run
    # to/from shore), so the inset check applies to COVERAGE segments only — which is why
    # validate consumes the generated `segments`, not just the flat route.
    if inset is not None and not inset.is_empty and coords:
        c = Polygon([(p[0], p[1]) for p in boundary]).centroid
        zone = int((c.x + 180) / 6) + 1
        hemi = 6 if c.y >= 0 else 7
        to_proj = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:32{hemi}{zone:02d}", always_xy=True)
        line_proj = transform(to_proj.transform, LineString(coords))

        segs = raw.get("segments") if isinstance(raw, dict) else None
        coverage_pts = []
        if isinstance(segs, list):
            for s in segs:
                if isinstance(s, dict) and s.get("kind") in ("primary", "secondary"):
                    coverage_pts.extend([(p[0], p[1]) for p in (s.get("coordinates") or [])])
        if len(coverage_pts) >= 2:
            cov_proj = transform(to_proj.transform, LineString(coverage_pts))
            # A small tolerance buffer absorbs projection/rounding noise at the boundary
            # itself (the generator clips exactly to the inset edge); a real excursion is
            # far larger.
            outside = cov_proj.difference(inset.buffer(0.5))
            checks["coverage_within_navigable"] = outside.is_empty or outside.length < 1.0
            if not checks["coverage_within_navigable"]:
                warnings.append("Part of the coverage route lies outside the navigable "
                                "(shoreline-inset) area — review the route before upload.")
        else:
            checks["coverage_within_navigable"] = None  # no segment metadata to check against

        crosses = []
        for i, z in enumerate(zones):
            try:
                zp = transform(to_proj.transform, Polygon([(pt[0], pt[1]) for pt in z]).buffer(0))
                if line_proj.intersection(zp.buffer(-0.5)).length > 1.0:
                    crosses.append(i + 1)
            except Exception:
                continue
        checks["route_clears_no_go"] = not crosses
        if crosses:
            errors.append(f"The route crosses the interior of no-go zone(s) {crosses}.")

    checks["home_set"] = inp["home"] is not None
    if inp["home"] and _point_in_any_zone(inp["home"], zones):
        errors.append("The planning home lies inside a no-go zone.")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}
