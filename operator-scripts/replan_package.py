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
TWO CONTRACTS LIVE HERE DURING THE MIGRATION
--------------------------------------------
`build_package` below builds the ORIGINAL package shape (`usv_id`/`revision`/`route`/
`navigable_boundary`) that the Scout Local Agent currently deployed on port 8090 validates.
`build_v1_package` at the bottom of this module builds `replan-planning-package-v1`, the
newer shape Scout's replanning rework accepts, which preserves the operator's FULL geometry
and provenance (detailed `segments`, detailed `original_execution_order`, the ring-nested
navigable geometry and no-go zones) instead of flattening it. They are deliberately separate
functions over the same record: neither is derived from the other, so a fix to one contract
can never silently reshape the other.
"""
from __future__ import annotations

import copy

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
    ({lat,lng}, {latitude,longitude}, {home_position:{...}}, or a positional [lng, lat] pair),
    or None when no valid fix exists. Never fabricates 0,0 — an absent Home stays absent so
    readiness reports it honestly.

    The positional branch is load-bearing, not defensive: the mission record's own
    `planning_inputs.planning_home` IS a bare `[lng, lat]` pair (planning._point_of), so
    without it the documented "fall back to the plan's planning home" path could never fire
    on a real record and a Scout that is not reporting a verified Home would look home-less."""
    if isinstance(home, (list, tuple)) and len(home) == 2:
        coord = _valid_coord(home[1], home[0])           # positional order is [lng, lat]
        return None if coord is None else {"latitude": coord[0], "longitude": coord[1]}
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


# ══════════════════════════════════════════════════════════════════════════════════════
# replan-planning-package-v1 — the lossless wire package
# ══════════════════════════════════════════════════════════════════════════════════════
# The v1 contract exists because the older package above FLATTENS the operator's planning
# product to fit a coarser consumer: it keeps only the exterior navigable ring, drops the
# typed `segments` entirely, and collapses the 6-field `original_execution_order` entries to
# a single per-waypoint label. Scout's replanner needs the real geometry to reason about a
# safe return, so v1 carries the operator's structures UNCHANGED:
#
#   planning_home             [lng, lat]                       — one positional pair
#   boundary                  [[lng, lat], ...]                — the survey ring, flat
#   navigable_geometry        [[[lng, lat], ...], ...]         — a LIST OF RINGS (inset may
#                                                                be a MultiPolygon)
#   no_go_zones               [[[lng, lat], ...], ...]         — a LIST OF RINGS; [] stays []
#   route_waypoints           [{latitude, longitude, loiter_time_s}, ...]
#   segments                  full objects (segment_id, kind, coordinates, length_m,
#                             raw_point_count, final_point_count, start/end_execution_seq)
#   original_execution_order  full objects (execution_seq, latitude, longitude,
#                             source_segment_id, source_segment_kind, source_index)
#
# Note the two DELIBERATELY DIFFERENT conventions in one package: geometry is positional
# `[lng, lat]` (GeoJSON order, what the planner stores and the map draws), while route
# waypoints are named `{latitude, longitude}` objects (what mission-contract-v1 hashes).
# Converting either one to the other would move bytes Scout verifies, so neither is
# "harmonized" here.
#
# `route_hash` is COPIED from the verified record, never recomputed into the package. The
# record's hash is the value the Pixhawk read-back was verified against; recomputing from a
# differently-normalized route would silently mint a second identity. We still cross-check
# the copy against `mission_contract.route_content_hash` (the SAME calculator that produced
# it) and refuse to build when they disagree — that is a tamper check on the record, not an
# alternative source for the field.

PACKAGE_VERSION_V1 = "replan-planning-package-v1"

# The exact top-level fields of the v1 wire package, in wire order. Named as a tuple so a
# test can assert the package carries these and nothing else — an extra key is as much a
# contract break as a missing one.
V1_FIELDS = (
    "package_version", "route_contract_version", "mission_id", "mission_revision",
    "vehicle_id", "route_hash", "planning_home", "boundary", "navigable_geometry",
    "no_go_zones", "shoreline_clearance_m", "route_waypoints", "segments",
    "original_execution_order", "immutable", "created_at", "source",
)

# OPTIONAL wire fields, emitted only when the operator can PROVE them from approved geometry.
#
# `home_corridor` is the single implicitly-closed `[[lng, lat], ...]` ring that covers the
# approved connector between the navigable survey area and the launch/Home area. Scout needs it
# to prove a safe return to a Home that lies outside the survey polygon.
#
# ABSENCE IS A REAL ANSWER AND IS LOAD-BEARING. When the mission carries no approved transit
# geometry — or when the derived corridor fails any of its checks — the key is OMITTED, Scout
# cannot prove the last leg of the return, and it fails closed in LOITER. That is the designed
# outcome. Nothing here invents a corridor to a runtime Home, widens one to reach a Home that
# fell outside it, or emits an empty ring to make the field "present".
V1_OPTIONAL_FIELDS = ("home_corridor",)

# Where the optional fields sit in wire order when they ARE present (after the geometry they
# relate to, before the route).
V1_FIELD_ORDER = (
    "package_version", "route_contract_version", "mission_id", "mission_revision",
    "vehicle_id", "route_hash", "planning_home", "boundary", "navigable_geometry",
    "no_go_zones", "home_corridor", "shoreline_clearance_m", "route_waypoints", "segments",
    "original_execution_order", "immutable", "created_at", "source",
)


def derive_home_corridor(record):
    """(ring, meta) for a record's approved Home corridor — see V1_OPTIONAL_FIELDS.

    The geometry lives in planning.py (which owns shapely/pyproj); it is imported LAZILY so this
    module stays importable, and the package stays buildable, on a backend without the geometry
    stack. Without it no corridor can be CHECKED, so none is emitted — the fail-closed direction.
    """
    try:
        import planning
    except Exception as exc:                                  # pragma: no cover - defensive
        return None, {"available": False, "reason": f"planning module unavailable ({exc})"}
    inputs = record.get("planning_inputs") if isinstance(record.get("planning_inputs"), dict) else {}
    navigable = record.get("navigable_geometry")
    if navigable is None:
        navigable = inputs.get("navigable_boundary")
    zones = record.get("no_go_zones")
    if zones is None:
        zones = inputs.get("no_go_zones")
    return planning.home_corridor_ring(
        segments=record.get("segments"),
        navigable_geometry=navigable,
        no_go_zones=zones,
        planning_home=inputs.get("planning_home"),
    )

# The fields every detailed record entry must carry. Preserved verbatim (plus anything else
# the record holds) — the point of v1 is that this metadata SURVIVES, so a missing one is a
# hard error rather than a quietly thinner package.
V1_SEGMENT_FIELDS = ("segment_id", "kind", "coordinates", "length_m", "raw_point_count",
                     "final_point_count", "start_execution_seq", "end_execution_seq")
V1_EXECUTION_ORDER_FIELDS = ("execution_seq", "latitude", "longitude", "source_segment_id",
                             "source_segment_kind", "source_index")


def canonical_vehicle_id(vehicle_id):
    """The canonical `usv-<n>` slug for any accepted spelling (2, "2", "usv-2", "USV-2").

    Pure and registry-free ON PURPOSE: the builder must stay network- and state-free, and the
    slug is a pure function of the numeric identity the record already carries. Returns None
    for anything that is not a positive vehicle number, so the caller fails closed rather
    than shipping a package addressed to nobody."""
    if isinstance(vehicle_id, bool):
        return None
    if isinstance(vehicle_id, int):
        n = vehicle_id
    else:
        text = str(vehicle_id or "").strip().lower()
        if text.startswith("usv-"):
            text = text[4:]
        try:
            n = int(text)
        except (TypeError, ValueError):
            return None
    return f"usv-{n}" if n > 0 else None


def _positional_pair(pt, what):
    """One `[lng, lat]` pair, validated and copied. Raises PackageError with `what` in the
    message rather than emitting a half-valid geometry Scout would have to guess about."""
    if not isinstance(pt, (list, tuple)) or len(pt) != 2:
        raise PackageError(f"{what} is not a [longitude, latitude] pair")
    coord = _valid_coord(pt[1], pt[0])          # positional order is [lng, lat]
    if coord is None:
        raise PackageError(f"{what} is not a valid [longitude, latitude] coordinate")
    return [float(pt[0]), float(pt[1])]


def _positional_ring(ring, what):
    """A `[[lng, lat], ...]` ring, validated point by point. An empty ring is refused: an
    empty polygon is not geometry, and shipping one would read as "checked, nothing there"."""
    if not isinstance(ring, list) or not ring:
        raise PackageError(f"{what} is not a non-empty ring of [longitude, latitude] points")
    return [_positional_pair(p, f"{what}[{i}]") for i, p in enumerate(ring)]


def _positional_rings(rings, what):
    """A `[[[lng, lat], ...], ...]` list of rings. An EMPTY LIST is preserved as `[]` — for
    no_go_zones that is the meaningful, checkable statement "there are no zones", which is
    categorically different from the field being absent."""
    if rings is None:
        raise PackageError(f"{what} is absent")
    if not isinstance(rings, list):
        raise PackageError(f"{what} is not a list of rings")
    return [_positional_ring(r, f"{what}[{i}]") for i, r in enumerate(rings)]


def _v1_route_waypoints(record):
    """The canonical route: the record's `route_waypoints` verbatim, as the exact three
    hashed fields. These are the bytes `route_hash` digests, so nothing is added here — the
    per-waypoint provenance lives in `original_execution_order`, position-aligned, instead."""
    waypoints = record.get("route_waypoints")
    if not isinstance(waypoints, list) or not waypoints:
        raise PackageError("mission record has no route_waypoints — nothing to package")
    out = []
    for i, wp in enumerate(waypoints):
        if not isinstance(wp, dict):
            raise PackageError(f"route waypoint {i} is not an object")
        coord = _valid_coord(wp.get("latitude"), wp.get("longitude"))
        if coord is None:
            raise PackageError(f"route waypoint {i} has an invalid coordinate")
        out.append({
            "latitude": coord[0],
            "longitude": coord[1],
            "loiter_time_s": round(float(wp.get("loiter_time_s", 0) or 0), 3),
        })
    return out


def _v1_detailed_list(items, required, what, count=None):
    """A list of detailed record objects, DEEP-COPIED whole. Every field in `required` must be
    present; every other field the record carries rides along untouched. The deep copy is what
    makes the builder non-mutating in both directions — the package can never alias, and so
    can never later corrupt, the immutable record's nested lists."""
    if not isinstance(items, list) or not items:
        raise PackageError(f"mission record has no {what} — v1 requires the full metadata")
    if count is not None and len(items) != count:
        raise PackageError(f"{what} has {len(items)} entries but the route has {count} "
                           f"waypoints — the record's provenance is not 1:1 with its route")
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise PackageError(f"{what}[{i}] is not an object")
        missing = [f for f in required if item.get(f) is None]
        if missing:
            raise PackageError(f"{what}[{i}] is missing {', '.join(missing)} — refusing to "
                               f"ship a package with thinned-out metadata")
        out.append(copy.deepcopy(item))
    return out


def build_v1_package(record, *, vehicle_id=None, source=SOURCE, home_corridor=None):
    """Build the `replan-planning-package-v1` wire package from an immutable revision-0
    mission record. Returns (package, meta).

    PURE: no network, no clock, no global state. The same record always yields byte-identical
    output — `created_at` is the RECORD's creation time, not `now()`, precisely so that
    re-sending an unchanged mission produces an unchanged package and Scout's single-slot
    store stays idempotent.

    NON-MUTATING: the record is only read; every nested structure that reaches the package is
    a fresh copy.

    Raises PackageError — never a thinner package — when the record cannot yield a complete,
    hash-consistent v1 package.
    """
    if not isinstance(record, dict):
        raise PackageError("mission record is not an object")

    mission_id = record.get("mission_id")
    if not isinstance(mission_id, str) or not mission_id.strip():
        raise PackageError("mission record has no mission_id")

    # v1 packages describe the ORIGINAL approved mission. A derived revision is a different
    # artifact with a different provenance and is not what Scout replans from.
    revision = record.get("mission_revision")
    if revision != 0:
        raise PackageError(f"mission record is revision {revision!r} — a v1 planning package "
                           f"is built only from the immutable revision-0 original")
    if record.get("immutable") is not True:
        raise PackageError("mission record is not marked immutable — refusing to package it")

    usv = canonical_vehicle_id(vehicle_id if vehicle_id is not None else record.get("vehicle_id"))
    if usv is None:
        raise PackageError("mission record has no resolvable vehicle identity")
    # Identity isolation: an explicit target must be the record's own vehicle. One USV's
    # approved geometry must never be addressable to another USV's Scout.
    record_usv = canonical_vehicle_id(record.get("vehicle_id"))
    if vehicle_id is not None and record_usv is not None and usv != record_usv:
        raise PackageError(f"mission record belongs to {record_usv} — refusing to build a "
                           f"package addressed to {usv}")

    contract_version = record.get("route_contract_version")
    if contract_version != mission_contract.CONTRACT_VERSION:
        raise PackageError(f"mission record route_contract_version is {contract_version!r}, "
                           f"not {mission_contract.CONTRACT_VERSION}")

    route_waypoints = _v1_route_waypoints(record)

    # The hash is COPIED, then tamper-checked against the same calculator that produced it.
    stored_hash = record.get("route_hash")
    if not isinstance(stored_hash, str) or not stored_hash.startswith(mission_contract.HASH_PREFIX):
        raise PackageError("mission record has no canonical route_hash")
    if stored_hash != mission_contract.route_content_hash(record.get("route_waypoints") or []):
        raise PackageError("mission record route_hash disagrees with its route waypoints — "
                           "refusing to package an altered route")

    inputs = record.get("planning_inputs") if isinstance(record.get("planning_inputs"), dict) else {}
    metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}

    planning_home = _positional_pair(inputs.get("planning_home"), "planning_home")
    boundary = _positional_ring(inputs.get("boundary"), "boundary")

    navigable = record.get("navigable_geometry")
    if navigable is None:
        navigable = inputs.get("navigable_boundary")
    navigable_geometry = _positional_rings(navigable, "navigable_geometry")
    if not navigable_geometry:
        raise PackageError("navigable_geometry is empty — Scout cannot prove a return safe "
                           "against no navigable area")

    # `[]` is a real, checkable answer here and is preserved as one — hence the explicit
    # None test rather than an `or`, which would erase the distinction.
    zones = record.get("no_go_zones")
    if zones is None:
        zones = inputs.get("no_go_zones")
    no_go_zones = _positional_rings(zones, "no_go_zones")

    clearance = metrics.get("shoreline_clearance_m")
    if clearance is None:
        clearance = inputs.get("shoreline_clearance_m")
    if clearance is None:
        raise PackageError("mission record has no shoreline_clearance_m")

    created_at = record.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise PackageError("mission record has no created_at")

    # The approved Home corridor, when — and ONLY when — the record's own approved geometry
    # proves one. A refusal here is not an error: the key is omitted and Scout fails closed.
    if home_corridor is None:
        corridor, corridor_meta = derive_home_corridor(record)
    else:
        corridor = home_corridor
        corridor_meta = {"available": True, "reason": None, "source": "caller"}
    if corridor is not None:
        corridor = _positional_ring(corridor, "home_corridor")
        if len({tuple(p) for p in corridor}) < 3:
            raise PackageError("home_corridor has fewer than 3 distinct vertices")

    package = {
        "package_version": PACKAGE_VERSION_V1,
        "route_contract_version": contract_version,
        "mission_id": mission_id,
        "mission_revision": 0,
        "vehicle_id": usv,
        "route_hash": stored_hash,
        "planning_home": planning_home,
        "boundary": boundary,
        "navigable_geometry": navigable_geometry,
        "no_go_zones": no_go_zones,
        # OMITTED entirely when no corridor is proven — never `null`, never `[]`. An empty ring
        # would read as "checked, nothing there"; absence reads as "not proven", which is what
        # makes Scout fail closed instead of returning through unapproved water.
        **({"home_corridor": corridor} if corridor is not None else {}),
        "shoreline_clearance_m": clearance,
        "route_waypoints": route_waypoints,
        "segments": _v1_detailed_list(record.get("segments"), V1_SEGMENT_FIELDS, "segments"),
        "original_execution_order": _v1_detailed_list(
            record.get("original_execution_order"), V1_EXECUTION_ORDER_FIELDS,
            "original_execution_order", count=len(route_waypoints)),
        "immutable": True,
        "created_at": created_at,
        "source": source,
    }

    # What the operator KNOWS it sent, so the UI can show it beside Scout's verdict without
    # re-deriving anything from the package it just built.
    meta = {
        "package_version": PACKAGE_VERSION_V1,
        "mission_id": mission_id,
        "vehicle_id": usv,
        "route_hash": stored_hash,
        "route_waypoint_count": len(route_waypoints),
        "segment_count": len(package["segments"]),
        "execution_order_count": len(package["original_execution_order"]),
        "navigable_ring_count": len(navigable_geometry),
        "no_go_zone_count": len(no_go_zones),
        "boundary_point_count": len(boundary),
        "shoreline_clearance_m": clearance,
        "home_corridor_supplied": corridor is not None,
        "home_corridor_vertex_count": len(corridor) if corridor is not None else 0,
        "home_corridor": corridor_meta,
        "limitations": _v1_limitations(no_go_zones, corridor, corridor_meta),
    }
    return package, meta


def _v1_limitations(no_go_zones, corridor=None, corridor_meta=None):
    """What the package does NOT prove, stated by the operator rather than left for Scout to
    discover. Reported alongside every sync so an absent constraint is never read as a
    cleared one."""
    limitations = []
    if not no_go_zones:
        limitations.append("no no-go zones were defined for this mission — an empty list is "
                           "the operator's actual input, not a missing constraint")
    limitations.append("shoreline_clearance_m is a scalar metadata value — it is not itself "
                       "geometry Scout can run an onboard clearance check against")
    limitations.append("no survey graph is supplied — Scout cannot re-derive coverage lanes "
                       "from this package, only reuse the approved route and geometry")
    if corridor is None:
        reason = (corridor_meta or {}).get("reason")
        limitations.append(
            "no approved home_corridor is supplied"
            + (f" ({reason})" if reason else "")
            + " — if the runtime launch Home lies outside the approved navigable geometry, "
              "Scout cannot prove a safe return and will fail closed in LOITER")
    else:
        limitations.append(
            f"home_corridor is the approved transit path buffered to "
            f"{(corridor_meta or {}).get('half_width_m')} m either side; it proves a connector "
            f"to the PLANNED Home only. A runtime launch Home outside it is not covered, and "
            f"the corridor is never widened to reach one")
    return limitations
