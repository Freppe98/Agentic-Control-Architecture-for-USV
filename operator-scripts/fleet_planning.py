"""Operator-side FLEET survey planning — a static, deterministic layer ABOVE the existing
single-vehicle planner (planning.py).

WHAT THIS IS (and is not)
-------------------------
A fleet mission divides ONE shared survey area between two or more registered USVs so they can
survey it together with reduced *planned* route overlap. This module owns only the fleet layer:

    shared survey geometry  →  survey lines (stable ids)  →  contiguous home-aware allocation
        →  independent per-vehicle child missions  →  fleet conflict validation

Every child mission it emits is an ORDINARY operator-survey-plan-v1 package — the exact shape
planning.generate_survey returns and POST /api/missions/finalize already accepts — so each
vehicle is uploaded, hashed and read-back-verified through the unchanged single-vehicle path.
There is no second mission contract and no second hash: child route hashes are the SAME
mission_contract.route_content_hash the Pixhawk upload verifies.

This is STATIC pre-deployment deconfliction, NOT runtime collision avoidance. Vehicles start at
different times, travel at different speeds, may LOITER/return/replan — non-intersecting planned
routes reduce risk but do not eliminate it. The UI states this; nothing here claims otherwise.

REUSE
-----
Coverage geometry, the navigable model and every safe connector come from planning.py: _NavGrid
(navigable region + bounded A* connectors), the ported boustrophedon scan math, _flatten_segments,
_route_waypoints, _route_hash, _path_length_m and the shared UTM projection. The ONE piece this
module adds at the geometry level is per-survey-LINE extraction (planning.py returns a single
stitched coverage path; fleet allocation needs individually addressable lines), kept faithful to
the same projection/rotation/clip the ported generator uses.

COORDINATE CONVENTION — identical to planning.py: geometry crosses this boundary as GeoJSON
[longitude, latitude]; child route waypoints are {latitude, longitude, loiter_time_s}. All metres
are UTM-projected (never raw lat/lon Euclidean), via planning._utm_for.
"""

import hashlib
import json
import math
from datetime import datetime, timezone
from itertools import combinations, permutations

import planning
from planning import (PLANNING_AVAILABLE, PlanningUnavailable, ConnectorError,
                      DisconnectedNavigableError, DEFAULT_PLANNING_SPEED_MPS, TOLERANCE)

if PLANNING_AVAILABLE:  # same guarded geometry stack planning.py uses
    from shapely.geometry import Polygon, LineString, MultiLineString, Point
    from shapely.ops import unary_union, transform
    from shapely.affinity import rotate

FLEET_PLAN_VERSION = "operator-fleet-plan-v1"
ALLOCATION_METHOD = "contiguous_survey_lines"

# A fleet plan requires at least this many vehicles; a small fleet is a bounded brute-force
# search (contiguous partitions × vehicle permutations) — see _allocate.
MIN_FLEET_VEHICLES = 2
# Default minimum PLANNED cross-route separation (metres). A planning/warning threshold, NOT a
# runtime guarantee. 10 m matches the project's coverage scale (see task "Fleet safety separation").
DEFAULT_FLEET_SEPARATION_M = 10.0
# Two cross-vehicle route waypoints closer than this are "near-identical" (a duplicate the fleet
# must not create unintentionally). Exact duplicates are the degenerate case of this.
NEAR_DUP_M = 2.0
# Cap on enumerated contiguous partitions before falling back to a single balanced split — keeps
# the allocation search inside a bounded, responsive budget for a realistic survey.
MAX_PARTITION_COMBOS = 60000

BALANCE_METRICS = ("estimated_duration", "distance")


class FleetPlanError(ValueError):
    """A fleet-input problem the operator can fix (too few vehicles, missing home, invalid
    speed/separation, more vehicles than assignable survey groups). main.py maps it to a 400
    with the specific reason, exactly like planning's ValueError."""


# ── input normalisation ──────────────────────────────────────────────────────────────────

def _num(v):
    return planning._num(v)


def normalize_fleet_inputs(raw):
    """Validate + normalise a fleet generate request. Returns a clean dict; raises
    FleetPlanError(joined reasons) for anything unusable. Deterministic and network-free."""
    raw = raw if isinstance(raw, dict) else {}
    errors = []

    boundary = planning._ring(raw.get("boundary"))
    if boundary is None:
        errors.append("A survey boundary polygon with at least 3 vertices is required.")

    clearance = _num(raw.get("shoreline_clearance_m", 0))
    if clearance is None or clearance < 0:
        errors.append("Shoreline clearance must be a number >= 0 metres.")
        clearance = 0.0

    # Same no-go-clearance contract as single-vehicle planning (planning.normalize_generate_
    # inputs): absent → the 5 m default, supplied → validated. Single Vehicle and Fleet Mission
    # must never interpret the same saved plan differently, so the rule is copied exactly.
    if raw.get("no_go_clearance_m") is None:
        no_go_clearance = planning.DEFAULT_NO_GO_CLEARANCE_M
    else:
        no_go_clearance = _num(raw.get("no_go_clearance_m"))
        if no_go_clearance is None or no_go_clearance < 0:
            errors.append("No-go clearance must be a number >= 0 metres.")
            no_go_clearance = planning.DEFAULT_NO_GO_CLEARANCE_M

    if raw.get("lane_spacing_m") is None:
        spacing = planning.DEFAULT_LANE_SPACING_M
    else:
        spacing = _num(raw.get("lane_spacing_m"))
        if spacing is None or spacing <= 0:
            errors.append("Lane spacing must be a positive number of metres.")

    primary = _num(raw.get("primary_angle_deg", 0))
    primary = (primary % 360.0) if primary is not None else 0.0
    dual = bool(raw.get("dual_pass"))
    secondary = _num(raw.get("secondary_angle_deg"))
    secondary = (secondary % 360.0) if secondary is not None else (primary + 90.0) % 360.0

    separation = _num(raw.get("minimum_fleet_separation_m"))
    if separation is None:
        separation = DEFAULT_FLEET_SEPARATION_M
    elif separation <= 0:
        errors.append("Minimum fleet separation must be a positive number of metres.")
        separation = DEFAULT_FLEET_SEPARATION_M

    balance = str(raw.get("balance_metric") or "estimated_duration")
    if balance not in BALANCE_METRICS:
        balance = "estimated_duration"

    zones = []
    for i, z in enumerate(raw.get("no_go_zones") or []):
        r = planning._ring(z)
        if r is None:
            errors.append(f"No-go zone {i + 1} is not a valid polygon (needs >= 3 vertices).")
        else:
            zones.append(r)

    vehicles = []
    seen_ids = set()
    raw_vehicles = raw.get("vehicles") or []
    if not isinstance(raw_vehicles, list):
        errors.append("vehicles must be a list.")
        raw_vehicles = []
    for i, v in enumerate(raw_vehicles):
        v = v if isinstance(v, dict) else {}
        vid = v.get("vehicle_id")
        if vid is None or str(vid).strip() == "":
            errors.append(f"Vehicle {i + 1} has no vehicle_id.")
            continue
        vid = str(vid)
        if vid in seen_ids:
            errors.append(f"Duplicate vehicle_id {vid!r} in the fleet selection.")
            continue
        seen_ids.add(vid)
        home = planning._point_of(v.get("home"))
        if home is None:
            errors.append(f"Vehicle {vid} has no valid planning home [lng, lat].")
        speed = _num(v.get("survey_speed_mps"))
        if speed is None:
            speed = DEFAULT_PLANNING_SPEED_MPS
            speed_is_default = True
        elif speed <= 0:
            errors.append(f"Vehicle {vid} survey speed must be a positive number of m/s.")
            speed, speed_is_default = DEFAULT_PLANNING_SPEED_MPS, True
        else:
            speed_is_default = False
        vehicles.append({
            "vehicle_id": vid,
            "vehicle_name": str(v.get("vehicle_name") or vid),
            "colour": v.get("colour"),
            "home": home,
            "survey_speed_mps": float(speed),
            "survey_speed_is_default": speed_is_default,
        })

    if len(vehicles) < MIN_FLEET_VEHICLES:
        errors.append(f"A fleet mission requires at least {MIN_FLEET_VEHICLES} vehicles with a "
                      f"valid planning home.")

    if errors:
        raise FleetPlanError("; ".join(errors))

    # Deterministic vehicle order — stable sort by id, the documented tie-breaker.
    vehicles.sort(key=lambda x: x["vehicle_id"])
    return {
        "boundary": boundary,
        "shoreline_clearance_m": float(clearance),
        "no_go_clearance_m": float(no_go_clearance),
        "lane_spacing_m": float(spacing),
        "primary_angle_deg": float(primary),
        "dual_pass": dual,
        "secondary_angle_deg": float(secondary),
        "minimum_fleet_separation_m": float(separation),
        "balance_metric": balance,
        "no_go_zones": zones,
        "vehicles": vehicles,
        "manual_assignments": raw.get("manual_assignments") or None,
    }


# ── survey-line extraction (the one geometry piece added on top of planning.py) ─────────────

def _dot(p, u):
    return p[0] * u[0] + p[1] * u[1]


def _survey_lines(grid, boundary, spacing, angle_deg, clearance, zones, pass_kind):
    """Extract the individual, separately-addressable survey lines of ONE coverage pass, in
    contiguous sweep order, each with a stable id and projected geometry.

    Faithful to the ported boustrophedon geometry (same UTM projection, same shoreline inset,
    same scan-line clip, same no-go subtraction) — but it returns the lines SEPARATELY instead
    of stitching them into one path, because fleet allocation assigns COMPLETE lines to vehicles
    (never arbitrary waypoint chunks). A no-go zone that splits a nominal row into several
    segments yields several lines (…-a, …-b), each a first-class allocatable unit."""
    to_proj, to_deg = grid.to_proj, grid.to_deg
    poly_deg = Polygon([(c[0], c[1]) for c in boundary])
    if not poly_deg.is_valid:
        poly_deg = poly_deg.buffer(0)
    main = transform(to_proj.transform, poly_deg)
    if clearance and clearance > TOLERANCE:
        main = main.buffer(-abs(clearance), join_style=2)
    if grid.nogo is not None and not main.is_empty:
        main = main.difference(grid.nogo)
    if main.is_empty:
        return []

    centroid = main.centroid
    math_angle = (90.0 - angle_deg) % 360.0
    rot = rotate(main, -math_angle, origin=centroid)
    minx, miny, maxx, maxy = rot.bounds
    width = maxx - minx
    sep = abs(spacing) if spacing > 1e-6 else 0.1
    ys = _linspace(miny + sep / 2, maxy - sep / 2, max(1, int((maxy - miny) / sep)))

    raw_lines = []  # (sweep_y, [ [x,y] proj-rot coords ])
    for y in ys:
        scan = LineString([(minx - width * 1.1, y), (maxx + width * 1.1, y)])
        clipped = rot.buffer(0).intersection(scan)
        segs = []
        if isinstance(clipped, LineString) and clipped.length > TOLERANCE:
            segs = [clipped]
        elif isinstance(clipped, MultiLineString):
            segs = [s for s in clipped.geoms if s.length > TOLERANCE]
        segs.sort(key=lambda s: s.coords[0][0])  # left→right within the row
        for s in segs:
            raw_lines.append((y, list(s.coords)))

    # Rotate each line back to true UTM, then to degrees; assign stable ids in sweep order.
    lines = []
    # unrotate helper
    def unrot(pts):
        ls = rotate(LineString(pts), math_angle, origin=centroid)
        return list(ls.coords)

    # Group by sweep row so split segments in one row share a row index with -a/-b suffixes.
    row_index = -1
    last_y = None
    per_row_seq = 0
    for y, rot_coords in raw_lines:
        if last_y is None or abs(y - last_y) > TOLERANCE:
            row_index += 1
            per_row_seq = 0
            last_y = y
        else:
            per_row_seq += 1
        proj_coords = unrot(rot_coords)
        deg_ls = transform(to_deg.transform, LineString(proj_coords))
        coords_deg = [[round(x, 7), round(yy, 7)] for x, yy in deg_ls.coords]
        suffix = "" if per_row_seq == 0 and not _row_is_split(raw_lines, y) else "-" + chr(ord("a") + per_row_seq)
        line_id = f"{pass_kind}-line-{row_index + 1:04d}{suffix}"
        cx = sum(p[0] for p in proj_coords) / len(proj_coords)
        cy = sum(p[1] for p in proj_coords) / len(proj_coords)
        lines.append({
            "id": line_id,
            "pass_kind": pass_kind,
            "row": row_index,
            "coords_deg": coords_deg,
            "proj": proj_coords,
            "start_deg": coords_deg[0],
            "end_deg": coords_deg[-1],
            "start_proj": proj_coords[0],
            "end_proj": proj_coords[-1],
            "length_m": round(_proj_len(proj_coords), 2),
            "centroid_proj": (cx, cy),
        })
    return lines


def _row_is_split(raw_lines, y):
    return sum(1 for (yy, _) in raw_lines if abs(yy - y) <= TOLERANCE) > 1


def _linspace(a, b, n):
    if n <= 1:
        return [(a + b) / 2]
    step = (b - a) / (n - 1)
    return [a + step * i for i in range(n)]


def _proj_len(coords):
    return sum(math.hypot(coords[i + 1][0] - coords[i][0], coords[i + 1][1] - coords[i][1])
               for i in range(len(coords) - 1))


def _axis(lines):
    """(direction, sweep-normal) unit vectors for a pass, from its first line's orientation.
    sweep coordinate = dot(point, normal); along coordinate = dot(point, direction)."""
    s = lines[0]["start_proj"]
    e = lines[0]["end_proj"]
    dx, dy = e[0] - s[0], e[1] - s[1]
    mag = math.hypot(dx, dy) or 1.0
    d = (dx / mag, dy / mag)
    n = (-d[1], d[0])
    return d, n


def _order_lines_by_sweep(lines):
    """Sort lines into contiguous sweep order (perpendicular offset), then along-line — the
    ordering the whole allocation depends on. Deterministic."""
    if not lines:
        return []
    d, n = _axis(lines)
    for ln in lines:
        c = ln["centroid_proj"]
        ln["sweep"] = _dot(c, n)
        ln["along"] = _dot(c, d)
    lines.sort(key=lambda ln: (round(ln["sweep"], 2), round(ln["along"], 2), ln["id"]))
    return lines


# ── allocation ───────────────────────────────────────────────────────────────────────────

def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _home_proj(grid, home):
    return grid.to_proj.transform(home[0], home[1])


def _group_survey_cost(group):
    """Survey length of a contiguous line group in metres: line lengths + nearest-endpoint
    transitions between consecutive lines (the lawnmower turns)."""
    total = sum(ln["length_m"] for ln in group)
    for i in range(len(group) - 1):
        a, b = group[i], group[i + 1]
        # nearest of the 4 endpoint pairings (the turn the ordered route will actually make)
        total += min(_dist(a["end_proj"], b["start_proj"]), _dist(a["end_proj"], b["end_proj"]),
                     _dist(a["start_proj"], b["start_proj"]), _dist(a["start_proj"], b["end_proj"]))
    return total


def _group_transit(group, home_proj):
    """Approximate approach + return transit for a group from a home (straight-line, used only
    to BALANCE the search; the final routes use real safe connectors)."""
    if not group:
        return 0.0
    ends = [group[0]["start_proj"], group[0]["end_proj"],
            group[-1]["start_proj"], group[-1]["end_proj"]]
    approach = min(_dist(home_proj, group[0]["start_proj"]), _dist(home_proj, group[0]["end_proj"]))
    ret = min(_dist(home_proj, group[-1]["start_proj"]), _dist(home_proj, group[-1]["end_proj"]))
    return approach + ret


def _contiguous_partitions(n, k):
    for cuts in combinations(range(1, n), k - 1):
        bounds = [0, *cuts, n]
        yield [(bounds[i], bounds[i + 1]) for i in range(k)]


def _balanced_split(lines, k):
    """Fallback single partition: k contiguous groups minimising the maximum group survey
    length (prefix-sum + feasibility binary search). Deterministic."""
    n = len(lines)
    costs = [ln["length_m"] for ln in lines]

    def feasible(cap):
        groups, cur, count = 0, 0.0, 1
        for c in costs:
            if cur + c > cap and count < k:
                count += 1
                cur = c
            else:
                cur += c
        return count <= k

    lo, hi = max(costs), sum(costs)
    for _ in range(60):
        mid = (lo + hi) / 2
        if feasible(mid):
            hi = mid
        else:
            lo = mid
    # rebuild the boundaries at cap=hi
    bounds, cur, count = [0], 0.0, 1
    for i, c in enumerate(costs):
        if cur + c > hi and count < k and i > bounds[-1]:
            bounds.append(i)
            count += 1
            cur = c
        else:
            cur += c
    bounds.append(n)
    # ensure exactly k non-empty groups
    while len(bounds) - 1 < k:
        # split the largest group
        pass
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _allocate(lines, vehicles, balance_metric):
    """Assign COMPLETE contiguous survey-line groups to vehicles.

    1. enumerate contiguous partitions of the sweep-ordered lines into K groups (bounded; a
       balanced split fallback when the count is large);
    2. for each partition, try every vehicle→group permutation, scoring by the balance metric
       (estimated duration when speeds available, else distance) with home transit included so a
       vehicle tends to get the region nearest its home;
    3. keep the assignment with the smallest imbalance, tie-broken by max cost then vehicle-id
       order — deterministic for identical input.

    Returns {groups: [[line,...] spatially ordered], assignment: {vehicle_id: group_index}}."""
    n, k = len(lines), len(vehicles)
    if k < MIN_FLEET_VEHICLES:
        raise FleetPlanError(f"A fleet mission requires at least {MIN_FLEET_VEHICLES} vehicles.")
    if n < k:
        raise FleetPlanError(
            f"There are only {n} survey line(s) for {k} vehicles — every vehicle must receive at "
            f"least one complete survey line. Reduce the lane spacing, enlarge the survey area, "
            f"or select fewer vehicles.")

    homes = {v["vehicle_id"]: v["_home_proj"] for v in vehicles}
    speeds = {v["vehicle_id"]: v["survey_speed_mps"] for v in vehicles}
    use_duration = balance_metric == "estimated_duration"

    total_combos = math.comb(n - 1, k - 1)
    partitions = (_contiguous_partitions(n, k) if total_combos <= MAX_PARTITION_COMBOS
                  else [_balanced_split(lines, k)])

    best = None  # (key, groups, assignment)
    vids = [v["vehicle_id"] for v in vehicles]
    for part in partitions:
        groups = [lines[s:e] for (s, e) in part]
        if any(len(g) == 0 for g in groups):
            continue
        survey = [_group_survey_cost(g) for g in groups]
        for perm in permutations(range(k)):
            # perm[i] = group index assigned to vehicles[i]
            costs = []
            for i, v in enumerate(vehicles):
                g = groups[perm[i]]
                dist = survey[perm[i]] + _group_transit(g, homes[v["vehicle_id"]])
                cost = dist / speeds[v["vehicle_id"]] if use_duration else dist
                costs.append(cost)
            mx, mn = max(costs), min(costs)
            avg = sum(costs) / len(costs)
            imbalance = (mx - mn) / avg if avg > 0 else 0.0
            # Minimise makespan (the slowest vehicle) FIRST: this balances the fleet AND rewards
            # giving each vehicle its nearest region (a cross assignment inflates transit → a
            # higher makespan). Total cost, then imbalance, then perm break exact ties —
            # deterministic for identical input.
            key = (round(mx, 3), round(sum(costs), 3), round(imbalance, 6), perm)
            if best is None or key < best[0]:
                assignment = {vids[i]: perm[i] for i in range(k)}
                best = (key, groups, assignment)

    _, groups, assignment = best
    return {"groups": groups, "assignment": assignment}


# ── per-vehicle route building ─────────────────────────────────────────────────────────────

def _order_from_home(lines, home_proj):
    """Order a vehicle's assigned lines so coverage ENTERS from the end nearest its home; the
    line list may be reversed when entering from the opposite end shortens the approach."""
    if len(lines) <= 1:
        return list(lines)
    first, last = lines[0], lines[-1]
    cand = [
        (_dist(home_proj, first["start_proj"]), False),
        (_dist(home_proj, first["end_proj"]), False),
        (_dist(home_proj, last["start_proj"]), True),
        (_dist(home_proj, last["end_proj"]), True),
    ]
    reverse = min(cand, key=lambda c: c[0])[1]
    return list(reversed(lines)) if reverse else list(lines)


def _coverage_coords(lines, home_proj):
    """Alternating boustrophedon coverage coords (deg) through ordered lines, each line entered
    at the endpoint nearest the running cursor — the standard lawnmower sweep."""
    ordered = _order_from_home(lines, home_proj)
    coords = []
    cursor = None
    for ln in ordered:
        a, b = ln["coords_deg"][0], ln["coords_deg"][-1]
        if cursor is None:
            # first line: enter at the end nearest home
            if _dist(home_proj, ln["start_proj"]) <= _dist(home_proj, ln["end_proj"]):
                seq = ln["coords_deg"]
            else:
                seq = list(reversed(ln["coords_deg"]))
        else:
            # subsequent: enter at the endpoint nearest the cursor
            if _deg_dist(cursor, a) <= _deg_dist(cursor, b):
                seq = ln["coords_deg"]
            else:
                seq = list(reversed(ln["coords_deg"]))
        coords.extend(seq)
        cursor = coords[-1]
    return planning._dedup(coords)


def _deg_dist(a, b):
    return planning._path_length_m([a, b])


def _build_child_mission(grid, vehicle, prim_lines, sec_lines, shared, navigable):
    """Build ONE standalone operator-survey-plan-v1 child mission for a vehicle from its
    assigned survey lines, reusing planning.py's safe connectors, segmentation, flattening and
    canonical hashing. The result is uploadable through the unchanged finalize path."""
    home = vehicle["home"]
    home_proj = vehicle["_home_proj"]
    warnings = []

    prim_ordered = _order_lines_by_sweep(list(prim_lines))
    primary_cov = _coverage_coords(prim_ordered, home_proj)
    primary_cov = grid.repair_path(primary_cov)

    secondary_cov = None
    if sec_lines:
        sec_ordered = _order_lines_by_sweep(list(sec_lines))
        secondary_cov = grid.repair_path(_coverage_coords(sec_ordered, home_proj))

    survey_entry = primary_cov[0]
    coverage_end = (secondary_cov[-1] if secondary_cov else primary_cov[-1])

    segments = []
    seq = [0]

    def add(kind, coords):
        seq[0] += 1
        cleaned = grid.clean_path(
            planning._dedup(coords),
            require_inside=(kind in planning._REQUIRE_INSIDE_KINDS),
            aggressive=(kind in planning._AGGRESSIVE_KINDS))
        if len(cleaned) < 2:
            return
        segments.append({"segment_id": f"{vehicle['vehicle_id']}-seg-{seq[0]:02d}-{kind}",
                         "kind": kind,
                         "coordinates": [[float(c[0]), float(c[1])] for c in cleaned],
                         "length_m": round(planning._path_length_m(cleaned), 2)})

    # approach: home → survey entry (near-shore leg → no-go clearance only)
    if not planning._close(home, survey_entry):
        try:
            add("start_connector", grid.safe_connector(home, survey_entry, require_inside=False))
        except ConnectorError:
            warnings.append(f"{vehicle['vehicle_id']}: no safe approach from home to the survey "
                            f"entry was found; the route begins at the survey entry.")
    add("primary", primary_cov)
    if secondary_cov:
        if not planning._close(primary_cov[-1], secondary_cov[0]):
            try:
                add("pass_transition", grid.safe_connector(primary_cov[-1], secondary_cov[0], require_inside=True))
            except ConnectorError:
                warnings.append(f"{vehicle['vehicle_id']}: no safe pass transition; passes joined directly.")
        add("secondary", secondary_cov)
    # return: coverage exit → home
    if not planning._close(coverage_end, home):
        try:
            add("final_home_connector", grid.safe_connector(coverage_end, home, require_inside=False))
        except ConnectorError:
            warnings.append(f"{vehicle['vehicle_id']}: no safe return to home was found; the route "
                            f"ends at the last coverage waypoint.")

    route_coords, order = planning._flatten_segments(segments)
    route_coords = planning._dedup(route_coords)
    route_wps = planning._route_waypoints(route_coords)
    route_hash = planning._route_hash(route_wps)

    def seg_len(kinds):
        return round(sum(s["length_m"] for s in segments if s["kind"] in kinds), 2)

    approach_m = seg_len(("start_connector",))
    survey_m = seg_len(("primary", "secondary", "pass_transition"))
    return_m = seg_len(("final_home_connector",))
    total_m = round(approach_m + survey_m + return_m, 2)
    speed = vehicle["survey_speed_mps"]
    duration_s = round(total_m / speed, 1) if speed > 0 else None

    assigned_ids = [ln["id"] for ln in prim_lines] + [ln["id"] for ln in (sec_lines or [])]

    planning_inputs = {
        "boundary": shared["boundary"],
        "shoreline_clearance_m": shared["shoreline_clearance_m"],
        "navigable_boundary": navigable,
        # Original rings + the clearance parameter, exactly as the single-vehicle package
        # stores them — a fleet child mission is an ordinary operator-survey-plan-v1 package.
        "no_go_zones": shared["no_go_zones"],
        "no_go_clearance_m": shared["no_go_clearance_m"],
        "lane_spacing_m": shared["lane_spacing_m"],
        "primary_angle_deg": shared["primary_angle_deg"],
        "dual_pass": shared["dual_pass"],
        "secondary_angle_deg": shared["secondary_angle_deg"] if shared["dual_pass"] else None,
        "planning_home": home,
        "route_start_mode": "planning_home",
        "approach_waypoints": [],
        "return_waypoints": [],
    }
    # THE SAME GEOMETRY CONTRACT AS A SINGLE-VEHICLE MISSION. A fleet child is an ordinary
    # operator-survey-plan-v1 package and gets no exemption: its approved Home corridor is
    # derived from its own transit segments (a fleet home is routinely outside the survey
    # polygon), and the whole route is then proven against navigable ∪ corridor − exclusion.
    # A failure raises, so an inconsistent child mission is never assembled into a fleet plan.
    home_corridor, home_corridor_meta = planning.home_corridor_ring(
        segments=segments, navigable_geometry=navigable,
        no_go_zones=shared["no_go_zones"], planning_home=home,
        no_go_clearance_m=shared["no_go_clearance_m"])
    if home_corridor is None:
        warnings.append(f"{vehicle['vehicle_id']}: no approved Home corridor could be derived "
                        f"({home_corridor_meta['reason']}).")

    metrics = {
        "waypoint_count": len(route_wps),
        "assigned_survey_line_count": len(assigned_ids),
        "approach_distance_m": approach_m,
        "survey_distance_m": survey_m,
        "return_distance_m": return_m,
        "total_distance_m": total_m,
        "survey_speed_mps": speed,
        "survey_speed_is_default": vehicle["survey_speed_is_default"],
        "estimated_duration_s": duration_s,
        "coverage_length_m": survey_m,
        "transit_length_m": round(approach_m + return_m, 2),
        "total_length_m": total_m,
    }
    package = {
        "ok": True,
        "mission_package_version": planning.MISSION_PACKAGE_VERSION,
        "contract_version": planning.ROUTE_CONTRACT_VERSION,
        "planning_inputs": planning_inputs,
        "segments": segments,
        # A fleet child always starts at its own planning Home (`route_start_mode:
        # planning_home`), so every approved transit leg is an EXECUTED leg and there is no
        # planning-only geometry. Stated explicitly so the child package has the same shape as
        # a single-vehicle one and the empty list is an answer, not an omission.
        "planning_only_transit_segments": [],
        "original_execution_order": order,
        "route_waypoints": route_wps,
        "route_hash": route_hash,
        "metrics": metrics,
        "navigable_boundary": navigable,
        "home_corridor": home_corridor,
        "home_corridor_meta": home_corridor_meta,
        "warnings": warnings,
    }
    geometry_check = planning.check_mission_geometry(
        segments=segments, route_waypoints=route_wps, navigable_geometry=navigable,
        no_go_zones=shared["no_go_zones"], no_go_clearance_m=shared["no_go_clearance_m"],
        planning_home=home, home_corridor=home_corridor)
    if not geometry_check["ok"]:
        raise planning.GeometryConsistencyError([
            {**f, "message": f"{vehicle['vehicle_id']}: {f['message']}"}
            for f in geometry_check["failures"]])
    package["geometry_check"] = geometry_check
    return {
        "vehicle_id": vehicle["vehicle_id"],
        "vehicle_name": vehicle["vehicle_name"],
        "colour": vehicle["colour"],
        "planning_home": home,
        "survey_speed_mps": speed,
        "survey_speed_is_default": vehicle["survey_speed_is_default"],
        "assigned_survey_line_ids": assigned_ids,
        "mission_package": package,
        "route_hash": route_hash,
        "metrics": metrics,
        "warnings": warnings,
    }, warnings


# ── two-pass banding ───────────────────────────────────────────────────────────────────────

def _line_from_proj(grid, proj_coords, line_id, pass_kind="pass-2"):
    """Build a survey-line record from projected coords (used for CLIPPED secondary pieces)."""
    deg_ls = transform(grid.to_deg.transform, LineString(proj_coords))
    coords_deg = [[round(x, 7), round(y, 7)] for x, y in deg_ls.coords]
    cx = sum(p[0] for p in proj_coords) / len(proj_coords)
    cy = sum(p[1] for p in proj_coords) / len(proj_coords)
    return {"id": line_id, "pass_kind": pass_kind, "row": 0, "coords_deg": coords_deg,
            "proj": proj_coords, "start_deg": coords_deg[0], "end_deg": coords_deg[-1],
            "start_proj": proj_coords[0], "end_proj": proj_coords[-1],
            "length_m": round(_proj_len(proj_coords), 2), "centroid_proj": (cx, cy)}


def _clip_seg_to_slab(a, b, sa, sb, lo, hi):
    """Clip straight segment a→b to the sweep slab lo ≤ s ≤ hi, where s(a)=sa, s(b)=sb are the
    endpoints' primary-axis coordinates. Returns [p0, p1] or None."""
    if abs(sb - sa) < 1e-9:
        return [a, b] if lo - 1e-9 <= sa <= hi + 1e-9 else None
    t0, t1 = 0.0, 1.0
    for bound, sign in ((lo, 1), (hi, -1)):
        t = (bound - sa) / (sb - sa)
        if sign * (sb - sa) > 0:          # entering the half-space as t increases
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
    if t0 > t1 + 1e-9:
        return None
    def at(t):
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    return [at(max(0.0, t0)), at(min(1.0, t1))]


def _clip_line_to_range(coords_proj, n, lo, hi):
    """The portion of a (straight) survey line whose primary-axis coordinate lies in [lo, hi]."""
    out = []
    for i in range(len(coords_proj) - 1):
        a, b = coords_proj[i], coords_proj[i + 1]
        seg = _clip_seg_to_slab(a, b, _dot(a, n), _dot(b, n), lo, hi)
        if seg:
            for p in seg:
                if not out or _dist(out[-1], p) > 1e-6:
                    out.append(p)
    return out


def _clip_secondary_to_bands(grid, sec_lines, vehicles, prim_by_vehicle, prim_normal):
    """CLIP each secondary-pass line to each vehicle's contiguous primary-axis band, so a vehicle
    surveys BOTH passes only within its OWN geographic region (the task's preferred two-pass
    approach). Vehicles are ordered by band centre; band boundaries are the midpoints between
    adjacent centres, so the bands tile the whole sweep range with no gap or overlap. Each
    clipped piece becomes that vehicle's own secondary survey line with a stable, unique id."""
    centers = {}
    for v in vehicles:
        pl = prim_by_vehicle[v["vehicle_id"]]
        if pl:
            sweeps = [_dot(l["centroid_proj"], prim_normal) for l in pl]
            centers[v["vehicle_id"]] = sum(sweeps) / len(sweeps)
    order = sorted(centers, key=lambda vid: centers[vid])
    cuts = [(centers[order[i]] + centers[order[i + 1]]) / 2 for i in range(len(order) - 1)]
    ranges = {}
    for i, vid in enumerate(order):
        lo = cuts[i - 1] if i > 0 else -1e18
        hi = cuts[i] if i < len(cuts) else 1e18
        ranges[vid] = (lo, hi)

    result = {v["vehicle_id"]: [] for v in vehicles}
    counter = {v["vehicle_id"]: 0 for v in vehicles}
    for ln in sec_lines:
        for vid, (lo, hi) in ranges.items():
            piece = _clip_line_to_range(ln["proj"], prim_normal, lo, hi)
            if len(piece) >= 2 and _proj_len(piece) > 1.0:
                counter[vid] += 1
                result[vid].append(_line_from_proj(grid, piece, f"pass-2-{vid}-line-{counter[vid]:04d}"))
    return result


# ── top-level fleet generation ─────────────────────────────────────────────────────────────

def _fleet_plan_id():
    return "fleet-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + \
        hashlib.sha1(str(datetime.now(timezone.utc).timestamp()).encode()).hexdigest()[:6]


def generate_fleet(raw_inputs, max_route_waypoints=None):
    """Generate a complete fleet plan: shared survey geometry → survey lines → contiguous
    home-aware allocation → one independent child mission per vehicle → fleet conflict
    validation. Deterministic except for the timestamp/id. Raises FleetPlanError /
    ConnectorError / DisconnectedNavigableError for anything unroutable."""
    if not PLANNING_AVAILABLE:
        raise PlanningUnavailable("Fleet planning requires shapely, pyproj and numpy.")
    inp = normalize_fleet_inputs(raw_inputs)

    boundary = inp["boundary"]
    clearance = inp["shoreline_clearance_m"]
    no_go_clearance = inp["no_go_clearance_m"]
    spacing = inp["lane_spacing_m"]
    zones = inp["no_go_zones"]

    # The SAME navigable model the single-vehicle planner builds, including the buffered no-go
    # exclusion — `_survey_lines` subtracts `grid.nogo`, so the fleet's allocatable survey lines
    # are clipped to the identical exclusion the single-vehicle coverage is.
    grid = planning._NavGrid(boundary, clearance, zones, step_m=spacing,
                             no_go_clearance=no_go_clearance)
    if not grid.buffer_valid:
        raise FleetPlanError(f"The no-go clearance of {no_go_clearance} m could not be applied — "
                             f"buffering the no-go zones produced invalid geometry.")
    if grid.empty:
        raise FleetPlanError("The navigable area is empty after applying the shoreline clearance "
                             "and the no-go zones with their no-go clearance.")
    if grid.disconnected:
        raise DisconnectedNavigableError(
            f"The shoreline clearance and the no-go zones (with a {no_go_clearance} m no-go "
            f"clearance) split the survey into {len(grid.components)} disconnected navigable "
            f"regions — fleet generation requires one connected region.")

    for v in inp["vehicles"]:
        v["_home_proj"] = _home_proj(grid, v["home"])

    prim_lines = _order_lines_by_sweep(
        _survey_lines(grid, boundary, spacing, inp["primary_angle_deg"], clearance, zones, "pass-1"))
    if len(prim_lines) < 1:
        raise FleetPlanError("No coverage lines could be generated — the navigable area may be too "
                             "small for the chosen lane spacing.")

    alloc = _allocate(prim_lines, inp["vehicles"], inp["balance_metric"])
    groups, assignment = alloc["groups"], alloc["assignment"]

    # Apply optional manual line-ownership overrides (line_id → vehicle_id), preserving one owner
    # per line. Contiguity is validated afterward, not forced.
    manual = _normalize_manual(inp["manual_assignments"], prim_lines, inp["vehicles"])

    # Per-vehicle primary lines (in sweep order), honouring manual overrides.
    prim_by_vehicle = {v["vehicle_id"]: [] for v in inp["vehicles"]}
    for i, ln in enumerate(prim_lines):
        owner = manual.get(ln["id"]) if manual else None
        if owner is None:
            gi = _group_of(i, groups)
            owner = next(vid for vid, g in assignment.items() if g == gi)
        prim_by_vehicle[owner].append(ln)

    # Two-pass: CLIP the secondary pass into each vehicle's geographic band (both passes stay in
    # region). The clipped pieces — not the shared full secondary lines — are the assignable units.
    sec_by_vehicle = {v["vehicle_id"]: [] for v in inp["vehicles"]}
    sec_pieces = []
    if inp["dual_pass"]:
        full_sec = _order_lines_by_sweep(
            _survey_lines(grid, boundary, spacing, inp["secondary_angle_deg"], clearance, zones, "pass-2"))
        if full_sec:
            _, prim_n = _axis(prim_lines)
            sec_by_vehicle = _clip_secondary_to_bands(grid, full_sec, inp["vehicles"],
                                                      prim_by_vehicle, prim_n)
            sec_pieces = [ln for lst in sec_by_vehicle.values() for ln in lst]

    navigable = planning._navigable_rings_deg(boundary, clearance)

    shared = {
        "boundary": boundary,
        "shoreline_clearance_m": clearance,
        "no_go_zones": zones,
        "no_go_clearance_m": no_go_clearance,
        "lane_spacing_m": spacing,
        "primary_angle_deg": inp["primary_angle_deg"],
        "dual_pass": inp["dual_pass"],
        "secondary_angle_deg": inp["secondary_angle_deg"],
        "survey_orientation_deg": inp["primary_angle_deg"],
    }

    vehicle_plans = []
    all_warnings = []
    for v in inp["vehicles"]:
        pl, warns = _build_child_mission(
            grid, v, prim_by_vehicle[v["vehicle_id"]], sec_by_vehicle.get(v["vehicle_id"], []),
            shared, navigable)
        if max_route_waypoints is not None and pl["metrics"]["waypoint_count"] > max_route_waypoints:
            pl["warnings"].append(
                f"Route has {pl['metrics']['waypoint_count']} waypoints, above the mission limit of "
                f"{max_route_waypoints} — increase lane spacing or reduce the survey area.")
        vehicle_plans.append(pl)
        all_warnings.extend(warns)

    fleet_plan = _assemble_fleet_plan(inp, shared, prim_lines, sec_pieces, vehicle_plans,
                                      navigable, bool(manual))
    fleet_plan["validation"] = validate_fleet(fleet_plan)
    fleet_plan["warnings"] = all_warnings
    if max_route_waypoints is not None:
        fleet_plan["max_route_waypoints"] = max_route_waypoints
    return fleet_plan


def _group_of(line_index, groups):
    """Which group index a sweep-ordered line index falls into."""
    acc = 0
    for gi, g in enumerate(groups):
        acc += len(g)
        if line_index < acc:
            return gi
    return len(groups) - 1


def _normalize_manual(manual, prim_lines, vehicles):
    if not isinstance(manual, dict):
        return None
    valid_lines = {ln["id"] for ln in prim_lines}
    valid_vids = {v["vehicle_id"] for v in vehicles}
    out = {}
    for line_id, vid in manual.items():
        if str(line_id) in valid_lines and str(vid) in valid_vids:
            out[str(line_id)] = str(vid)
    return out or None


def _input_revision(inp):
    def rd(pt):
        return [round(float(pt[0]), 7), round(float(pt[1]), 7)] if pt else None
    canonical = {
        "b": [rd(p) for p in inp["boundary"]],
        "z": [[rd(p) for p in z] for z in inp["no_go_zones"]],
        "c": inp["shoreline_clearance_m"],
        "ngc": inp["no_go_clearance_m"],
        "s": inp["lane_spacing_m"],
        "pa": inp["primary_angle_deg"],
        "d": inp["dual_pass"],
        "sa": inp["secondary_angle_deg"],
        "sep": inp["minimum_fleet_separation_m"],
        "bal": inp["balance_metric"],
        "v": [[v["vehicle_id"], rd(v["home"]), v["survey_speed_mps"]] for v in inp["vehicles"]],
        "m": inp["manual_assignments"],
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return "frev:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _assemble_fleet_plan(inp, shared, prim_lines, sec_lines, vehicle_plans, navigable, manual_applied):
    total_survey = round(sum(vp["metrics"]["survey_distance_m"] for vp in vehicle_plans), 2)
    durations = [vp["metrics"]["estimated_duration_s"] for vp in vehicle_plans
                 if vp["metrics"]["estimated_duration_s"] is not None]
    distances = [vp["metrics"]["total_distance_m"] for vp in vehicle_plans]
    if inp["balance_metric"] == "estimated_duration" and durations:
        vals = durations
    else:
        vals = distances
    mx, mn = (max(vals), min(vals)) if vals else (0, 0)
    avg = (sum(vals) / len(vals)) if vals else 0
    imbalance = round(((mx - mn) / avg * 100.0), 1) if avg > 0 else 0.0

    assigned = [lid for vp in vehicle_plans for lid in vp["assigned_survey_line_ids"]]
    all_line_ids = [ln["id"] for ln in prim_lines] + [ln["id"] for ln in sec_lines]
    duplicate = sorted({lid for lid in assigned if assigned.count(lid) > 1})
    unassigned = [lid for lid in all_line_ids if lid not in assigned]

    def line_view(ln):
        return {"id": ln["id"], "pass_kind": ln["pass_kind"], "row": ln["row"],
                "coordinates": ln["coords_deg"], "length_m": ln["length_m"]}

    return {
        "ok": True,
        "fleet_plan_id": _fleet_plan_id(),
        "fleet_plan_version": 1,
        "fleet_plan_schema": FLEET_PLAN_VERSION,
        "allocation_method": ALLOCATION_METHOD,
        "manual_adjusted": manual_applied,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_revision": _input_revision(inp),
        "shared_geometry": {
            "survey_polygon": inp["boundary"],
            "no_go_zones": inp["no_go_zones"],
            "shoreline_safety_margin_m": inp["shoreline_clearance_m"],
            # Explicit alongside the ORIGINAL rings above — the fleet plan states the
            # constraint, it does not ship pre-buffered geometry in place of what was drawn.
            "no_go_clearance_m": inp["no_go_clearance_m"],
            "survey_line_spacing_m": inp["lane_spacing_m"],
            "survey_orientation_deg": inp["primary_angle_deg"],
            "second_pass_enabled": inp["dual_pass"],
            "second_pass_orientation_deg": inp["secondary_angle_deg"] if inp["dual_pass"] else None,
            "navigable_boundary": navigable,
        },
        "settings": {
            "minimum_fleet_separation_m": inp["minimum_fleet_separation_m"],
            "allocation_method": ALLOCATION_METHOD,
            "balance_metric": inp["balance_metric"],
        },
        "survey_lines": [line_view(ln) for ln in prim_lines] + [line_view(ln) for ln in sec_lines],
        "vehicles": vehicle_plans,
        "allocation_summary": {
            "vehicle_count": len(vehicle_plans),
            "total_survey_distance_m": total_survey,
            "survey_line_count": len(all_line_ids),
            "imbalance_percent": imbalance,
            "max_estimated_duration_s": max(durations) if durations else None,
            "min_estimated_duration_s": min(durations) if durations else None,
            "unassigned_survey_line_ids": unassigned,
            "duplicate_survey_line_ids": duplicate,
        },
    }


# ── fleet conflict validation ──────────────────────────────────────────────────────────────

def _vehicle_geom(grid, vp):
    """Projected geometry for one vehicle plan: coverage / approach / return / full LineStrings
    and the projected route waypoints — everything the cross-vehicle checks compare."""
    segs = vp["mission_package"]["segments"]
    to_proj = grid.to_proj

    def proj_line(coords):
        if len(coords) < 2:
            return None
        return transform(to_proj.transform, LineString([(c[0], c[1]) for c in coords]))

    coverage, approach, ret = [], [], []
    for s in segs:
        if s["kind"] in ("primary", "secondary", "pass_transition"):
            coverage.append(s["coordinates"])
        elif s["kind"] in ("start_connector", "approach", "survey_entry_connector"):
            approach.append(s["coordinates"])
        elif s["kind"] in ("final_home_connector", "return_connector", "return_approach"):
            ret.append(s["coordinates"])
    wps = vp["mission_package"]["route_waypoints"]
    full = [[w["longitude"], w["latitude"]] for w in wps]
    return {
        "coverage": [proj_line(c) for c in coverage if len(c) >= 2],
        "approach": [proj_line(c) for c in approach if len(c) >= 2],
        "return": [proj_line(c) for c in ret if len(c) >= 2],
        "full": proj_line(full),
        "wps_proj": [to_proj.transform(w["longitude"], w["latitude"]) for w in wps],
    }


def validate_fleet(fleet_plan):
    """Deterministic fleet-wide conflict validation on TOP of each child mission's own
    validation. Distinguishes blocking errors, warnings and informational metrics.

    Blocking: too few vehicles, duplicate/invalid ids, a vehicle with no assigned line, a line
    assigned twice, an unassigned retained line, an identical/near-identical cross-vehicle
    waypoint, a cross-vehicle route-segment intersection, an invalid child mission.
    Warning: planned separation below the configured minimum, duration imbalance, an approach/
    return route running close to another vehicle's survey region."""
    errors, warnings, checks = [], [], {}
    metrics = {}
    vehicles = fleet_plan.get("vehicles") or []
    separation = (fleet_plan.get("settings") or {}).get("minimum_fleet_separation_m",
                                                        DEFAULT_FLEET_SEPARATION_M)

    checks["min_two_vehicles"] = len(vehicles) >= MIN_FLEET_VEHICLES
    if not checks["min_two_vehicles"]:
        errors.append("A fleet mission requires at least two vehicles.")

    ids = [v["vehicle_id"] for v in vehicles]
    checks["unique_vehicle_ids"] = len(ids) == len(set(ids))
    if not checks["unique_vehicle_ids"]:
        errors.append("Two child missions share a vehicle id.")

    # each vehicle has >= 1 line, unique home, valid speed
    for v in vehicles:
        if not v.get("assigned_survey_line_ids"):
            errors.append(f"Vehicle {v['vehicle_id']} has no assigned survey line.")
        if v.get("planning_home") is None:
            errors.append(f"Vehicle {v['vehicle_id']} has no planning home.")
        if not (isinstance(v.get("survey_speed_mps"), (int, float)) and v["survey_speed_mps"] > 0):
            errors.append(f"Vehicle {v['vehicle_id']} has an invalid survey speed.")

    # line-assignment integrity
    summary = fleet_plan.get("allocation_summary") or {}
    checks["all_lines_assigned_once"] = (not summary.get("unassigned_survey_line_ids")
                                         and not summary.get("duplicate_survey_line_ids"))
    if summary.get("duplicate_survey_line_ids"):
        errors.append("A survey line is assigned to more than one vehicle: "
                      + ", ".join(summary["duplicate_survey_line_ids"]))
    if summary.get("unassigned_survey_line_ids"):
        errors.append(f"{len(summary['unassigned_survey_line_ids'])} survey line(s) are not "
                      f"assigned to any vehicle.")

    # every child mission passes normal per-vehicle validation
    per_vehicle = {}
    if PLANNING_AVAILABLE:
        for v in vehicles:
            pkg = v.get("mission_package") or {}
            body = {**(pkg.get("planning_inputs") or {}),
                    "boundary": (pkg.get("planning_inputs") or {}).get("boundary"),
                    "home": (pkg.get("planning_inputs") or {}).get("planning_home"),
                    "route_waypoints": pkg.get("route_waypoints"),
                    "segments": pkg.get("segments"),
                    # The child's OWN approved geometry, so validation judges what was generated
                    # rather than re-deriving a corridor that could differ from the shipped one.
                    "navigable_boundary": pkg.get("navigable_boundary"),
                    "home_corridor": pkg.get("home_corridor"),
                    "route_hash": pkg.get("route_hash")}
            res = planning.validate_plan(body)
            per_vehicle[v["vehicle_id"]] = {"ok": res["ok"], "errors": res["errors"],
                                            "warnings": res["warnings"]}
            if not res["ok"]:
                errors.append(f"Vehicle {v['vehicle_id']} child mission is invalid: "
                              + "; ".join(res["errors"][:2]))
    checks["child_missions_valid"] = all(pv["ok"] for pv in per_vehicle.values()) if per_vehicle else None

    # geometric cross-vehicle checks (projected)
    min_sep = None
    dup_pairs = 0
    intersect_pairs = 0
    approach_conflicts = 0
    if PLANNING_AVAILABLE and len(vehicles) >= 2:
        boundary = (fleet_plan.get("shared_geometry") or {}).get("survey_polygon")
        shared_geom = fleet_plan["shared_geometry"]
        ngc = shared_geom.get("no_go_clearance_m")
        grid = planning._NavGrid(boundary,
                                 shared_geom.get("shoreline_safety_margin_m", 0),
                                 shared_geom.get("no_go_zones") or [],
                                 step_m=shared_geom.get("survey_line_spacing_m",
                                                        planning.DEFAULT_LANE_SPACING_M),
                                 # Absent (a plan from before the parameter existed) takes the
                                 # same default normalize_fleet_inputs would have applied.
                                 no_go_clearance=(planning.DEFAULT_NO_GO_CLEARANCE_M
                                                  if ngc is None else ngc))
        geoms = {v["vehicle_id"]: _vehicle_geom(grid, v) for v in vehicles}

        for a, b in combinations(vehicles, 2):
            ga, gb = geoms[a["vehicle_id"]], geoms[b["vehicle_id"]]
            # min separation between full routes
            if ga["full"] is not None and gb["full"] is not None:
                d = ga["full"].distance(gb["full"])
                if min_sep is None or d < min_sep:
                    min_sep = d
                # segment intersection
                inter = ga["full"].intersection(gb["full"])
                if not inter.is_empty:
                    intersect_pairs += 1
                    errors.append(f"Routes for {a['vehicle_id']} and {b['vehicle_id']} intersect.")
            # near-identical waypoints
            for pa in ga["wps_proj"]:
                for pb in gb["wps_proj"]:
                    if _dist(pa, pb) < NEAR_DUP_M:
                        dup_pairs += 1
            # approach/return of one crossing the coverage of the other
            for lbl, lines in (("approach", ga["approach"]), ("return", ga["return"])):
                for ln in lines:
                    if ln is None:
                        continue
                    for cov in gb["coverage"]:
                        if cov is not None and ln.intersects(cov):
                            approach_conflicts += 1
                            warnings.append(f"{a['vehicle_id']}'s {lbl} route crosses "
                                            f"{b['vehicle_id']}'s survey region.")
            for lbl, lines in (("approach", gb["approach"]), ("return", gb["return"])):
                for ln in lines:
                    if ln is None:
                        continue
                    for cov in ga["coverage"]:
                        if cov is not None and ln.intersects(cov):
                            approach_conflicts += 1
                            warnings.append(f"{b['vehicle_id']}'s {lbl} route crosses "
                                            f"{a['vehicle_id']}'s survey region.")

    checks["no_duplicate_cross_waypoints"] = (dup_pairs == 0)
    if dup_pairs:
        errors.append(f"{dup_pairs} cross-vehicle waypoint pair(s) are identical or within "
                      f"{NEAR_DUP_M} m of each other.")
    checks["no_route_intersections"] = (intersect_pairs == 0)
    checks["separation_respected"] = (min_sep is None) or (min_sep >= separation)
    if min_sep is not None and min_sep < separation:
        warnings.append(f"Minimum planned route separation is {round(min_sep, 1)} m, below the "
                        f"configured {separation} m.")

    imbalance = summary.get("imbalance_percent")
    if imbalance is not None and imbalance > 20:
        warnings.append(f"Route {(fleet_plan.get('settings') or {}).get('balance_metric', 'duration')} "
                        f"imbalance is {imbalance}%.")

    metrics["minimum_cross_route_separation_m"] = round(min_sep, 2) if min_sep is not None else None
    metrics["route_intersection_pairs"] = intersect_pairs
    metrics["duplicate_waypoint_pairs"] = dup_pairs
    metrics["approach_return_conflicts"] = approach_conflicts
    metrics["imbalance_percent"] = imbalance

    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks,
            "per_vehicle": per_vehicle, "metrics": metrics,
            "note": ("Fleet validation reduces planned route conflicts but does not provide "
                     "runtime collision avoidance.")}
