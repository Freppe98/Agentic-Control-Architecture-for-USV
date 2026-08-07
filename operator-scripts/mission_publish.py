"""ONE publish transaction for a planned mission: Pixhawk → Operator record → Scout package.

WHY THIS MODULE EXISTS
----------------------
Finalizing a survey used to end at the flight controller. `POST /api/missions/finalize` stored
the immutable revision-0 record, queued the verified MISSION_UPLOAD, and that was the whole
"upload". The Plan page then rendered "Uploaded & verified" the moment the command's read-back
verification landed — and nothing, anywhere in the station, ever sent the matching planning
package to Scout. `syncReplanPackage` existed in the API client with zero call sites; the only
package button in the UI (Agent page) POSTed the OLDER pre-v1 package shape. So Scout kept the
package from the PREVIOUS mission, and the next Start correctly refused with a mission/package
mismatch that only a manual `curl .../replan/planning-package/sync` could clear.

That is a publication transaction that stops two thirds of the way through and reports success.
This module is the whole transaction, in one place:

    VALIDATING_PLAN              the mission record exists, belongs to this vehicle, is intact
    UPLOADING_PIXHAWK            the MISSION_UPLOAD command reached a verified read-back
    VERIFYING_PIXHAWK            a LIVE, complete read-back whose route hash/count is the approved one
    PERSISTING_OPERATOR_MISSION  the durable store's ACTIVE mission is that same mission
    BUILDING_PLANNING_PACKAGE    replan-planning-package-v1 from that record (fails closed)
    SYNCING_SCOUT_PACKAGE        one POST to Scout's single package slot
    VERIFYING_SCOUT_PACKAGE      read the package BACK and prove id == hash == count
    READY                        all three copies aligned; and only now is Agent-ready true

WHAT IT DELIBERATELY IS NOT
---------------------------
NOT A SECOND HASH IMPLEMENTATION. Every identity here is the canonical mission-contract-v1
route hash (`mission_contract.route_content_hash`) as already stored on the record and already
verified against the flight controller. This module compares values; it never mints one.

NOT A BLOCKING UPLOAD. The Pixhawk write is the station's at-least-once command queue: finalize
QUEUEs a MISSION_UPLOAD, Scout claims it, executes it, and posts a verified result. A publish
operation that blocked until that round trip completed would hold an HTTP worker for the length
of a MAVLink mission upload and would lose the queue's redelivery semantics. So publish is
RESUMABLE and IDEMPOTENT instead: while the command is in flight it answers phase
UPLOADING_PIXHAWK with `pending:true` — an honest "not finished", never a failure — and the
caller invokes it again when the command reaches its verified read-back. Re-running it after
READY re-proves everything and returns READY again.

NOT A RE-UPLOAD PATH. This module issues NO vehicle command of any kind. It cannot arm, change
mode, upload, clear or execute anything. It reads the flight controller back, and it writes one
package to Scout. That is what makes the retry action provably safe: retrying package sync
cannot touch the Pixhawk, because there is no code here that could.

FAILURE SEMANTICS: a verified Pixhawk mission is NEVER rolled back because Scout was
unreachable. The record stays, the active mission stays, and the mission is marked
PACKAGE_SYNC_REQUIRED — durably, so a backend restart restores the fact that a sync is owed
rather than presenting an unsynced mission as ready.

DEPENDENCY INJECTION: everything the transaction touches in the operator backend arrives as a
`Deps` of callables, so the whole layer is unit-testable without a FastAPI app and cannot reach
around into main.py.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import mission_contract
import replan_package
import scout_replan


# ── Phases ────────────────────────────────────────────────────────────────────────────────
# Named exactly as the operator sees them. A response carries the phases that actually RAN, in
# order, each with its own status — so "which step stopped" is data, never inferred from prose.
PHASE_VALIDATING_PLAN = "VALIDATING_PLAN"
PHASE_UPLOADING_PIXHAWK = "UPLOADING_PIXHAWK"
PHASE_VERIFYING_PIXHAWK = "VERIFYING_PIXHAWK"
PHASE_PERSISTING_OPERATOR_MISSION = "PERSISTING_OPERATOR_MISSION"
PHASE_BUILDING_PLANNING_PACKAGE = "BUILDING_PLANNING_PACKAGE"
PHASE_SYNCING_SCOUT_PACKAGE = "SYNCING_SCOUT_PACKAGE"
PHASE_VERIFYING_SCOUT_PACKAGE = "VERIFYING_SCOUT_PACKAGE"
PHASE_READY = "READY"

PHASE_ORDER = (
    PHASE_VALIDATING_PLAN, PHASE_UPLOADING_PIXHAWK, PHASE_VERIFYING_PIXHAWK,
    PHASE_PERSISTING_OPERATOR_MISSION, PHASE_BUILDING_PLANNING_PACKAGE,
    PHASE_SYNCING_SCOUT_PACKAGE, PHASE_VERIFYING_SCOUT_PACKAGE, PHASE_READY,
)

# Phase statuses.
OK = "ok"
FAILED = "failed"
PENDING = "pending"        # not finished, not failed — the honest third answer
SKIPPED = "skipped"


# ── Error codes ───────────────────────────────────────────────────────────────────────────
# SPECIFIC by design. The single generic "the planning package is not consistent with the
# approved mission" is what made a refreshing read, an unreachable Scout and a genuine hash
# disagreement all look identical to the operator. Each of these names one cause and one fix.
NO_MISSION_RECORD = "NO_MISSION_RECORD"
MISSION_BELONGS_TO_ANOTHER_VEHICLE = "MISSION_BELONGS_TO_ANOTHER_VEHICLE"
MISSION_ID_MISMATCH = "MISSION_ID_MISMATCH"
MISSION_RECORD_ALTERED = "MISSION_RECORD_ALTERED"
PIXHAWK_UPLOAD_PENDING = "PIXHAWK_UPLOAD_PENDING"
PIXHAWK_UPLOAD_FAILED = "PIXHAWK_UPLOAD_FAILED"
PIXHAWK_READBACK_UNREACHABLE = "PIXHAWK_READBACK_UNREACHABLE"
PIXHAWK_READBACK_PARTIAL = "PIXHAWK_READBACK_PARTIAL"
PIXHAWK_READBACK_HASH_UNAVAILABLE = "PIXHAWK_READBACK_HASH_UNAVAILABLE"
PIXHAWK_HASH_MISMATCH = "PIXHAWK_HASH_MISMATCH"
PIXHAWK_COUNT_MISMATCH = "PIXHAWK_COUNT_MISMATCH"
OPERATOR_PERSIST_FAILED = "OPERATOR_PERSIST_FAILED"
PACKAGE_BUILD_FAILED = "PACKAGE_BUILD_FAILED"
SCOUT_UNREACHABLE = "SCOUT_UNREACHABLE"
SCOUT_PACKAGE_POST_FAILED = "SCOUT_PACKAGE_POST_FAILED"
SCOUT_PACKAGE_READBACK_FAILED = "SCOUT_PACKAGE_READBACK_FAILED"
SCOUT_PACKAGE_ID_MISMATCH = "SCOUT_PACKAGE_ID_MISMATCH"
SCOUT_PACKAGE_HASH_MISMATCH = "SCOUT_PACKAGE_HASH_MISMATCH"
SCOUT_PACKAGE_COUNT_MISMATCH = "SCOUT_PACKAGE_COUNT_MISMATCH"
SCOUT_PACKAGE_NOT_STORED = "SCOUT_PACKAGE_NOT_STORED"
PACKAGE_SYNC_REQUIRED = "PACKAGE_SYNC_REQUIRED"
PUBLISH_BUSY = "PUBLISH_BUSY"

# ── Terminal states the whole station reasons with ────────────────────────────────────────
# Map, Agent and Plan all render from THIS vocabulary, so a mission cannot read READY on one
# page and mismatched on another.
STATE_READY = "READY"                                    # all three copies aligned
STATE_VERIFYING = "VERIFYING"                            # a read is in flight / evidence incomplete
STATE_UPLOAD_IN_PROGRESS = "UPLOAD_IN_PROGRESS"          # the Pixhawk write has not finished
STATE_PACKAGE_SYNC_REQUIRED = "PACKAGE_SYNC_REQUIRED"    # Pixhawk verified, Scout package is not
STATE_SCOUT_UNREACHABLE = "SCOUT_UNREACHABLE"            # cannot ask Scout — not a mismatch
STATE_REAL_MISMATCH = "REAL_MISMATCH"                    # proven disagreement, evidence complete
STATE_BLOCKED = "BLOCKED"                                # refused before anything was sent
STATE_BUSY = "BUSY"                                      # another publish holds this vehicle

# Which record `package_sync_state` values are durable. Persisted on the mission record so a
# backend restart restores "a sync is owed" instead of presenting an unsynced mission as ready.
SYNC_STATE_REQUIRED = "REQUIRED"
SYNC_STATE_SYNCED = "SYNCED"

# Operations, for the trace.
OPERATION_PUBLISH = "publish"
OPERATION_PACKAGE_SYNC = "package_sync"


class Busy(RuntimeError):
    """Another publish transaction holds this vehicle. The caller answers BUSY rather than
    letting two publications interleave over one vehicle's single package slot."""


# ── Per-vehicle serialization ─────────────────────────────────────────────────────────────
# Publish reads the flight controller, writes Scout's SINGLE package slot and updates the
# durable record. Two concurrent publishes for one vehicle could interleave into a package that
# describes neither mission. FastAPI runs sync route handlers in a worker threadpool, so this is
# a plain non-reentrant lock per vehicle, acquired NON-BLOCKING: a second request is told BUSY
# immediately rather than queueing behind a MAVLink download.
_vehicle_locks: dict = {}
_locks_guard = threading.Lock()


def _vehicle_lock(vid):
    with _locks_guard:
        lock = _vehicle_locks.get(vid)
        if lock is None:
            lock = _vehicle_locks[vid] = threading.Lock()
        return lock


class vehicle_publish_lock:
    """Context manager holding one vehicle's publish lock, or raising Busy at once."""

    def __init__(self, vid):
        self._lock = _vehicle_lock(vid)
        self._held = False

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            raise Busy("A publish operation is already running for this vehicle.")
        self._held = True
        return self

    def __exit__(self, *exc):
        if self._held:
            self._lock.release()
            self._held = False
        return False


def is_publishing(vid):
    """True while a publish transaction holds this vehicle. Read-only; never acquires."""
    lock = _vehicle_lock(vid)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


class Deps:
    """Everything the transaction needs from the operator backend, injected.

    active_mission_id(vid)      -> the vehicle's ACTIVE PERSISTED original mission id, or None
    mission_record(mid)         -> the immutable revision-0 record dict, or None
    pixhawk_readback(vid)       -> a LIVE (uncached) normalized read-back dict for this vehicle,
                                   or None when the vehicle has no Flask (8080) API configured
    scout_get_package(base)     -> scout_replan.get_planning_package result
    scout_post_package(base, p) -> scout_replan.post_planning_package result
    scout_package_evidence(body)-> the ONE normalizer over Scout's package GET body
                                   (main._normalize_scout_package) — nested v1 and flat pre-v1
    readiness(vid, base)        -> the combined readiness summary, for the response bundle
    persist_sync_state(record)  -> durably save the record after its package_sync_state changed;
                                   returns True on success, False when the snapshot could not
                                   be written (reported, never silently swallowed)
    record_operation(entry)     -> append one entry to the publish trace (diagnostics)
    """

    def __init__(self, *, active_mission_id, mission_record, pixhawk_readback,
                 scout_get_package, scout_post_package, scout_package_evidence,
                 readiness, persist_sync_state, record_operation):
        self.active_mission_id = active_mission_id
        self.mission_record = mission_record
        self.pixhawk_readback = pixhawk_readback
        self.scout_get_package = scout_get_package
        self.scout_post_package = scout_post_package
        self.scout_package_evidence = scout_package_evidence
        self.readiness = readiness
        self.persist_sync_state = persist_sync_state
        self.record_operation = record_operation


# ── small helpers ─────────────────────────────────────────────────────────────────────────
def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _as_int(value):
    """An int, or None. A bool is never a count."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def route_count_from_readback(readback, *, expected=None):
    """(route_waypoint_count, source) from a Pixhawk read-back, applying the mission-contract
    rule that item 0 is Home.

    Scout reports `route_waypoint_count` and `pixhawk_item_count` EXPLICITLY under
    mission-contract-v1, and that explicit count is always preferred — it is Scout's own
    statement about the route, derived on the vehicle side.

    Only when Scout supplies no explicit route count do we fall back to the raw item list, and
    then the Home rule is applied rather than assumed: a raw count of `expected + 1` is Home
    plus the route (the normal case), a raw count of exactly `expected` is a mission with no
    Home item. Any other raw count is returned AS-IS with source `raw_items` so the caller sees
    a genuine disagreement instead of a number massaged into agreement.

    Returns (None, None) when nothing usable is present — an UNKNOWN count, never a guessed one.
    In particular, a read-back that reports neither contract count nor a single mission item
    yields None rather than 0: "no items arrived" is an absence of evidence about the route, and
    turning it into "the route has zero waypoints" would fabricate an observation. The route
    HASH is the proof that carries in that case; an unknown count is reported as unknown.
    """
    if not isinstance(readback, dict):
        return None, None
    explicit = _as_int(readback.get("route_waypoint_count"))
    if explicit is not None:
        return explicit, "scout_route_waypoint_count"
    raw = _as_int(readback.get("pixhawk_item_count"))
    if raw is None:
        items = readback.get("waypoints")
        if isinstance(items, list) and items:
            reported = _as_int(readback.get("raw_count"))
            raw = reported if reported is not None else len(items)
    if raw is None:
        return None, None
    if expected is not None:
        if raw == expected + 1:
            return raw - 1, "raw_items_minus_home"
        if raw == expected:
            return raw, "raw_items_no_home"
    return raw, "raw_items"


def _phase(name, status, detail=None, code=None, **extra):
    out = {"phase": name, "status": status, "detail": detail, "code": code}
    out.update(extra)
    return out


def _envelope(operation, slug, mission_id=None):
    return {
        "ok": False,
        "operation": operation,
        "vehicle_id": slug,
        "mission_id": mission_id,
        "phase": PHASE_VALIDATING_PLAN,
        "state": STATE_BLOCKED,
        "error": None,
        "message": None,
        "idempotent": False,
        "generated_at": _now_iso(),
        "expected_route_hash": None,
        "expected_route_count": None,
        "phases": [],
        "pixhawk": None,
        "operator_store": None,
        "scout": None,
        "final": {"mission_id_match": None, "hash_match": None, "count_match": None,
                  "agent_ready": False},
    }


def _stop(env, phase, code, message, state, **extra):
    """End the transaction at `phase`. Everything gathered so far is preserved — the operator
    needs the evidence up to the refusal, not only its name."""
    env["phase"] = phase
    env["error"] = code
    env["message"] = message
    env["state"] = state
    env["ok"] = False
    env["phases"].append(_phase(phase, FAILED, message, code, **extra))
    return env


def _pending(env, phase, code, message, state, **extra):
    """End the transaction at `phase` because it is NOT FINISHED — distinct from failed. A
    queued Pixhawk upload is the system doing what was asked, and must never render as an error."""
    env["phase"] = phase
    env["error"] = code
    env["message"] = message
    env["state"] = state
    env["ok"] = False
    env["phases"].append(_phase(phase, PENDING, message, code, **extra))
    return env


# ── The transaction ───────────────────────────────────────────────────────────────────────
def run_publish(deps, vid, base, slug, *, mission_id=None, package_only=False):
    """Complete the publication of a planned mission and return the phase-by-phase evidence.

    `base` is the vehicle's Local Agent (8090) base URL. `mission_id` is OPTIONAL and never
    trusted over the durable store: it must name the vehicle's ACTIVE mission (ownership is
    checked first, so a mission belonging to another vehicle is reported as exactly that).

    `package_only=True` is the RETRY path. It changes nothing about what is verified — the same
    live read-back, the same hash and count proof — only how the UPLOADING_PIXHAWK phase is
    reported: a retry is invoked against an already-verified upload, so an unverified record is
    a refusal there rather than a "still uploading". Neither mode issues a vehicle command.

    Issues no vehicle command in either mode. The only write it performs is ONE POST of the
    planning package to Scout's single slot.
    """
    operation = OPERATION_PACKAGE_SYNC if package_only else OPERATION_PUBLISH
    env = _envelope(operation, slug)
    try:
        with vehicle_publish_lock(vid):
            out = _run_locked(deps, vid, base, slug, env, mission_id=mission_id,
                              package_only=package_only)
    except Busy as exc:
        env["phase"] = PHASE_VALIDATING_PLAN
        env["error"] = PUBLISH_BUSY
        env["message"] = str(exc)
        env["state"] = STATE_BUSY
        env["phases"].append(_phase(PHASE_VALIDATING_PLAN, FAILED, str(exc), PUBLISH_BUSY))
        return env
    if deps.record_operation:
        deps.record_operation(_trace_entry(out))
    return out


def _trace_entry(env):
    """The one-line record of a publish attempt kept for the diagnostics endpoint."""
    return {
        "at": env.get("generated_at"),
        "operation": env.get("operation"),
        "vehicle_id": env.get("vehicle_id"),
        "mission_id": env.get("mission_id"),
        "phase": env.get("phase"),
        "state": env.get("state"),
        "error": env.get("error"),
        "agent_ready": bool((env.get("final") or {}).get("agent_ready")),
        "idempotent": bool(env.get("idempotent")),
    }


def _run_locked(deps, vid, base, slug, env, *, mission_id, package_only):
    # ── VALIDATING_PLAN ───────────────────────────────────────────────────────────────────
    # Resolve the mission the DURABLE STORE says is active. Ownership is checked before the
    # active-mission comparison so a mission approved for another vehicle is named as that,
    # which is the more specific and more useful refusal.
    active_id = deps.active_mission_id(vid)
    supplied = mission_id.strip() if isinstance(mission_id, str) and mission_id.strip() else None
    resolved = supplied or active_id
    env["mission_id"] = resolved

    rec = deps.mission_record(resolved) if resolved else None
    if rec is None:
        return _stop(env, PHASE_VALIDATING_PLAN, NO_MISSION_RECORD,
                     "No immutable original mission record for this vehicle — finalize a "
                     "survey first.", STATE_BLOCKED)
    if rec.get("vehicle_id") != vid:
        return _stop(env, PHASE_VALIDATING_PLAN, MISSION_BELONGS_TO_ANOTHER_VEHICLE,
                     "That mission was approved for a different vehicle and will not be "
                     "published here.", STATE_BLOCKED,
                     mission_vehicle_id=rec.get("vehicle_id"))
    if supplied and active_id and supplied != active_id:
        # The durable active record WINS. A caller must never be able to publish an older
        # mission over the one the operator most recently approved — this is the guard that
        # makes "a stale older mission is never selected for package sync" structural.
        return _stop(env, PHASE_VALIDATING_PLAN, MISSION_ID_MISMATCH,
                     f"{supplied} is not this vehicle's active mission ({active_id}). The "
                     f"Operator publishes the active persisted mission only.", STATE_BLOCKED,
                     active_mission_id=active_id)

    expected_hash = rec.get("route_hash")
    route_waypoints = rec.get("route_waypoints") or []
    expected_count = len(route_waypoints)
    env["expected_route_hash"] = expected_hash
    env["expected_route_count"] = expected_count

    # Tamper check on the record itself, through the SAME calculator that produced its hash.
    if not expected_hash or expected_hash != mission_contract.route_content_hash(route_waypoints):
        return _stop(env, PHASE_VALIDATING_PLAN, MISSION_RECORD_ALTERED,
                     "The stored mission record's route hash does not describe its own route "
                     "waypoints — refusing to publish an altered mission.", STATE_REAL_MISMATCH)
    env["phases"].append(_phase(PHASE_VALIDATING_PLAN, OK,
                                f"{expected_count} route waypoints, hash {expected_hash}"))

    # ── UPLOADING_PIXHAWK ─────────────────────────────────────────────────────────────────
    # This module does not upload. The MISSION_UPLOAD command queue does, and its verified
    # read-back is projected onto the record's upload_status. So this phase READS that status.
    upload_status = rec.get("upload_status")
    if upload_status == "FAILED":
        return _stop(env, PHASE_UPLOADING_PIXHAWK, PIXHAWK_UPLOAD_FAILED,
                     rec.get("upload_failure_reason")
                     or "The mission upload to the flight controller failed.",
                     STATE_BLOCKED, upload_status=upload_status)
    if upload_status != "VERIFIED":
        message = (f"Mission upload_status is {upload_status!r} — only a VERIFIED mission may "
                   f"be synchronized as a planning package."
                   if package_only else
                   "The mission upload to the flight controller has not been verified yet.")
        state = STATE_BLOCKED if package_only else STATE_UPLOAD_IN_PROGRESS
        return _pending(env, PHASE_UPLOADING_PIXHAWK, PIXHAWK_UPLOAD_PENDING, message, state,
                        upload_status=upload_status)
    env["phases"].append(_phase(PHASE_UPLOADING_PIXHAWK, OK,
                                "MISSION_UPLOAD verified by Scout's read-back"))

    # ── VERIFYING_PIXHAWK ─────────────────────────────────────────────────────────────────
    # A LIVE download, never the polling cache: this decides whether a package may be sent, and
    # a ten-second-old hash is evidence about the past.
    readback = deps.pixhawk_readback(vid)
    if readback is None:
        env["pixhawk"] = _pixhawk_block(None, expected_hash, expected_count)
        return _stop(env, PHASE_VERIFYING_PIXHAWK, PIXHAWK_READBACK_UNREACHABLE,
                     "No vehicle Flask API (port 8080) is configured — the read-back that "
                     "proves the flight controller carries this route cannot be performed.",
                     STATE_SCOUT_UNREACHABLE)
    pix = _pixhawk_block(readback, expected_hash, expected_count)
    env["pixhawk"] = pix
    # The raw normalized read-back is echoed too: the caller brackets the Scout write with a
    # second live read, and the two are only comparable if both are the same shape.
    env["pixhawk_readback"] = readback

    if not pix["reachable"]:
        return _stop(env, PHASE_VERIFYING_PIXHAWK, PIXHAWK_READBACK_UNREACHABLE,
                     "The Pixhawk mission read-back is unreachable — refusing to publish a "
                     "mission whose route cannot be confirmed against the flight controller.",
                     STATE_SCOUT_UNREACHABLE)
    if not pix["complete"]:
        return _stop(env, PHASE_VERIFYING_PIXHAWK, PIXHAWK_READBACK_PARTIAL,
                     "The Pixhawk mission read-back is PARTIAL — an incomplete download proves "
                     "nothing about the route on the flight controller.", STATE_VERIFYING)
    if not pix["route_hash"]:
        return _stop(env, PHASE_VERIFYING_PIXHAWK, PIXHAWK_READBACK_HASH_UNAVAILABLE,
                     "The read-back reported no route_content_hash — the content comparison "
                     "that proves the route is unavailable, so publication fails closed.",
                     STATE_VERIFYING)
    if pix["route_hash"] != expected_hash:
        return _stop(env, PHASE_VERIFYING_PIXHAWK, PIXHAWK_HASH_MISMATCH,
                     "The route on the flight controller does NOT match the approved mission "
                     "route.", STATE_REAL_MISMATCH)
    if pix["route_waypoint_count"] is not None and pix["route_waypoint_count"] != expected_count:
        return _stop(env, PHASE_VERIFYING_PIXHAWK, PIXHAWK_COUNT_MISMATCH,
                     f"The flight controller reports {pix['route_waypoint_count']} route "
                     f"waypoints; the approved mission has {expected_count}.",
                     STATE_REAL_MISMATCH)
    env["phases"].append(_phase(PHASE_VERIFYING_PIXHAWK, OK,
                                f"live read-back: {pix['route_waypoint_count']} route waypoints "
                                f"({pix['raw_item_count']} Pixhawk items), hash matches"))

    # ── PERSISTING_OPERATOR_MISSION ───────────────────────────────────────────────────────
    # The record was written by finalize; this phase READS THE STORE BACK and proves that the
    # mission we just verified on the flight controller is the one the store calls active. That
    # is the check a stale in-memory active mission would fail.
    store = {
        "record_present": True,
        "active_mission_id": active_id,
        "upload_status": upload_status,
        "route_hash": expected_hash,
        "route_waypoint_count": expected_count,
        "package_sync_state": rec.get("package_sync_state"),
        "verified_at": rec.get("verified_at"),
    }
    env["operator_store"] = store
    if active_id != resolved:
        return _stop(env, PHASE_PERSISTING_OPERATOR_MISSION, OPERATOR_PERSIST_FAILED,
                     f"The durable store's active mission for this vehicle is {active_id!r}, "
                     f"not the mission being published ({resolved}).", STATE_REAL_MISMATCH)
    env["phases"].append(_phase(PHASE_PERSISTING_OPERATOR_MISSION, OK,
                                f"active mission {active_id} is VERIFIED and persisted"))

    # ── BUILDING_PLANNING_PACKAGE ─────────────────────────────────────────────────────────
    try:
        package, pkg_meta = replan_package.build_v1_package(rec, vehicle_id=slug)
    except replan_package.PackageError as exc:
        _mark_sync_required(deps, rec, PACKAGE_BUILD_FAILED)
        return _stop(env, PHASE_BUILDING_PLANNING_PACKAGE, PACKAGE_BUILD_FAILED, str(exc),
                     STATE_PACKAGE_SYNC_REQUIRED)
    env["package_sent"] = package
    env["operator_package"] = pkg_meta
    env["phases"].append(_phase(PHASE_BUILDING_PLANNING_PACKAGE, OK,
                                f"{pkg_meta['package_version']} built "
                                f"({pkg_meta['route_waypoint_count']} route waypoints)"))

    # ── SYNCING_SCOUT_PACKAGE ─────────────────────────────────────────────────────────────
    # Read Scout's slot FIRST, so "this package was already there" is a fact we can report
    # (`idempotent`) rather than a guess. The POST still happens: matching id/hash/count proves
    # the identity matched, not that every byte of geometry did.
    before = deps.scout_get_package(base)
    before_ev = deps.scout_package_evidence((before or {}).get("scout"))
    already = _identity_matches(before_ev, resolved, expected_hash, expected_count)
    env["idempotent"] = bool(already)

    post = deps.scout_post_package(base, package)
    env["scout_post"] = post
    outcome = post.get("outcome")
    if outcome in (scout_replan.OUTCOME_REJECTED, scout_replan.OUTCOME_UNSUPPORTED):
        _mark_sync_required(deps, rec, SCOUT_PACKAGE_POST_FAILED)
        env["scout"] = _scout_block(post, before_ev, None, resolved, expected_hash, expected_count)
        return _stop(env, PHASE_SYNCING_SCOUT_PACKAGE, SCOUT_PACKAGE_POST_FAILED,
                     post.get("error") or "Scout refused the planning package.",
                     STATE_PACKAGE_SYNC_REQUIRED)
    if outcome == scout_replan.OUTCOME_UNAVAILABLE:
        _mark_sync_required(deps, rec, SCOUT_UNREACHABLE)
        env["scout"] = _scout_block(post, before_ev, None, resolved, expected_hash, expected_count)
        return _stop(env, PHASE_SYNCING_SCOUT_PACKAGE, SCOUT_UNREACHABLE,
                     post.get("error") or "Scout is unreachable.", STATE_SCOUT_UNREACHABLE)
    # ACCEPTED or UNKNOWN both continue. An UNKNOWN write is NEVER resent and NEVER called a
    # failure — Scout's package slot is idempotent, so the read-back below is what resolves it.
    env["phases"].append(_phase(PHASE_SYNCING_SCOUT_PACKAGE,
                                OK if outcome == scout_replan.OUTCOME_ACCEPTED else PENDING,
                                post.get("error") or f"Scout POST outcome: {outcome}",
                                None, outcome=outcome))

    # ── VERIFYING_SCOUT_PACKAGE ───────────────────────────────────────────────────────────
    stored = deps.scout_get_package(base)
    stored_ev = deps.scout_package_evidence((stored or {}).get("scout"))
    env["scout_package"] = stored
    scout_block = _scout_block(post, before_ev, stored_ev, resolved, expected_hash, expected_count)
    env["scout"] = scout_block

    if stored.get("outcome") in (scout_replan.OUTCOME_UNAVAILABLE, scout_replan.OUTCOME_UNKNOWN) \
            or not stored.get("ok"):
        _mark_sync_required(deps, rec, SCOUT_PACKAGE_READBACK_FAILED)
        return _stop(env, PHASE_VERIFYING_SCOUT_PACKAGE, SCOUT_PACKAGE_READBACK_FAILED,
                     stored.get("error") or "Scout's stored planning package could not be read "
                     "back, so the package cannot be proven to match the approved mission.",
                     STATE_SCOUT_UNREACHABLE)

    final = {
        "mission_id_match": scout_block["mission_id_match"],
        "hash_match": scout_block["hash_match"],
        "count_match": scout_block["count_match"],
        "agent_ready": False,
    }
    env["final"] = final

    if scout_block["package_mission_id"] is None and scout_block["package_route_hash"] is None:
        _mark_sync_required(deps, rec, SCOUT_PACKAGE_NOT_STORED)
        return _stop(env, PHASE_VERIFYING_SCOUT_PACKAGE, SCOUT_PACKAGE_NOT_STORED,
                     "Scout reports no stored planning package after the write.",
                     STATE_PACKAGE_SYNC_REQUIRED)
    if final["mission_id_match"] is False:
        _mark_sync_required(deps, rec, SCOUT_PACKAGE_ID_MISMATCH)
        return _stop(env, PHASE_VERIFYING_SCOUT_PACKAGE, SCOUT_PACKAGE_ID_MISMATCH,
                     f"Scout stores a package for mission "
                     f"{scout_block['package_mission_id']}, not {resolved}.",
                     STATE_REAL_MISMATCH)
    if final["hash_match"] is False:
        _mark_sync_required(deps, rec, SCOUT_PACKAGE_HASH_MISMATCH)
        return _stop(env, PHASE_VERIFYING_SCOUT_PACKAGE, SCOUT_PACKAGE_HASH_MISMATCH,
                     "Scout's stored package route hash does not match the approved route.",
                     STATE_REAL_MISMATCH)
    if final["count_match"] is False:
        _mark_sync_required(deps, rec, SCOUT_PACKAGE_COUNT_MISMATCH)
        return _stop(env, PHASE_VERIFYING_SCOUT_PACKAGE, SCOUT_PACKAGE_COUNT_MISMATCH,
                     f"Scout's stored package carries {scout_block['package_route_count']} "
                     f"route waypoints; the approved mission has {expected_count}.",
                     STATE_REAL_MISMATCH)
    if None in (final["mission_id_match"], final["hash_match"], final["count_match"]):
        # Scout stored something but did not report every identity we compare on. Not a
        # mismatch — an UNAVAILABLE COMPARISON, which is exactly the case that must never
        # render as one. Agent-ready requires all three PROVEN equal, so this is not READY
        # either: an unmade comparison is not a passed one.
        _mark_sync_required(deps, rec, SCOUT_PACKAGE_READBACK_FAILED)
        missing = [name for name, value in
                   (("mission id", final["mission_id_match"]),
                    ("route hash", final["hash_match"]),
                    ("route waypoint count", final["count_match"])) if value is None]
        return _stop(env, PHASE_VERIFYING_SCOUT_PACKAGE, SCOUT_PACKAGE_READBACK_FAILED,
                     "Scout stored a package but did not report the "
                     + ", ".join(missing)
                     + " needed to prove it matches the approved mission.", STATE_VERIFYING)

    env["phases"].append(_phase(PHASE_VERIFYING_SCOUT_PACKAGE, OK,
                                "package mission id, route hash and route count all match"))

    # ── READY ─────────────────────────────────────────────────────────────────────────────
    final["agent_ready"] = True
    env["ok"] = True
    env["phase"] = PHASE_READY
    env["state"] = STATE_READY
    env["message"] = "Mission uploaded and Agent package synchronized."
    env["phases"].append(_phase(PHASE_READY, OK, env["message"]))
    _mark_synced(deps, rec)
    env["operator_store"]["package_sync_state"] = rec.get("package_sync_state")
    env["readiness"] = deps.readiness(vid, base) if deps.readiness else None
    return env


# ── Evidence blocks ───────────────────────────────────────────────────────────────────────
def _pixhawk_block(readback, expected_hash, expected_count):
    """The Pixhawk evidence, phrased in the mission contract's terms. `raw_item_count` and
    `route_waypoint_count` are reported SEPARATELY and are never compared with each other by a
    consumer — item 0 is Home, and that offset is applied here, once (route_count_from_readback)."""
    if not isinstance(readback, dict):
        return {"reachable": False, "complete": False, "source": None, "evidence_age_s": None,
                "evidence_cached": None, "raw_item_count": None, "route_waypoint_count": None,
                "route_count_source": None, "route_hash": None, "hash_match": False,
                "count_match": False}
    reachable = bool(readback.get("reachable"))
    partial = bool(readback.get("partial"))
    raw = _as_int(readback.get("pixhawk_item_count"))
    if raw is None:
        raw = _as_int(readback.get("raw_count"))
    count, source = route_count_from_readback(readback, expected=expected_count)
    route_hash = readback.get("route_content_hash") or None
    return {
        "reachable": reachable,
        "complete": reachable and not partial,
        "source": readback.get("source"),
        "evidence_age_s": readback.get("evidence_age_s"),
        "evidence_cached": readback.get("evidence_cached"),
        "raw_item_count": raw,
        "route_waypoint_count": count,
        "route_count_source": source,
        "route_hash": route_hash,
        "hash_match": bool(route_hash and expected_hash and route_hash == expected_hash),
        "count_match": (None if count is None else count == expected_count),
    }


def _identity_matches(evidence, mission_id, expected_hash, expected_count):
    """True only when Scout's package evidence PROVES all three identities. A null is never a
    match: an unreported field is an unavailable comparison, not an agreement."""
    if not isinstance(evidence, dict):
        return False
    if evidence.get("mission_id") != mission_id or not mission_id:
        return False
    if evidence.get("route_hash") != expected_hash or not expected_hash:
        return False
    count = _as_int(evidence.get("route_count"))
    return count is not None and count == expected_count


def _scout_block(post, before_ev, after_ev, mission_id, expected_hash, expected_count):
    """The Scout evidence: reachability, support, what it stores now, and the THREE comparisons
    the operator owns. Each comparison is TRI-STATE — True proven equal, False proven different,
    None the comparison could not be made — because collapsing None into False is precisely how
    an unreachable Scout became a "package mismatch"."""
    ev = after_ev if isinstance(after_ev, dict) else {}
    post = post if isinstance(post, dict) else {}
    pkg_id = ev.get("mission_id")
    pkg_hash = ev.get("route_hash")
    pkg_count = _as_int(ev.get("route_count"))

    def cmp(observed, expected):
        if observed is None or expected is None:
            return None
        return observed == expected

    return {
        "reachable": post.get("reachable") is not False,
        "supported": post.get("supported") is not False,
        "post_outcome": post.get("outcome"),
        "post_error": post.get("error"),
        "post_error_code": post.get("scout_error_code"),
        "stored": ev.get("stored"),
        "usable": ev.get("usable"),
        "generation": ("v1" if ev.get("scout_state") is not None
                       or ev.get("scout_replanning_ready") is not None else "legacy"),
        "package_mission_id": pkg_id,
        "package_route_hash": pkg_hash,
        "package_route_count": pkg_count,
        "readiness_state": ev.get("scout_state"),
        "scout_replanning_ready": ev.get("scout_replanning_ready"),
        "mission_id_match": cmp(pkg_id, mission_id),
        "hash_match": cmp(pkg_hash, expected_hash),
        "count_match": cmp(pkg_count, expected_count),
        "matched_before_write": _identity_matches(before_ev, mission_id, expected_hash,
                                                  expected_count),
    }


# ── Durable PACKAGE_SYNC_REQUIRED marking ─────────────────────────────────────────────────
def _mark_sync_required(deps, rec, code):
    """A verified Pixhawk mission whose package did not land. The record and the active mission
    are PRESERVED — rolling back a mission the vehicle is genuinely carrying would be the more
    dangerous lie — and the owed sync is written down durably."""
    rec["package_sync_state"] = SYNC_STATE_REQUIRED
    rec["package_sync_error"] = code
    rec["package_sync_attempted_at"] = _now_iso()
    if deps.persist_sync_state:
        deps.persist_sync_state(rec)


def _mark_synced(deps, rec):
    rec["package_sync_state"] = SYNC_STATE_SYNCED
    rec["package_sync_error"] = None
    rec["package_synced_at"] = _now_iso()
    rec["package_sync_attempted_at"] = rec["package_synced_at"]
    if deps.persist_sync_state:
        deps.persist_sync_state(rec)


# ── HTTP status for an envelope ───────────────────────────────────────────────────────────
def status_code(env):
    """The honest HTTP status for a publish result.

      200  READY — the whole transaction succeeded
      202  the Pixhawk upload has not finished, or Scout's verdict never reached us. Accepted,
           unresolved; poll again. NEVER an error status for a system that is mid-operation.
      409  BUSY, or a state-based refusal (the request was well-formed; the STATE refused it)
      503  Scout / the vehicle API could not be reached at all
    """
    state = env.get("state")
    if env.get("ok"):
        return 200
    if state == STATE_BUSY:
        return 409
    if state == STATE_UPLOAD_IN_PROGRESS or state == STATE_VERIFYING:
        return 202
    if state == STATE_SCOUT_UNREACHABLE:
        return 503
    return 409
