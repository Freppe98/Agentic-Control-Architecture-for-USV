"""Construct Scout's replanning planning-package from an Operator immutable mission record.

WHAT A PLANNING PACKAGE IS, AND WHY THE OPERATOR BUILDS IT FROM THE STORED RECORD
--------------------------------------------------------------------------------
When Scout must replan a safe return, it does NOT re-derive the survey from scratch — it
needs the operator's approved geometry: the exact route, its semantic segments, the
navigable boundary, the no-go zones and Home. The Operator Station already retains all of
this in the immutable revision-0 mission record it stored at finalize time (see main.py
`_new_mission_record`). This module projects that record onto the package shape Scout's
`PUT /agent/replan/planning_package` accepts.

THE ROUTE IS THE PIXHAWK ROUTE, BYTE-FOR-BYTE (hash invariant)
-------------------------------------------------------------
The package route MUST hash to the same `route_content_hash` as the mission uploaded to the
Pixhawk, or Scout fails closed with PLANNING_PACKAGE_HASH_MISMATCH. So the route items carry
exactly the canonical fields (`latitude`, `longitude`, `loiter_time_s`) taken verbatim from
the record's `route_waypoints` — the same list `mission_contract.route_content_hash` digests.
The per-waypoint `segment` label is RICH METADATA alongside those fields; it is not part of
the canonical hash input on either side, so labelling never moves the hash. We recompute the
hash here from the record's route and refuse to build a package whose route disagrees with
the record's stored hash — a package can never carry a route the operator did not approve.

SEMANTIC LABELS COME FROM THE ACTUAL GENERATION STAGES, NOT A GUESS
-------------------------------------------------------------------
Each route waypoint in the record's `original_execution_order` is stamped with the
`source_segment_kind` of the generator stage that produced it (start_connector, approach,
primary, …) — and that array is 1:1 with the final, cleaned route by construction (see
planning.py `_flatten_segments`). We map those nine operator kinds onto Scout's four coarser
route-segment labels. The mapping is explicit and documented; the one judgment it encodes is
`pass_transition` (the connector from the primary pass to the secondary pass) → SECONDARY_
SURVEY, because it is the lead-in that keeps the secondary coverage block contiguous and it
is neither outbound nor a return leg. Scout's vocabulary is coarser than the operator's, so
this coarsening is a documented, reported limitation — never a silent reinterpretation.
"""
from __future__ import annotations

import mission_contract

SOURCE = "OPERATOR_STATION"

# Scout's supported route-segment labels (the only values Scout validates against).
OUTBOUND_TRANSIT = "OUTBOUND_TRANSIT"
PRIMARY_SURVEY = "PRIMARY_SURVEY"
SECONDARY_SURVEY = "SECONDARY_SURVEY"
RETURN = "RETURN"
SCOUT_SEGMENTS = (OUTBOUND_TRANSIT, PRIMARY_SURVEY, SECONDARY_SURVEY, RETURN)

# Operator generator kind (planning.SEGMENT_KINDS) → Scout label. Every one of the nine
# generator kinds is mapped explicitly; an unmapped/unknown kind is a hard error rather than
# a fabricated default, so a future generator stage cannot silently mis-label the route.
SEGMENT_KIND_TO_LABEL = {
    "start_connector": OUTBOUND_TRANSIT,
    "approach": OUTBOUND_TRANSIT,
    "survey_entry_connector": OUTBOUND_TRANSIT,
    "primary": PRIMARY_SURVEY,
    "pass_transition": SECONDARY_SURVEY,   # documented coarsening (see module docstring)
    "secondary": SECONDARY_SURVEY,
    "return_connector": RETURN,
    "return_approach": RETURN,
    "final_home_connector": RETURN,
}


class PackageError(ValueError):
    """The record cannot produce a valid package (empty route, hash disagreement, an
    unmappable segment kind, or an invalid Home). Raised rather than emitting a package Scout
    would reject — the operator sees the specific reason before anything reaches Scout."""


def label_for_kind(kind):
    """Scout route-segment label for an operator generator kind. Raises for an unknown kind."""
    try:
        return SEGMENT_KIND_TO_LABEL[kind]
    except KeyError as exc:
        raise PackageError(f"route waypoint carries an unmappable segment kind {kind!r}") from exc


def _valid_coord(lat, lng):
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None
    if lat == 0.0 and lng == 0.0:      # 0,0 is the null-island sentinel, never a real fix
        return None
    return (lat, lng)


def normalize_home(home):
    """A `{latitude, longitude}` Home from any of the shapes the record / live home-block use
    ({lat,lng}, {latitude,longitude}, {home_position:{...}}), or None when no valid fix exists.
    Never fabricates 0,0 — an absent Home stays absent so readiness reports it honestly."""
    if not isinstance(home, dict):
        return None
    src = home.get("home_position") if isinstance(home.get("home_position"), dict) else home
    coord = _valid_coord(
        src.get("latitude", src.get("lat")),
        src.get("longitude", src.get("lng", src.get("lon"))),
    )
    if coord is None:
        return None
    return {"latitude": coord[0], "longitude": coord[1]}


def route_with_segments(record):
    """The Scout package route: the record's canonical route waypoints, each carrying its
    Scout segment label. 1:1 with `route_waypoints`, in route order.

    The `latitude`/`longitude`/`loiter_time_s` are taken verbatim from `route_waypoints`
    (the hashed fields); the label comes from the position-aligned `original_execution_order`.
    """
    waypoints = record.get("route_waypoints") or []
    if not isinstance(waypoints, list) or not waypoints:
        raise PackageError("mission record has no route_waypoints — nothing to package")
    order = record.get("original_execution_order") or []
    kinds = [o.get("source_segment_kind") for o in order if isinstance(o, dict)]
    route = []
    for i, wp in enumerate(waypoints):
        if not isinstance(wp, dict):
            raise PackageError(f"route waypoint {i} is not an object")
        coord = _valid_coord(wp.get("latitude"), wp.get("longitude"))
        if coord is None:
            raise PackageError(f"route waypoint {i} has an invalid coordinate")
        # The order array is 1:1 with the route by construction; if it is absent (a record
        # built before segmentation) we cannot invent a label, so fail closed rather than
        # defaulting every leg to one bucket.
        if i >= len(kinds) or kinds[i] is None:
            raise PackageError("mission record lacks per-waypoint segment provenance "
                               "(original_execution_order) — cannot label the route")
        route.append({
            "latitude": coord[0],
            "longitude": coord[1],
            "loiter_time_s": round(float(wp.get("loiter_time_s", 0) or 0), 3),
            "segment": label_for_kind(kinds[i]),
        })
    return route


def _boundary_points(rings):
    """The exterior navigable ring as `[{latitude, longitude}, ...]`, or [] when absent.
    planning stores navigable_boundary as a list of rings of [lng, lat]; Scout validates route
    segments against ONE boundary polygon, so we hand it the first (exterior) ring in the
    package's coordinate convention. The full ring set is preserved under planner_metadata."""
    if not isinstance(rings, list) or not rings:
        return []
    first = rings[0]
    if not isinstance(first, list):
        return []
    pts = []
    for p in first:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            coord = _valid_coord(p[1], p[0])   # ring is [lng, lat]
            if coord:
                pts.append({"latitude": coord[0], "longitude": coord[1]})
    return pts


def segment_label_counts(route):
    """{label: count} over a package route — the summary readiness/UX shows without re-deriving."""
    counts = {}
    for item in route:
        counts[item["segment"]] = counts.get(item["segment"], 0) + 1
    return counts


def build_package(record, home, *, usv_id, revision=None, source=SOURCE):
    """Build the Scout planning-package payload from an immutable mission record + a Home.

    Returns (package, meta). `package` is the exact body for PUT /agent/replan/planning_package.
    `meta` carries the operator-computed `route_content_hash`, the segment-label counts and the
    limitations Scout will also report (no survey graph, boundary-shape assumption), so the
    caller can present readiness without re-deriving anything. Raises PackageError when the
    record cannot yield a valid, hash-consistent package.
    """
    mission_id = record.get("mission_id")
    if not mission_id:
        raise PackageError("mission record has no mission_id")

    route = route_with_segments(record)

    # The authoritative route hash, recomputed from the record's route (Home excluded) — the
    # SAME digest the Pixhawk upload was verified against. If the record already stored one,
    # they MUST agree, or the record was altered after finalize and no package may be built.
    computed_hash = mission_contract.route_content_hash(record.get("route_waypoints") or [])
    stored_hash = record.get("route_hash")
    if stored_hash and stored_hash != computed_hash:
        raise PackageError("mission record route_hash disagrees with its route waypoints — "
                           "refusing to package an altered route")

    home_obj = normalize_home(home)
    if home_obj is None:
        raise PackageError("no valid Home — a planning package requires a verified Home")

    rev = record.get("mission_revision", 0) if revision is None else revision
    inputs = record.get("planning_inputs") or {}
    metrics = record.get("metrics") or {}
    shoreline = (metrics.get("shoreline_clearance_m")
                 if metrics.get("shoreline_clearance_m") is not None
                 else inputs.get("shoreline_clearance_m"))

    navigable_rings = (record.get("navigable_geometry")
                       or inputs.get("navigable_boundary") or [])
    boundary_pts = _boundary_points(navigable_rings)
    no_go = record.get("no_go_zones") or inputs.get("no_go_zones") or []

    package = {
        "usv_id": usv_id,
        "mission_id": mission_id,
        "revision": rev,
        "home": home_obj,
        "route": route,
        "sections": [],
        "navigable_boundary": boundary_pts,
        "no_go_zones": no_go,
        "shoreline_clearance_m": shoreline if shoreline is not None else 0,
        "survey_graph": {},                 # not available operator-side; never fabricated
        "planner_metadata": {
            "mission_package_version": record.get("mission_package_version"),
            "route_contract_version": record.get("route_contract_version"),
            "input_revision": record.get("input_revision"),
            "route_content_hash": computed_hash,
            "waypoint_count": len(route),
            "segment_label_counts": segment_label_counts(route),
            "navigable_boundary_rings": navigable_rings,
        },
        "source": source,
    }

    limitations = []
    if not boundary_pts:
        limitations.append("navigable_boundary absent — Scout cannot prove connector safety")
    if not no_go:
        limitations.append("no no-go zones supplied")
    limitations.append("shoreline_clearance_m is a scalar metadata value — it is not itself "
                       "geometry Scout can run an onboard clearance check against")
    limitations.append("survey_graph not supplied by the Operator Station")

    meta = {
        "route_content_hash": computed_hash,
        "waypoint_count": len(route),
        "segment_label_counts": segment_label_counts(route),
        "boundary_supplied": bool(boundary_pts),
        "no_go_supplied": bool(no_go),
        "home": home_obj,
        "revision": rev,
        "mission_id": mission_id,
        "limitations": limitations,
    }
    return package, meta
