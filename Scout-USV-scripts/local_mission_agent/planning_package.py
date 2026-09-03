"""
The approved planning package Scout persists locally so a safe-return replan
remains possible while DISCONNECTED from the Operator Station and after a Local
Agent process restart.

What the Operator Station must supply (documented contract)
----------------------------------------------------------
The package is authored by the Operator Station at mission approval time and
delivered to Scout (in this first phase, seeded locally / via a future operator
command). At minimum it must contain, to make a safe-return strategy possible:

    {
      "usv_id": "usv-2",
      "mission_id": "<id of the approved mission>",
      "revision": 0,
      "home": {"latitude": .., "longitude": ..},          # REQUIRED
      "route": [                                            # REQUIRED, non-empty
        {"latitude": .., "longitude": .., "loiter_time_s": 0,
         "segment": "OUTBOUND_TRANSIT"},                    # segment optional
        ...
      ],
      "no_go_zones": [ [[lat,lon],[lat,lon],...], ... ],    # OPTIONAL
      "no_go_clearance_m": 5.0,               # OPTIONAL; missing -> 0.0 (see below)
      "survey_graph": { ... mission_graph.build_survey_graph output ... },  # OPTIONAL
      "source": "OPERATOR_STATION"
    }

Semantic segments (section 5) let a safe-return distinguish outbound transit /
primary survey / secondary survey / return legs instead of assuming "the last
waypoint is Home". They are OPTIONAL for backward compatibility: a package (or
legacy mission) with no `segment` on its waypoints is accepted and every point
is treated as UNSPECIFIED -- the retrace safe-return strategy still works, it
just cannot label which leg a point belonged to.

If no package is stored, or it lacks Home or a route, the safe-return planner
fails closed (see safe_return_planner.py) rather than inventing a direct line.

Persistence is a single atomic-replace JSON file (config.PLANNING_PACKAGE_FILE),
same durability approach as mission_operation_status.py.
"""
import hashlib
import json
import math
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import config
import geo
import route_hash

# Semantic segment tags.
SEGMENT_OUTBOUND_TRANSIT = "OUTBOUND_TRANSIT"
SEGMENT_PRIMARY_SURVEY = "PRIMARY_SURVEY"
SEGMENT_SECONDARY_SURVEY = "SECONDARY_SURVEY"
SEGMENT_RETURN = "RETURN"
SEGMENT_UNSPECIFIED = "UNSPECIFIED"

VALID_SEGMENTS = (
    SEGMENT_OUTBOUND_TRANSIT, SEGMENT_PRIMARY_SURVEY,
    SEGMENT_SECONDARY_SURVEY, SEGMENT_RETURN, SEGMENT_UNSPECIFIED,
)

# Bounds enforced when the Operator Station uploads a package (validate_incoming).
# MAX_ROUTE_WAYPOINTS matches the vehicle Flask mission-contract-v1 ceiling
# (services/mission_contract.MAX_ROUTE_WAYPOINTS = 200) -- a package Scout
# cannot upload as a mission is not a package it should store. MAX_PACKAGE_BYTES
# bounds the whole serialized payload (survey_graph/boundary can be large) so an
# over-sized upload is refused before it touches disk.
MAX_ROUTE_WAYPOINTS = 200
MAX_PACKAGE_BYTES = 512 * 1024

# Planning-package / mission consistency states (see check_consistency).
CONSISTENCY_OK = "PLANNING_PACKAGE_CONSISTENT"
CONSISTENCY_MISSING = "PLANNING_PACKAGE_MISSING"
CONSISTENCY_UNUSABLE = "PLANNING_PACKAGE_UNUSABLE"
CONSISTENCY_MISSION_MISMATCH = "PLANNING_PACKAGE_MISSION_MISMATCH"
CONSISTENCY_HASH_MISMATCH = "PLANNING_PACKAGE_HASH_MISMATCH"

# ── Vehicle-side legacy operator mission-LABEL namespace (mission binding/
# reproof identity bug root cause) ──────────────────────────────────────────
# Flask app.py's legacy `/start_mission` endpoint sets vehicle_state.mission.
# current_mission_id to an OPERATOR-TYPED sensor-logging label
# ("<YYYY-MM-DD_HH-MM>_<free-text name>", e.g. "2026-08-20_11-54_biltema 1"),
# used only to tag measurements for InfluxDB/CSV export -- a completely
# separate feature from planning-package upload/replan. It is therefore a
# DIFFERENT IDENTIFIER NAMESPACE from this module's canonical `msn-*` mission
# identity: the Pixhawk/MAVLink mission itself carries no such id at all, and
# the legacy label was never derived from or propagated to a package upload.
# A value in THIS format is never valid evidence either for or against
# route-binding identity and must be excluded wherever a vehicle-reported
# mission id is compared against the resolved/expected canonical identity
# (mission_execution_controller's Start/reprove identity proof and
# mission_progression's live AUTO-progression identity gate both use this).
# Any OTHER non-null vehicle-reported id -- including a bare token, or a
# stale/previous canonical msn-* id genuinely left over from an earlier
# upload -- is NOT in this format and remains fully comparable; a real
# disagreement there still fails closed exactly as before this fix.
_LEGACY_OPERATOR_MISSION_LABEL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}_.+$")


def is_legacy_operator_mission_label(vehicle_mission_id: Optional[str]) -> bool:
    """True when `vehicle_mission_id` is Flask's legacy `/start_mission`
    operator-typed sensor-logging label (`YYYY-MM-DD_HH-MM_<name>`) rather
    than a value in this module's canonical `msn-*` mission-identity
    namespace -- see the module-level comment above _LEGACY_OPERATOR_MISSION_
    LABEL_RE for why this makes it non-comparable, non-evidentiary supporting
    data for a route-binding identity conflict."""
    return bool(vehicle_mission_id) and bool(
        _LEGACY_OPERATOR_MISSION_LABEL_RE.match(vehicle_mission_id))

# ── Replan planning-package v1 contract (the immutable original safety envelope
# the Operator Station authors and hands Scout for onboard replanning) ─────────
#
# The Operator owns mission generation and the immutable original mission record;
# Scout receives, validates (structurally AND against the live Pixhawk route),
# and stores this package. This first version does NOT execute a mission
# revision -- it only establishes the receiving/validation/storage/readiness
# contract. Wire coordinate convention: positional arrays (planning_home,
# boundary, navigable_geometry, no_go_zone rings) are GeoJSON [longitude,
# latitude]; route_waypoints are objects with explicit latitude/longitude keys so
# their ordering is unambiguous. Scout canonicalizes geometry to the internal
# (latitude, longitude) convention on ingest (geo.py / safe_return_planner.py).
SUPPORTED_PACKAGE_VERSIONS = ("replan-planning-package-v1",)
SUPPORTED_ROUTE_CONTRACT_VERSIONS = ("mission-contract-v1",)

# On-disk envelope wrapping the accepted package with Scout-side provenance and a
# storage shape that keeps the immutable original (revision 0), the current
# active revision, and derived revision history SEPARABLE -- so later code that
# does execute revisions never has to overwrite the original safety envelope.
STORE_VERSION = "replan-planning-store-v1"

# Readiness states (build_readiness). Distinguish every reason replanning is not
# yet ready so the Operator/Agent surface can show WHY, not just false.
#
# The FRESHNESS axis is kept STRICTLY SEPARATE from the CONSISTENCY axis. A
# stale, expired, or still-refreshing PROOF only means we do not currently hold
# fresh evidence -- it is NEVER, on its own, a package inconsistency. Reserve
# PLANNING_PACKAGE_STALE for a COMPLETED, FRESH proof that shows the stored
# package no longer matches the mission actually on the vehicle (mission-id /
# route-hash / waypoint-count mismatch, or an invalid Pixhawk mission). This is
# the defect the readiness state machine used to have: an expired or refreshing
# cache read was mapped to PLANNING_PACKAGE_STALE, so READY oscillated to a
# spurious "package inconsistent" every time the ~8 s cache lapsed.
READY_USABLE = "REPLANNING_READY"
READY_MISSING = "PLANNING_PACKAGE_MISSING"
READY_INVALID = "PLANNING_PACKAGE_STRUCTURALLY_INVALID"
READY_PIXHAWK_UNAVAILABLE = "PIXHAWK_UNAVAILABLE"
READY_HASH_UNAVAILABLE = "HASH_COMPARISON_UNAVAILABLE"
READY_REFRESHING = "REPLANNING_READINESS_REFRESHING"   # a refresh/download is in flight (transient)
READY_PROOF_STALE = "REPLANNING_PROOF_STALE"           # cache expired / unattributed proof; NO proven mismatch
READY_PACKAGE_STALE = "PLANNING_PACKAGE_STALE"         # a COMPLETED, FRESH proof shows a genuine mismatch
# Backward-compatible aliases: what earlier code reported as READY_STALE /
# READY_HASH_MISMATCH for a genuine, freshly-proven inconsistency is now the one
# READY_PACKAGE_STALE bucket. Kept so existing references keep resolving.
READY_STALE = READY_PACKAGE_STALE
READY_HASH_MISMATCH = READY_PACKAGE_STALE

# ── Safety-proof freshness of a Pixhawk mission readback ───────────────────
# GET /agent/pixhawk_mission is cache-first and non-blocking on the Flask side
# (services/mission_service.py's readback coordinator). That is correct for
# UI/display reads, but a SAFETY PROOF (Start identity, READY/can_start,
# replanning route-consistency, mission acceptance) must never rest on a stale,
# still-refreshing, or unattributed readback.
#
# A readback is acceptable proof ONLY when it declares an explicit, recognized
# proof_source and satisfies that source's freshness contract:
#
#   DIRECT_TRANSACTION -- returned straight from a trusted in-process download
#     transaction (the Flask side stamps download_pixhawk_mission results).
#     Freshness is established by the transaction; require its completion time
#     (proof_completed_at) present and within PROOF_MAX_CACHE_AGE_S of now.
#
#   COORDINATED_CACHE -- served by GET /agent/pixhawk_mission. Require
#     refresh_generation, observed_at, age_s within PROOF_MAX_CACHE_AGE_S, and
#     stale/refreshing/busy all false.
#
# A readback with NO proof_source (or an unknown one) -- even one carrying a
# valid-looking hash/count -- is REJECTED for every safety consumer. Absence of
# explicit provenance is never treated as fresh. (Display-only consumers may
# still render such a payload as unavailable/legacy; they simply never gate a
# vehicle action on it.)
PROOF_SOURCE_DIRECT = "DIRECT_TRANSACTION"
PROOF_SOURCE_CACHE = "COORDINATED_CACHE"
PROOF_MAX_CACHE_AGE_S = float(os.environ.get("REPLAN_PROOF_MAX_CACHE_AGE_S", "8.0"))


def readback_is_fresh(readback: Any, now: Optional[float] = None,
                      max_age_s: float = PROOF_MAX_CACHE_AGE_S) -> "tuple":
    """(fresh: bool, reason: Optional[str]) -- the single freshness predicate for
    every safety-proof consumer. Requires an explicit, recognized proof_source;
    see the module section above."""
    if not isinstance(readback, dict):
        return False, "no Pixhawk mission readback available"
    if readback.get("busy"):
        return False, "Pixhawk mission readback is busy (a mission download is in progress)"

    source = readback.get("proof_source")
    if source == PROOF_SOURCE_DIRECT:
        completed = readback.get("proof_completed_at")
        if not isinstance(completed, (int, float)):
            return False, "DIRECT_TRANSACTION readback has no transaction completion time"
        now = time.time() if now is None else now
        age = now - completed
        if age > max_age_s:
            return False, (f"DIRECT_TRANSACTION readback completed {age:.1f}s ago, "
                           f"exceeding the {max_age_s:.1f}s proof freshness limit")
        return True, None

    if source == PROOF_SOURCE_CACHE:
        if readback.get("refreshing") is True:
            return False, "coordinated-cache readback is refreshing (a background download is in progress)"
        if readback.get("stale") is True:
            return False, "coordinated-cache readback is stale"
        if not isinstance(readback.get("refresh_generation"), int):
            return False, "coordinated-cache readback has no refresh_generation"
        if not isinstance(readback.get("observed_at"), (int, float)):
            return False, "coordinated-cache readback has no observed_at timestamp"
        age = readback.get("age_s")
        if not isinstance(age, (int, float)):
            return False, "coordinated-cache readback has no age"
        if age > max_age_s:
            return False, (f"coordinated-cache readback age {age:.1f}s exceeds the "
                           f"{max_age_s:.1f}s proof freshness limit")
        return True, None

    return False, (f"Pixhawk mission readback has no recognized proof_source "
                   f"(got {source!r}); refusing to treat it as a safety proof")


_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_lock = threading.Lock()


def _valid_coord(lat, lon) -> bool:
    if lat is None or lon is None:
        return False
    try:
        lat = float(lat); lon = float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    if lat == 0.0 and lon == 0.0:
        return False  # ArduPilot "no fix" sentinel, never a real coordinate
    return True


def _normalize_polygon(poly):
    """A polygon (list of [lat,lon] pairs or {latitude,longitude} dicts) into a
    canonical [[lat, lon], ...] list. Malformed vertices are skipped."""
    out = []
    for v in poly or []:
        if isinstance(v, dict):
            la, lo = v.get("latitude", v.get("lat")), v.get("longitude", v.get("lng", v.get("lon")))
        elif isinstance(v, (list, tuple)) and len(v) >= 2:
            la, lo = v[0], v[1]
        else:
            continue
        if la is not None and lo is not None:
            out.append([float(la), float(lo)])
    return out

_FILE = getattr(config, "PLANNING_PACKAGE_FILE",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "planning_package.json"))


def _normalize_route(route: Any) -> List[dict]:
    """Coerce a supplied route into canonical waypoints with a segment tag.
    Backward compatible: a missing/unknown segment becomes UNSPECIFIED rather
    than being rejected."""
    out: List[dict] = []
    for wp in route or []:
        if not isinstance(wp, dict):
            continue
        lat = wp.get("latitude", wp.get("lat"))
        lon = wp.get("longitude", wp.get("lng", wp.get("lon")))
        if lat is None or lon is None:
            continue
        loiter = wp.get("loiter_time_s", wp.get("loiter_time", wp.get("loiter", 0))) or 0
        segment = wp.get("segment")
        if segment not in VALID_SEGMENTS:
            segment = SEGMENT_UNSPECIFIED
        out.append({
            "latitude": float(lat),
            "longitude": float(lon),
            "loiter_time_s": float(loiter),
            "segment": segment,
        })
    return out


def _normalize_home(home: Any) -> Optional[dict]:
    if not isinstance(home, dict):
        return None
    lat = home.get("latitude", home.get("lat"))
    lon = home.get("longitude", home.get("lng", home.get("lon")))
    if lat is None or lon is None:
        return None
    return {"latitude": float(lat), "longitude": float(lon)}


def build_package(
    mission_id: Optional[str],
    route: List[dict],
    home: Optional[dict],
    usv_id: Optional[str] = None,
    revision: int = 0,
    no_go_zones: Optional[List[List[Any]]] = None,
    survey_graph: Optional[dict] = None,
    source: str = "OPERATOR_STATION",
    sections: Optional[List[Any]] = None,
    navigable_boundary: Optional[List[Any]] = None,
    home_corridor: Optional[List[Any]] = None,
    shoreline_clearance_m: Optional[float] = None,
    no_go_clearance_m: Optional[float] = None,
    planner_metadata: Optional[dict] = None,
    now: Optional[float] = None,
) -> dict:
    """Assemble (but do not persist) a normalized package dict, computing the
    original route content hash so it can be compared later. Home/route are
    normalized; segment tags default to UNSPECIFIED when absent. The optional
    approved-geometry fields (navigable_boundary, no_go_zones) are normalized to
    canonical [[lat,lon],...] polygons so the planner can consume them directly.
    no_go_clearance_m is the scalar outward-buffer distance (metres) a safe
    return must keep beyond no_go_zones; missing/None normalizes to 0.0 -- the
    historical raw-zone-only exclusion semantics (see no_go_clearance_m_of)."""
    now = time.time() if now is None else now
    norm_route = _normalize_route(route)
    norm_no_go = [_normalize_polygon(z) for z in (no_go_zones or [])]
    norm_no_go = [z for z in norm_no_go if len(z) >= 3]
    norm_boundary = _normalize_polygon(navigable_boundary) if navigable_boundary else []
    norm_corridor = _normalize_polygon(home_corridor) if home_corridor else []
    return {
        "usv_id": usv_id,
        "mission_id": mission_id,
        "revision": revision,
        "home": _normalize_home(home),
        "route": norm_route,
        "original_route_hash": route_hash.route_content_hash(norm_route),
        "sections": sections or [],
        "navigable_boundary": norm_boundary,
        "home_corridor": norm_corridor,
        "no_go_zones": norm_no_go,
        "shoreline_clearance_m": shoreline_clearance_m,
        "no_go_clearance_m": float(no_go_clearance_m) if no_go_clearance_m is not None else 0.0,
        "survey_graph": survey_graph,
        "planner_metadata": planner_metadata or {},
        "source": source,
        "created_at": round(now, 3),
    }


def validate_incoming(body: Any, expected_usv_id: Optional[str]) -> "tuple":
    """
    SUPERSEDED by validate_package_v1 (the replan-planning-package-v1 acceptance
    path replan_api.put_planning_package now uses). Retained only as the earlier,
    looser safe-return seeding validator (no package/route-contract version, no
    mandatory navigable geometry, no immutable flag, no live-Pixhawk check); it is
    NOT wired to any HTTP route. Do not use for new acceptance -- it exists so the
    older internal build_package/save seeding semantics stay documented.

    Validate an Operator-Station-supplied planning-package upload. Returns
    (package_or_None, error_code, error_message). On success the returned
    package is the normalized dict ready to persist; on any problem the package
    is None and NOTHING is stored.

    Checks: JSON object; payload size; target usv_id matches this Scout;
    mission_id present; non-empty route within the size ceiling; valid
    coordinates (not 0/0); supported semantic segments; valid Home. The route
    content hash is computed with the existing mission-contract-v1
    canonicalization (route_hash).
    """
    if not isinstance(body, dict):
        return None, "INVALID_REQUEST", "request body must be a JSON object"

    # Bound the raw payload before doing anything else.
    try:
        size = len(json.dumps(body).encode("utf-8"))
    except (TypeError, ValueError):
        return None, "INVALID_REQUEST", "request body is not JSON-serializable"
    if size > MAX_PACKAGE_BYTES:
        return None, "PACKAGE_TOO_LARGE", (
            f"package is {size} bytes; the maximum is {MAX_PACKAGE_BYTES}"
        )

    usv_id = body.get("usv_id")
    if expected_usv_id is not None and usv_id != expected_usv_id:
        return None, "WRONG_TARGET_USV", (
            f"package targets usv_id {usv_id!r}; this Scout is {expected_usv_id!r}"
        )

    mission_id = body.get("mission_id")
    if not mission_id or not isinstance(mission_id, str):
        return None, "MISSING_MISSION_ID", "mission_id is required and must be a non-empty string"

    route = body.get("route")
    if not isinstance(route, list) or not route:
        return None, "EMPTY_ROUTE", "route must be a non-empty list of waypoints"
    if len(route) > MAX_ROUTE_WAYPOINTS:
        return None, "ROUTE_TOO_LARGE", (
            f"route has {len(route)} waypoints; the maximum is {MAX_ROUTE_WAYPOINTS}"
        )
    for i, wp in enumerate(route):
        if not isinstance(wp, dict):
            return None, "INVALID_WAYPOINT", f"route waypoint {i} is not an object"
        lat = wp.get("latitude", wp.get("lat"))
        lon = wp.get("longitude", wp.get("lng", wp.get("lon")))
        if not _valid_coord(lat, lon):
            return None, "INVALID_COORDINATE", (
                f"route waypoint {i} has an invalid or zero coordinate ({lat},{lon})"
            )
        seg = wp.get("segment")
        if seg is not None and seg not in VALID_SEGMENTS:
            return None, "UNSUPPORTED_SEGMENT", (
                f"route waypoint {i} has unsupported segment {seg!r} "
                f"(supported: {', '.join(VALID_SEGMENTS)})"
            )
        loiter = wp.get("loiter_time_s", wp.get("loiter_time", wp.get("loiter", 0))) or 0
        try:
            if float(loiter) < 0:
                return None, "INVALID_WAYPOINT", f"route waypoint {i} has a negative loiter_time_s"
        except (TypeError, ValueError):
            return None, "INVALID_WAYPOINT", f"route waypoint {i} has a non-numeric loiter_time_s"

    home = _normalize_home(body.get("home"))
    if home is None or not _valid_coord(home["latitude"], home["longitude"]):
        return None, "INVALID_HOME", "home must have a valid, non-zero latitude/longitude"

    package = build_package(
        mission_id=mission_id,
        route=route,
        home=home,
        usv_id=usv_id,
        revision=body.get("revision", 0) or 0,
        no_go_zones=body.get("no_go_zones"),
        survey_graph=body.get("survey_graph"),
        source=body.get("source", "OPERATOR_STATION"),
        sections=body.get("sections"),
        navigable_boundary=body.get("navigable_boundary"),
        shoreline_clearance_m=body.get("shoreline_clearance_m"),
        planner_metadata=body.get("planner_metadata"),
    )
    return package, None, None


def check_consistency(package: Optional[dict], current_mission_id: Optional[str] = None,
                      current_route_hash: Optional[str] = None) -> "tuple":
    """
    Decide whether the stored package may be safely used to replan the currently
    known mission. Returns (state, detail_dict). state is one of the
    CONSISTENCY_* constants. Fail closed: anything but CONSISTENT means "do not
    replan from this package".

    Hash comparison is best-effort: it only runs when a current mission hash is
    actually available (`current_route_hash`); otherwise it is reported as
    not-checked rather than assumed to match.
    """
    detail = {
        "package_mission_id": (package or {}).get("mission_id"),
        "current_mission_id": current_mission_id,
        "package_route_hash": (package or {}).get("original_route_hash"),
        "current_route_hash": current_route_hash,
        "hash_checked": False,
    }
    if not package:
        return CONSISTENCY_MISSING, detail
    if not is_usable(package):
        return CONSISTENCY_UNUSABLE, detail
    pkg_mid = package.get("mission_id")
    if pkg_mid and current_mission_id and pkg_mid != current_mission_id:
        return CONSISTENCY_MISSION_MISMATCH, detail
    if current_route_hash is not None:
        detail["hash_checked"] = True
        if package.get("original_route_hash") and package["original_route_hash"] != current_route_hash:
            return CONSISTENCY_HASH_MISMATCH, detail
    return CONSISTENCY_OK, detail


def _read_raw_nolock() -> Optional[dict]:
    """The raw persisted object (flat legacy package OR a v1 store envelope), or
    None if nothing is stored / it is unreadable. Caller must hold `_lock`."""
    try:
        with open(_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write_raw_nolock(obj: dict) -> None:
    """Atomic-replace `obj` onto _FILE (tmp write + os.replace) so a concurrent
    reader never observes a half-written file. Caller must hold `_lock`."""
    tmp = f"{_FILE}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, _FILE)
    except OSError as e:
        print(f"[PLANNING_PACKAGE] could not persist package: {e}")


def _is_envelope(raw: Any) -> bool:
    return isinstance(raw, dict) and raw.get("store_version") == STORE_VERSION


def save_package(package: dict) -> dict:
    """Persist a flat `package` atomically, replacing any existing one. Returns
    it. This is the legacy single-slot writer used by the internal safe-return
    seeding path (`save`/`build_package`); the v1 acceptance path writes a full
    store envelope via `store_accepted`."""
    with _lock:
        _write_raw_nolock(package)
        return package


def save(mission_id, route, home, **kwargs) -> dict:
    """Convenience: build + persist in one call."""
    return save_package(build_package(mission_id, route, home, **kwargs))


def update_home(new_home: Any) -> Optional[dict]:
    """
    Synchronize the stored package's Home to `new_home` and re-persist it,
    returning the updated package (or None if there is no stored package or the
    supplied Home is invalid, in which case NOTHING is changed).

    Used by the mission-execution Start transaction (section 9): once the live
    launch Home has been verified via Set Home, the approved package's planned
    Home is replaced with the verified launch Home so a later safe-return plans
    back to where the boat actually launched, not to a stale planned Home. The
    route is untouched, so original_route_hash -- which is computed over the
    route only, Home excluded (route_hash.py) -- is preserved, and mission/hash
    consistency is unaffected. Fail closed: an invalid Home is refused rather
    than written, so the caller can treat None as "synchronization failed".
    """
    home = _normalize_home(new_home)
    if home is None or not _valid_coord(home["latitude"], home["longitude"]):
        return None
    with _lock:
        raw = _read_raw_nolock()
        if raw is None:
            return None
        if _is_envelope(raw):
            # Sync only the ACTIVE package's Home. The immutable original safety
            # envelope (original_package) is left pristine on purpose -- Home is
            # excluded from the route hash, so mission/hash consistency is
            # unaffected either way, and the original record stays authoritative.
            active = dict(raw.get("active_package") or raw.get("original_package") or {})
            if not active:
                return None
            active["home"] = home
            active["planning_home"] = [home["longitude"], home["latitude"]]
            active["home_synchronized_at"] = round(time.time(), 3)
            raw = dict(raw)
            raw["active_package"] = active
            _write_raw_nolock(raw)
            return active
        pkg = dict(raw)
        pkg["home"] = home
        pkg["home_synchronized_at"] = round(time.time(), 3)
        _write_raw_nolock(pkg)
        return pkg


def load() -> Optional[dict]:
    """The persisted approved package (the ACTIVE revision), or None if none is
    stored / unreadable. Transparently unwraps a v1 store envelope so every
    downstream consumer keeps reading the flat package (home/route/
    original_route_hash/...) exactly as before; a legacy flat package is returned
    as-is. A package missing Home or a non-empty route is returned as-is (the
    planner is responsible for failing closed on it)."""
    with _lock:
        raw = _read_raw_nolock()
    if raw is None:
        return None
    if _is_envelope(raw):
        return raw.get("active_package") or raw.get("original_package")
    return raw


def clear() -> bool:
    """Remove the stored package. Idempotent -- returns True if a package was
    actually removed, False if there was nothing to remove."""
    with _lock:
        try:
            os.remove(_FILE)
            return True
        except OSError:
            return False


def is_usable(package: Optional[dict]) -> bool:
    """True only if the package can support a safe-return strategy: a Home and a
    non-empty route. This is the fail-closed gate the planner relies on."""
    if not package:
        return False
    home = _normalize_home(package.get("home"))
    route = package.get("route") or []
    return home is not None and len(route) > 0


def summary(package: Optional[dict]) -> dict:
    """Compact status view of what is stored, for the Agent page. Never dumps
    the full route (bounded status payload)."""
    if not package:
        return {"stored": False, "usable": False}
    route = package.get("route") or []
    segments: Dict[str, int] = {}
    for wp in route:
        seg = wp.get("segment", SEGMENT_UNSPECIFIED)
        segments[seg] = segments.get(seg, 0) + 1
    return {
        "stored": True,
        "usable": is_usable(package),
        "package_version": package.get("package_version"),
        "route_contract_version": package.get("route_contract_version"),
        "mission_id": package.get("mission_id"),
        "revision": package.get("revision"),
        "mission_revision": package.get("mission_revision", package.get("revision")),
        "immutable": package.get("immutable"),
        "route_waypoint_count": len(route),
        "segment_counts": segments,
        "has_home": _normalize_home(package.get("home")) is not None,
        # no_go_zones is present-and-explicit vs. absent: an empty list means
        # "checked zero zones", a missing field is not equivalent (see the v1
        # contract). Report both the count and whether the field was supplied.
        "no_go_zones_present": package.get("no_go_zones") is not None,
        "no_go_zone_count": len(package.get("no_go_zones") or []),
        "has_navigable_geometry": _navigable_geometry_present(package),
        "has_navigable_boundary": len(package.get("navigable_boundary") or []) >= 3,
        # Operator-provided approved Home/launch corridor (safe_return_planner's
        # Home-connector contract, task section 4) -- surfaced here so a preflight
        # can see Home/corridor evidence BEFORE Start, not only after a replan
        # validation has already run and populated geometry_validation.
        "has_home_corridor": len(package.get("home_corridor") or []) >= 3,
        "shoreline_clearance_m": package.get("shoreline_clearance_m"),
        # Outward buffer (metres) a safe return must keep beyond no_go_zones.
        # Always the NORMALIZED effective value (missing/legacy -> 0.0), never
        # raw/None, so a preflight can read it directly without re-deriving the
        # legacy-default rule itself (see no_go_clearance_m_of).
        "no_go_clearance_m": no_go_clearance_m_of(package),
        "has_survey_graph": bool(package.get("survey_graph")),
        "route_hash": package.get("route_hash", package.get("original_route_hash")),
        "original_route_hash": package.get("original_route_hash"),
        "source": package.get("source"),
    }


def _navigable_geometry_present(package: Optional[dict]) -> bool:
    """True if the package carries a usable navigable region: either the v1
    navigable_geometry (>=1 ring of >=3 vertices) or the legacy
    navigable_boundary single ring."""
    if not package:
        return False
    geom = package.get("navigable_geometry") or []
    for ring in geom:
        if isinstance(ring, list) and len(ring) >= 3:
            return True
    return len(package.get("navigable_boundary") or []) >= 3


# ══════════════════════════════════════════════════════════════════════════════
# Replan planning-package v1: strict acceptance, live-Pixhawk verification,
# atomic envelope storage, and the readiness contract.
# ══════════════════════════════════════════════════════════════════════════════

def _finite(x: Any) -> Optional[float]:
    """`float(x)` iff it is finite (rejects None, non-numeric, NaN, +/-Inf)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _valid_latlon(lat: Optional[float], lon: Optional[float]) -> bool:
    if lat is None or lon is None:
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    if lat == 0.0 and lon == 0.0:
        return False  # ArduPilot "no fix" sentinel, never a real coordinate
    return True


def no_go_clearance_m_of(package: Optional[dict]) -> float:
    """The EFFECTIVE no_go_clearance_m for `package`: a finite, non-negative
    float, defaulting to 0.0 when the field is missing, non-numeric, negative,
    or non-finite. This is the single normalization point every consumer
    (summary, safe_return_planner) uses, so a legacy package stored before this
    field existed is never silently upgraded to the new 5.0 m Operator default
    -- it keeps meaning raw-zone-only exclusion, exactly as it always did."""
    f = _finite((package or {}).get("no_go_clearance_m"))
    return f if (f is not None and f >= 0) else 0.0


def _valid_hash(h: Any) -> bool:
    """A well-formed mission-contract SHA-256 route hash: 'sha256:' + 64 hex."""
    return isinstance(h, str) and bool(_HASH_RE.match(h))


def _wire_pair_latlon(pair: Any) -> "tuple":
    """Interpret a wire [longitude, latitude] pair. Returns (lat, lon, error):
    error is None on success, 'LATLON_ORDER' for a detectable ordering swap
    (the value in the latitude slot is an impossible latitude but the longitude
    slot holds a plausible latitude), or 'INVALID_COORDINATE' otherwise (missing
    element, non-numeric, NaN/Inf, out of range, or null-island)."""
    if not (isinstance(pair, (list, tuple)) and len(pair) >= 2):
        return None, None, "INVALID_COORDINATE"
    lon = _finite(pair[0])
    lat = _finite(pair[1])
    if lon is None or lat is None:
        return None, None, "INVALID_COORDINATE"
    if not (-90.0 <= lat <= 90.0):
        # Detectable swap: the latitude slot is out of range but the longitude
        # slot would be a valid latitude and this a valid longitude.
        if -90.0 <= lon <= 90.0 and -180.0 <= lat <= 180.0:
            return None, None, "LATLON_ORDER"
        return None, None, "INVALID_COORDINATE"
    if not _valid_latlon(lat, lon):
        return None, None, "INVALID_COORDINATE"
    return lat, lon, None


def _ring_latlon(ring: Any) -> "tuple":
    """A wire polygon ring (list of [lon,lat] pairs) -> ([[lat,lon],...], error).
    Requires >=3 valid vertices; propagates a LATLON_ORDER/INVALID_COORDINATE
    from any vertex."""
    if not isinstance(ring, list) or len(ring) < 3:
        return None, "RING_TOO_SMALL"
    out: List[List[float]] = []
    for p in ring:
        lat, lon, err = _wire_pair_latlon(p)
        if err:
            return None, err
        out.append([lat, lon])
    return out, None


def _polygon_latlon(geom: Any) -> "tuple":
    """A wire polygon -> (list-of-rings [[lat,lon],...], error). Accepts either a
    single ring (list of [lon,lat] pairs) or a list of rings. Canonicalizes to
    the internal [lat,lon] convention geo.py / safe_return_planner.py consume."""
    if not isinstance(geom, list) or not geom:
        return None, "EMPTY_GEOMETRY"
    first = geom[0]
    is_multi = isinstance(first, list) and first and isinstance(first[0], (list, tuple))
    rings_wire = geom if is_multi else [geom]
    rings: List[List[List[float]]] = []
    for r in rings_wire:
        ring, err = _ring_latlon(r)
        if err:
            return None, err
        rings.append(ring)
    return rings, None


def _planning_home_latlon(val: Any) -> "tuple":
    """planning_home as a wire [lon,lat] pair OR a {latitude,longitude} dict ->
    (lat, lon, error)."""
    if isinstance(val, dict):
        lat = _finite(val.get("latitude", val.get("lat")))
        lon = _finite(val.get("longitude", val.get("lng", val.get("lon"))))
        if lat is None or lon is None or not _valid_latlon(lat, lon):
            return None, None, "INVALID_HOME"
        return lat, lon, None
    lat, lon, err = _wire_pair_latlon(val)
    if err == "LATLON_ORDER":
        return None, None, "LATLON_ORDER"
    if err or not _valid_latlon(lat, lon):
        return None, None, "INVALID_HOME"
    return lat, lon, None


def validate_package_v1(body: Any, expected_usv_id: Optional[str],
                        now: Optional[float] = None) -> "tuple":
    """
    Strictly validate an Operator-authored replan-planning-package-v1 upload,
    fail-closed. Returns (normalized_package_or_None, error_code, error_message).
    NOTHING is stored here and the live-Pixhawk consistency check is NOT done
    here (see verify_pixhawk_consistency) -- this is the pure, offline structural
    and semantic gate, so it is fully unit-testable without hardware.

    The normalized package carries BOTH the v1 field names and the legacy
    top-level fields (home/route/original_route_hash/navigable_boundary) so every
    existing downstream consumer (safe_return_planner, decision_snapshot,
    mission_execution_controller, replan_controller) keeps working unchanged.
    """
    now = time.time() if now is None else now
    if not isinstance(body, dict):
        return None, "INVALID_REQUEST", "request body must be a JSON object"
    try:
        size = len(json.dumps(body).encode("utf-8"))
    except (TypeError, ValueError):
        return None, "INVALID_REQUEST", "request body is not JSON-serializable"
    if size > MAX_PACKAGE_BYTES:
        return None, "PACKAGE_TOO_LARGE", f"package is {size} bytes; the maximum is {MAX_PACKAGE_BYTES}"

    pv = body.get("package_version")
    if pv not in SUPPORTED_PACKAGE_VERSIONS:
        return None, "UNSUPPORTED_PACKAGE_VERSION", (
            f"package_version {pv!r} is not supported (supported: {', '.join(SUPPORTED_PACKAGE_VERSIONS)})")
    rcv = body.get("route_contract_version")
    if rcv not in SUPPORTED_ROUTE_CONTRACT_VERSIONS:
        return None, "UNSUPPORTED_ROUTE_CONTRACT", (
            f"route_contract_version {rcv!r} is not supported "
            f"(supported: {', '.join(SUPPORTED_ROUTE_CONTRACT_VERSIONS)})")

    mission_id = body.get("mission_id")
    if not mission_id or not isinstance(mission_id, str):
        return None, "MISSING_MISSION_ID", "mission_id is required and must be a non-empty string"

    revision = body.get("mission_revision", body.get("revision"))
    if isinstance(revision, bool) or not isinstance(revision, int):
        return None, "INVALID_REVISION", "mission_revision must be an integer"
    if revision != 0:
        # This surface receives only the immutable original safety envelope.
        # An agent-generated mission revision (>0) is refused here and can never
        # overwrite the original package.
        return None, "IMMUTABLE_ORIGINAL_ONLY", (
            f"mission_revision {revision} is not accepted here; only the immutable "
            f"original package (mission_revision 0) may be received")

    vehicle_id = body.get("vehicle_id", body.get("usv_id"))
    if expected_usv_id is not None and vehicle_id != expected_usv_id:
        return None, "WRONG_TARGET_USV", (
            f"package targets vehicle_id {vehicle_id!r}; this Scout is {expected_usv_id!r}")

    route_hash_val = body.get("route_hash")
    if not _valid_hash(route_hash_val):
        return None, "INVALID_ROUTE_HASH", (
            "route_hash must be a valid mission-contract SHA-256 hash ('sha256:' + 64 hex digits)")

    route_wire = body.get("route_waypoints", body.get("route"))
    if not isinstance(route_wire, list) or not route_wire:
        return None, "EMPTY_ROUTE", "route_waypoints must be a non-empty list of waypoints"
    if len(route_wire) > MAX_ROUTE_WAYPOINTS:
        return None, "ROUTE_TOO_LARGE", (
            f"route has {len(route_wire)} waypoints; the maximum is {MAX_ROUTE_WAYPOINTS}")
    norm_route: List[dict] = []
    for i, wp in enumerate(route_wire):
        if not isinstance(wp, dict):
            return None, "INVALID_WAYPOINT", f"route waypoint {i} is not an object"
        lat = _finite(wp.get("latitude", wp.get("lat")))
        lon = _finite(wp.get("longitude", wp.get("lng", wp.get("lon"))))
        if lat is None or lon is None:
            return None, "INVALID_COORDINATE", (
                f"route waypoint {i} has a non-finite or missing latitude/longitude")
        if not (-90.0 <= lat <= 90.0) and (-90.0 <= lon <= 90.0) and (-180.0 <= lat <= 180.0):
            return None, "LATLON_ORDER", (
                f"route waypoint {i} appears to have latitude/longitude swapped ({lat},{lon})")
        if not _valid_latlon(lat, lon):
            return None, "INVALID_COORDINATE", (
                f"route waypoint {i} has an invalid or zero coordinate ({lat},{lon})")
        seg = wp.get("segment")
        if seg is not None and seg not in VALID_SEGMENTS:
            return None, "UNSUPPORTED_SEGMENT", (
                f"route waypoint {i} has unsupported segment {seg!r} "
                f"(supported: {', '.join(VALID_SEGMENTS)})")
        loiter = _finite(wp.get("loiter_time_s", wp.get("loiter_time", wp.get("loiter", 0))) or 0)
        if loiter is None or loiter < 0:
            return None, "INVALID_WAYPOINT", f"route waypoint {i} has an invalid loiter_time_s"
        norm_route.append({
            "latitude": lat, "longitude": lon, "loiter_time_s": loiter,
            "segment": seg if seg in VALID_SEGMENTS else SEGMENT_UNSPECIFIED,
        })

    recomputed = route_hash.route_content_hash(norm_route)
    if recomputed != route_hash_val:
        return None, "ROUTE_HASH_INCONSISTENT", (
            f"route_hash {route_hash_val} does not match the route_waypoints "
            f"content hash {recomputed}; the package's route and its claimed hash disagree")

    home_lat, home_lon, herr = _planning_home_latlon(body.get("planning_home", body.get("home")))
    if herr == "LATLON_ORDER":
        return None, "LATLON_ORDER", "planning_home appears to have latitude/longitude swapped"
    if herr:
        return None, "INVALID_HOME", (
            "planning_home must be a finite [longitude, latitude] within geographic range "
            "and not null-island")

    if "navigable_geometry" not in body:
        return None, "MISSING_NAVIGABLE_GEOMETRY", (
            "navigable_geometry is required -- it is the preprocessed navigable region "
            "future routes must stay inside")
    nav_rings, nerr = _polygon_latlon(body.get("navigable_geometry"))
    if nerr:
        return None, "INVALID_NAVIGABLE_GEOMETRY", f"navigable_geometry is structurally invalid ({nerr})"

    boundary_rings: List[List[List[float]]] = []
    if body.get("boundary") is not None:
        boundary_rings, berr = _polygon_latlon(body.get("boundary"))
        if berr:
            return None, "INVALID_BOUNDARY", f"boundary is structurally invalid ({berr})"

    # no_go_zones MUST be present. An empty array is valid (checked zero zones);
    # a missing field is NOT equivalent and is refused.
    if "no_go_zones" not in body:
        return None, "MISSING_NO_GO_ZONES", (
            "no_go_zones must be explicitly present; an empty array means checked zero "
            "zones, a missing field is not equivalent")
    zones_wire = body.get("no_go_zones")
    if not isinstance(zones_wire, list):
        return None, "INVALID_NO_GO_ZONE", "no_go_zones must be a list of polygon rings"
    nogo_rings: List[List[List[float]]] = []
    for z in zones_wire:
        ring, zerr = _ring_latlon(z)
        if zerr:
            return None, "INVALID_NO_GO_ZONE", f"no_go_zones contains a structurally invalid zone ({zerr})"
        nogo_rings.append(ring)

    # OPTIONAL outward buffer (metres) beyond no_go_zones a safe return must
    # keep clear of. Additive to the v1 contract -- ABSENT (older Operator
    # packages, or ones predating this field) normalizes to 0.0, the historical
    # raw-zone-only exclusion semantics, NEVER to the new 5.0 m Operator
    # default; see no_go_clearance_m_of, the single normalization point every
    # downstream consumer uses instead of re-deriving this rule.
    ngc = body.get("no_go_clearance_m")
    if ngc is None:
        ngc_f = 0.0
    else:
        ngc_f = _finite(ngc)
        if ngc_f is None or ngc_f < 0:
            return None, "INVALID_NO_GO_CLEARANCE", (
                "no_go_clearance_m must be a finite, non-negative number when present")

    # The approved ROUTE itself must not cross any of its own declared no-go
    # zones, buffered outward by no_go_clearance_m (the same effective
    # exclusion a later safe-return retrace is checked against -- see
    # safe_return_planner.validate_route). Nothing previously checked this
    # here -- only the CONSTRUCTED safe-return retrace route was ever checked
    # against no_go_zones; the original mission's own route was accepted,
    # uploaded, and flown with no such check. A no-go zone has no safe
    # fallback polygon to substitute -- if the approved route enters the
    # buffered exclusion, the mission is unsafe as authored and must be
    # rejected outright, not patched around.
    route_latlon_for_nogo = [(wp["latitude"], wp["longitude"]) for wp in norm_route]
    nogo_crossing = geo.route_crosses_no_go(route_latlon_for_nogo, nogo_rings, ngc_f)
    if nogo_crossing is not None:
        clearance_note = f" (within the required {ngc_f} m clearance)" if ngc_f > 0 else ""
        return None, "ROUTE_CROSSES_NO_GO_ZONE", (
            f"route segment {nogo_crossing} crosses a declared no-go zone{clearance_note}; "
            "the approved route must not enter geometry it also declares off-limits")

    scm = body.get("shoreline_clearance_m")
    if scm is not None:
        scm_f = _finite(scm)
        if scm_f is None or scm_f < 0:
            return None, "INVALID_SHORELINE_CLEARANCE", (
                "shoreline_clearance_m must be a finite, non-negative number when present")
        scm = scm_f

    # OPTIONAL approved Home/launch corridor (task section 4). A single polygon
    # ring that contains the connector from the survey area to a runtime launch
    # Home OUTSIDE the survey navigable geometry, so the safe-return validator can
    # prove that connector instead of failing closed. Backward-compatible: absent
    # on existing packages (safe return then still fails closed for an
    # out-of-boundary Home). The corridor is NOT exempt from no-go constraints.
    home_corridor_ring: List[List[float]] = []
    if body.get("home_corridor") is not None:
        home_corridor_ring, hcerr = _ring_latlon(body.get("home_corridor"))
        if hcerr:
            return None, "INVALID_HOME_CORRIDOR", (
                f"home_corridor must be a structurally valid polygon ring when present ({hcerr})")

    # Route + Home containment (fail closed at acceptance, not at emergency
    # replan). The approved movement region is navigable_geometry ∪
    # home_corridor -- the Operator's own mission-geometry contract. Every
    # approved route SEGMENT (not just waypoints -- geo.route_within_regions
    # requires the whole segment, endpoints and edge, inside at least one
    # region) must lie within that union, and Home must be covered by it too
    # (directly, or via home_corridor).
    #
    # The raw Operator-submitted `boundary` is PROVENANCE ONLY (see the module
    # docstring / `boundary` field): it is never consulted here and never
    # substituted for navigable_geometry as the region safe-return validation
    # uses. A navigable_geometry that does not actually contain the approved
    # route/Home is rejected outright -- it is not rescued by the broader raw
    # survey boundary, and it is not widened. A package that fails this can
    # remain unreadable / not start-eligible; that is the correct, safe
    # outcome, not a bug to route around.
    nav_boundary_latlon = nav_rings[0] if nav_rings else []
    route_latlon = [(wp["latitude"], wp["longitude"]) for wp in norm_route]
    approved_regions = [nav_boundary_latlon, home_corridor_ring]

    route_bad = geo.route_within_regions(route_latlon, approved_regions)
    if route_bad is not None:
        return None, "ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY", (
            f"route segment {route_bad} lies outside the approved geometry "
            "(navigable_geometry / home_corridor); every approved waypoint and "
            "segment must be retraceable within approved geometry for a safe "
            "return to be provable")

    home_in_nav = geo.point_in_polygon((home_lat, home_lon), nav_boundary_latlon)
    home_in_corridor = bool(home_corridor_ring
                            and geo.point_in_polygon((home_lat, home_lon), home_corridor_ring))
    if not home_in_nav and not home_in_corridor:
        return None, "HOME_OUTSIDE_NAVIGABLE_GEOMETRY", (
            "planning_home lies outside navigable_geometry and no home_corridor contains "
            "it; safe-return cannot prove a route back to Home. Supply a home_corridor "
            "that contains Home, or move Home inside navigable_geometry.")

    if body.get("immutable") is not True:
        return None, "NOT_IMMUTABLE", (
            "immutable must be exactly true for the original safety-envelope package")

    segments = body.get("segments")
    if segments is not None and not isinstance(segments, list):
        return None, "INVALID_SEGMENTS", "segments must be a list when present"
    exec_order = body.get("original_execution_order")
    if exec_order is not None and not isinstance(exec_order, list):
        return None, "INVALID_EXECUTION_ORDER", "original_execution_order must be a list when present"

    package = {
        # ── v1 wire fields (verbatim identity/contract) ──
        "package_version": pv,
        "route_contract_version": rcv,
        "mission_id": mission_id,
        "mission_revision": revision,
        "vehicle_id": vehicle_id,
        "route_hash": route_hash_val,
        "planning_home": [home_lon, home_lat],
        "route_waypoints": norm_route,
        "boundary": boundary_rings,
        "navigable_geometry": nav_rings,
        "no_go_zones": nogo_rings,
        "segments": segments or [],
        "original_execution_order": exec_order or [],
        "shoreline_clearance_m": scm,
        "no_go_clearance_m": ngc_f,
        "immutable": True,
        "source": body.get("source", "OPERATOR_STATION"),
        "package_created_at": body.get("created_at"),
        # ── legacy aliases so existing consumers keep working unchanged ──
        "revision": revision,
        "usv_id": vehicle_id,
        "home": {"latitude": home_lat, "longitude": home_lon},
        "route": norm_route,
        "original_route_hash": route_hash_val,
        # ALWAYS navigable_geometry -- never the raw `boundary` (provenance
        # only, see above). safe_return_planner and every other downstream
        # consumer read this field as THE approved navigable region.
        "navigable_boundary": nav_boundary_latlon,
        # OPTIONAL approved Home/launch corridor (task section 4); [] when absent.
        "home_corridor": home_corridor_ring,
        # ── Scout-side provenance ──
        "created_at": round(now, 3),
    }
    return package, None, None


def verify_pixhawk_consistency(package: dict, readback_first: Any,
                               readback_second: Any = None) -> "tuple":
    """
    Independently confirm the accepted package describes the mission actually on
    the Pixhawk, comparing package.route_hash to the canonical route-content hash
    of the live readback (NOT the full mission hash -- sequence 0/Home may change
    legitimately). Returns (ok, error_code, error_message, evidence). Fail
    closed: the package must NOT become usable if the Pixhawk is unreachable, the
    readback is partial, the route hash is unavailable, the hashes differ, the
    route waypoint counts conflict, or the active mission changed during
    validation (detected via a second readback whose route hash differs).
    """
    ev = {
        "package_route_hash": package.get("route_hash"),
        "package_route_waypoint_count": len(package.get("route") or []),
        "reachable": None, "partial": None,
        "pixhawk_route_content_hash": None, "pixhawk_route_waypoint_count": None,
    }
    rb = readback_first
    if not isinstance(rb, dict):
        return False, "PIXHAWK_UNAVAILABLE", "no Pixhawk mission readback available", ev
    ev["reachable"] = rb.get("reachable")
    if rb.get("reachable") is False:
        return False, "PIXHAWK_UNAVAILABLE", f"Pixhawk mission unreachable: {rb.get('error')}", ev
    ev["partial"] = rb.get("partial")
    if rb.get("partial"):
        return False, "PIXHAWK_READBACK_PARTIAL", "Pixhawk mission readback is partial", ev
    if rb.get("mission_valid") is False:
        return False, "PIXHAWK_MISSION_INVALID", "the Pixhawk mission is not valid", ev
    # Freshness gate: mission acceptance must not validate against a stale,
    # still-refreshing, or unattributed readback (see readback_is_fresh / the
    # cache-first GET /agent/pixhawk_mission). Fail closed.
    fresh, fresh_reason = readback_is_fresh(rb)
    if not fresh:
        ev["freshness"] = fresh_reason
        if rb.get("refreshing") or rb.get("busy"):
            code = "PIXHAWK_UNAVAILABLE"          # transient: a download is in flight
        elif rb.get("stale"):
            code = "PIXHAWK_READBACK_STALE"       # the mission moved on
        else:
            code = "PIXHAWK_READBACK_UNVERIFIED"  # missing/unknown proof_source, or too old
        return False, code, fresh_reason, ev
    cur = rb.get("route_content_hash")
    ev["pixhawk_route_content_hash"] = cur
    ev["pixhawk_route_waypoint_count"] = rb.get("route_waypoint_count")
    if not cur:
        # A live-but-degraded readback with no route hash (e.g. Flask reachable
        # but the mission download itself errored) -- fail closed.
        if rb.get("error"):
            return False, "PIXHAWK_UNAVAILABLE", f"Pixhawk mission unreachable: {rb.get('error')}", ev
        return False, "ROUTE_HASH_UNAVAILABLE", "Pixhawk route content hash is unavailable", ev
    if cur != package.get("route_hash"):
        return False, "ROUTE_HASH_MISMATCH", (
            f"package route_hash {package.get('route_hash')} != Pixhawk route hash {cur}"), ev
    pc = rb.get("route_waypoint_count")
    if isinstance(pc, int) and pc != len(package.get("route") or []):
        return False, "ROUTE_COUNT_MISMATCH", (
            f"Pixhawk route waypoint count {pc} != package {len(package.get('route') or [])}"), ev
    if readback_second is not None:
        cur2 = readback_second.get("route_content_hash") if isinstance(readback_second, dict) else None
        ev["pixhawk_route_content_hash_recheck"] = cur2
        if cur2 != cur:
            return False, "ACTIVE_MISSION_CHANGED", (
                "the active Pixhawk mission changed during validation "
                f"({cur} -> {cur2})"), ev
    return True, None, None, ev


def _package_identity(pkg: Optional[dict]) -> Optional[str]:
    """A stable content identity for idempotency -- the mission identity plus the
    normalized safety geometry, excluding Scout-side timestamps/provenance. Two
    POSTs with the same identity are the same package."""
    if not pkg:
        return None
    ident = {
        "package_version": pkg.get("package_version"),
        "mission_id": pkg.get("mission_id"),
        "mission_revision": pkg.get("mission_revision"),
        "route_hash": pkg.get("route_hash"),
        "planning_home": pkg.get("planning_home"),
        "boundary": pkg.get("boundary"),
        "navigable_geometry": pkg.get("navigable_geometry"),
        "no_go_zones": pkg.get("no_go_zones"),
        "shoreline_clearance_m": pkg.get("shoreline_clearance_m"),
        "no_go_clearance_m": pkg.get("no_go_clearance_m"),
    }
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def store_accepted(package: dict, pixhawk_hash: Optional[str],
                   evidence: Optional[dict] = None, now: Optional[float] = None) -> dict:
    """
    Atomically persist a fully validated + Pixhawk-verified package as the store
    envelope, replacing any prior envelope. The write is a single tmp+os.replace
    so a concurrent GET can never observe a half-written package. Idempotent: a
    repeat of the identical package keeps the same generation and received_at
    (only validated_at / pixhawk_hash_used / evidence refresh); a genuinely
    different verified original bumps the generation counter and resets the
    revision history. Returns the stored envelope.
    """
    now = time.time() if now is None else now
    with _lock:
        prev = _read_raw_nolock()
        prev_env = prev if _is_envelope(prev) else None
        ident = _package_identity(package)
        idempotent = False
        generation = 1
        received_at = round(now, 3)
        if prev_env is not None:
            if _package_identity(prev_env.get("original_package")) == ident:
                idempotent = True
                generation = prev_env.get("generation") or 1
                received_at = prev_env.get("received_at", received_at)
            else:
                generation = (prev_env.get("generation") or 0) + 1
        env = {
            "store_version": STORE_VERSION,
            "generation": generation,
            "received_at": received_at,
            "validated_at": round(now, 3),
            "pixhawk_hash_used": pixhawk_hash,
            "validation": {
                "valid": True, "code": None, "message": None,
                "evidence": evidence or {},
            },
            # The immutable original safety envelope (revision 0). Kept SEPARATE
            # from the active revision so later revision-executing code never has
            # to overwrite it.
            "original_package": package,
            # The currently active revision. For v1 this IS the original; the
            # slot exists so a future revision can diverge without a rewrite.
            "active_package": package,
            "active_package_revision": package.get("mission_revision", 0),
            # Derived revision history (revision-executing code will append).
            "revision_history": [],
            "package_identity": ident,
            "idempotent": idempotent,
        }
        _write_raw_nolock(env)
        return env


def load_envelope() -> Optional[dict]:
    """The full store envelope (provenance + original/active/history), or None if
    nothing is stored. A legacy flat package is wrapped in a synthesized envelope
    (store_version None) so callers can read it uniformly."""
    with _lock:
        raw = _read_raw_nolock()
    if raw is None:
        return None
    if _is_envelope(raw):
        return raw
    return {
        "store_version": None, "generation": None,
        "received_at": None, "validated_at": None, "pixhawk_hash_used": None,
        "validation": {"valid": is_usable(raw), "code": None, "message": None, "evidence": {}},
        "original_package": raw, "active_package": raw,
        "active_package_revision": raw.get("mission_revision", raw.get("revision", 0)),
        "revision_history": [], "package_identity": _package_identity(raw),
        "idempotent": False,
    }


def load_original() -> Optional[dict]:
    """The immutable original (revision 0) safety-envelope package, distinct from
    the active revision. For v1 they coincide."""
    env = load_envelope()
    return env.get("original_package") if env else None


def _v1_structurally_ready(pkg: Optional[dict]) -> bool:
    """The stricter 'package usable for replanning' gate (beyond the safe-return
    is_usable floor): a usable Home+route PLUS the safety envelope the replanner
    needs -- navigable geometry present, no-go zones explicitly present, and the
    immutable flag set."""
    if not is_usable(pkg):
        return False
    if not _navigable_geometry_present(pkg):
        return False
    if pkg.get("no_go_zones") is None:  # explicit empty list is fine, missing is not
        return False
    if pkg.get("immutable") is not True:
        return False
    return True


# ── Last-successful readiness proof retention ─────────────────────────────────
# A refresh in progress or an expired cache must NOT erase the evidence that
# replanning was proven consistent. build_readiness records the last COMPLETED,
# FRESH matching proof here and echoes it on every subsequent call (including
# while refreshing / proof-stale), so the surface can distinguish "checking"
# from a genuine package inconsistency. Only a genuine, freshly-proven mismatch
# (or a missing package) erases it. In-process runtime state (like the runtime
# config override layer above); a restart simply re-proves it.
_proof_lock = threading.Lock()
_last_verified_proof: Optional[Dict[str, Any]] = None


def _record_verified_proof(generation, mission_id, route_hash_val, route_count,
                           now: float) -> None:
    global _last_verified_proof
    with _proof_lock:
        _last_verified_proof = {
            "last_verified_generation": generation,
            "last_verified_at": round(now, 3),
            "last_verified_mission_id": mission_id,
            "last_verified_route_hash": route_hash_val,
            "last_verified_route_count": route_count,
        }


def last_verified_proof() -> Optional[Dict[str, Any]]:
    """The last COMPLETED, FRESH matching readiness proof, or None. Preserved
    across refresh-in-progress and proof-stale windows; erased only by a
    genuine freshly-proven mismatch or a missing package."""
    with _proof_lock:
        return dict(_last_verified_proof) if _last_verified_proof else None


def _clear_verified_proof() -> None:
    global _last_verified_proof
    with _proof_lock:
        _last_verified_proof = None


def build_readiness(pixhawk_readback: Any = None, now: Optional[float] = None) -> dict:
    """
    The replanning readiness contract. replanning_ready is True ONLY when the
    mission is verified, the package is stored, structurally usable, its route
    hash matches the CURRENT live Pixhawk route, navigable geometry and no-go
    zones are present-and-checked, and (where the readback exposes it) the
    mission id is consistent.

    Otherwise `state` distinguishes exactly why, keeping the FRESHNESS axis
    strictly separate from the CONSISTENCY axis:

      * missing / structurally-invalid          -- the stored package itself;
      * PIXHAWK_UNAVAILABLE                      -- no reachable/complete readback;
      * REPLANNING_READINESS_REFRESHING          -- a refresh/download is in
        flight (transient; we simply do not have fresh evidence THIS instant);
      * REPLANNING_PROOF_STALE                   -- the cached readback expired or
        carries no recognized proof_source (again, no fresh evidence -- NOT a
        package problem);
      * HASH_COMPARISON_UNAVAILABLE              -- fresh readback, but no route
        hash to compare against;
      * PLANNING_PACKAGE_STALE                   -- a COMPLETED, FRESH proof shows
        a genuine mismatch (route-hash / waypoint-count / mission-id) or an
        invalid Pixhawk mission. This bucket is reserved for that case ALONE.

    The last COMPLETED, FRESH matching proof is retained (`last_verified`) and
    is NOT erased by a refresh-in-progress or an expired cache -- only a genuine
    freshly-proven mismatch or a missing package clears it. connector_proven_safe
    is never asserted here -- it is reported null (not evaluated), never true,
    because acceptance of the original mission is not evidence of proven-safe
    connectivity.
    """
    now = time.time() if now is None else now
    env = load_envelope()
    pkg = load()
    r = {
        "replanning_ready": False,
        "state": READY_MISSING,
        "mission_verified": None,
        "package_stored": pkg is not None,
        "package_usable": False,
        "mission_id_consistent": None,
        "route_hash_match": None,
        "navigable_geometry_checked": None,
        "no_go_zones_checked": None,
        "connector_proven_safe": None,  # never asserted here; see docstring
        "generation": (env or {}).get("generation"),
        "pixhawk": {"reachable": None, "route_content_hash": None,
                    "route_waypoint_count": None, "partial": None, "error": None},
        "package": summary(pkg),
        "evidence": {},
        # Retained last-successful proof; preserved through refresh/proof-stale.
        "last_verified": last_verified_proof(),
    }
    if pkg is None:
        _clear_verified_proof()
        r["state"] = READY_MISSING
        r["last_verified"] = None
        return r

    env_valid = bool((env or {}).get("validation", {}).get("valid"))
    r["navigable_geometry_checked"] = _navigable_geometry_present(pkg)
    r["no_go_zones_checked"] = pkg.get("no_go_zones") is not None
    if not env_valid or not _v1_structurally_ready(pkg):
        r["state"] = READY_INVALID
        return r
    r["package_usable"] = True

    rb = pixhawk_readback
    if not isinstance(rb, dict):
        r["state"] = READY_PIXHAWK_UNAVAILABLE
        return r
    r["pixhawk"].update({
        "reachable": rb.get("reachable"),
        "route_content_hash": rb.get("route_content_hash"),
        "route_waypoint_count": rb.get("route_waypoint_count"),
        "partial": rb.get("partial"),
        "error": rb.get("error"),
    })
    if rb.get("reachable") is False:
        r["state"] = READY_PIXHAWK_UNAVAILABLE
        return r
    if rb.get("partial"):
        # An incomplete readback is not a COMPLETED proof -- it is an availability
        # gap, never evidence of a package inconsistency. Retain the last proof.
        r["state"] = READY_PIXHAWK_UNAVAILABLE
        r["evidence"]["partial"] = True
        return r

    # ── Freshness axis (STRICTLY separate from consistency) ──────────────────
    # A stale, expired, still-refreshing, or unattributed readback only means we
    # do not hold fresh evidence THIS instant. It must NEVER be reported as a
    # package inconsistency, and it must NEVER erase the retained proof. A
    # refresh in flight -> REFRESHING; anything else unfresh -> PROOF_STALE.
    fresh, fresh_reason = readback_is_fresh(rb, now=now)
    if not fresh:
        r["evidence"]["freshness"] = fresh_reason
        r["state"] = READY_REFRESHING if (rb.get("refreshing") or rb.get("busy")) else READY_PROOF_STALE
        return r

    # ── Consistency axis: from here the readback is a COMPLETED, FRESH proof, so
    #    (and only so) a genuine mismatch may be classified PLANNING_PACKAGE_STALE.
    cur = rb.get("route_content_hash")
    if not cur:
        # Fresh but nothing to compare against -- cannot compare, not a mismatch.
        r["state"] = READY_HASH_UNAVAILABLE
        return r

    pkg_hash = pkg.get("route_hash") or pkg.get("original_route_hash")
    if cur != pkg_hash:
        r["route_hash_match"] = False
        r["state"] = READY_PACKAGE_STALE
        r["evidence"]["route_hash_mismatch"] = {"pixhawk": cur, "package": pkg_hash}
        _clear_verified_proof()
        r["last_verified"] = None
        return r
    r["route_hash_match"] = True

    pc = rb.get("route_waypoint_count")
    if isinstance(pc, int) and pc != len(pkg.get("route") or []):
        r["state"] = READY_PACKAGE_STALE
        r["evidence"]["route_count_mismatch"] = {"pixhawk": pc, "package": len(pkg.get("route") or [])}
        _clear_verified_proof()
        r["last_verified"] = None
        return r

    r["mission_verified"] = bool(rb.get("mission_valid"))
    if not r["mission_verified"]:
        r["state"] = READY_PACKAGE_STALE  # a fresh proof shows the vehicle mission is invalid
        _clear_verified_proof()
        r["last_verified"] = None
        return r

    rb_mid = rb.get("mission_id")
    if rb_mid is not None:
        r["mission_id_consistent"] = (rb_mid == pkg.get("mission_id"))
        if not r["mission_id_consistent"]:
            r["state"] = READY_PACKAGE_STALE
            _clear_verified_proof()
            r["last_verified"] = None
            return r

    # Proven consistent against a fresh, complete readback: READY. Record/refresh
    # the retained proof so a later refresh/expiry window keeps this evidence.
    _record_verified_proof(
        generation=(env or {}).get("generation"),
        mission_id=pkg.get("mission_id"),
        route_hash_val=pkg_hash,
        route_count=len(pkg.get("route") or []),
        now=now,
    )
    r["last_verified"] = last_verified_proof()
    r["state"] = READY_USABLE
    r["replanning_ready"] = True
    return r


def _reset_for_tests(path: Optional[str] = None) -> None:
    global _FILE
    _clear_verified_proof()
    with _lock:
        if path is not None:
            _FILE = path
        try:
            os.remove(_FILE)
        except OSError:
            pass
