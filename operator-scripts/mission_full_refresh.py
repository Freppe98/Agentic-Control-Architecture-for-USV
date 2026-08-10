"""Agent Mission FULL REFRESH — one READ-ONLY operation that reconstructs the entire current
mission/readiness evidence graph on demand, without uploading a mission.

WHY THIS MODULE EXISTS
-----------------------
A mission upload reliably reaches READY because `mission_publish.run_publish` forces a LIVE
Pixhawk proof, builds and POSTs a matching planning package to Scout, and proves the write
landed. The existing Agent Mission "Refresh" button (`mission_lifecycle.preflight`, called with
`fresh=False`) does none of that: it is a pure, parallel pair of GETs that may read Scout's
mission-execution status/package through caches, and it can never repair anything it finds
wrong, because it performs no write of any kind. A vehicle that already carries the exact
approved mission — proved by a fresh Pixhawk read-back and a matching stored package — can
therefore stay UNBOUND / MISSION_ROUTE_UNVERIFIED / ROUTE_HASH_STALE after a restart even though
nothing is actually wrong, and only a redundant re-upload has ever recovered it.

Full Refresh is the missing middle ground: it forces the SAME fresh-evidence proof the Start
transaction performs (`mission_lifecycle.preflight(fresh=True)`), calls Scout's read-only
binding-reproof route (`scout_mission_execution.post_reprove_binding` — see that module's
docstring for the exact outcome contract), and reads Home, Scout's stabilizing `/agent/state`
evidence, energy feasibility and risk — then returns ONE coherent snapshot, generated from
evidence gathered in this one bounded operation, never a mixture of an old package mismatch
with a newer Pixhawk hash.

THE SEMANTIC CORRECTION THIS MODULE DEPENDS ON — read before touching binding anywhere below:
`binding_state == BOUND` means a LIVE mission execution currently owns/binds the original
mission identity. It is NOT a route-proof signal and it is NOT a Start precondition. A
completely healthy, verified, READY mission BEFORE Start correctly reports
`binding_state == UNBOUND` while `verified_route_hash`, `state == READY`, `start_eligible` and
`can_start` all say the route is proven and Start may proceed — that is the expected IDLE
outcome of a successful refresh, never a failure. Binding only becomes meaningful, and BOUND,
once a run is actually RUNNING. Full Refresh success is therefore judged from the PROOF
(`verified_route_hash`, the three-way reconciliation, Scout's `state`/`start_eligible`/
`can_start`/`start_block_reason`), never from `binding_state`.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-------------------------------------------
  * upload a mission, clear a Pixhawk mission, write mission items, or Set Home;
  * change vehicle mode (ARM/DISARM/AUTO/MANUAL/LOITER/RTL) or authority;
  * execute a replan or rewrite a mismatching planning package — a real mismatch is reported as
    PACKAGE_SYNC_REQUIRED / PIXHAWK_MISMATCH, never silently repaired;
  * fabricate `binding_state = BOUND` locally, or require it for success. Scout remains
    authoritative: binding is reported EXACTLY as Scout's own reprove outcome and
    GET /agent/mission_execution/status show it, whether that is a healthy idle UNBOUND or a
    live-execution BOUND.
  * recompute energy feasibility or continuous risk — both are read verbatim from Scout's
    `/agent/replan/status` body (`energy_feasibility`, `risk`), never derived here.

The only write this module issues, anywhere, is the read-only POST to Scout's binding-reproof
route — and that route's own contract (see scout_mission_execution.post_reprove_binding) forbids
it from ever commanding the vehicle. Every test in tests/test_mission_full_refresh.py proves
that no OTHER Scout write route is ever called by this module.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import scout_mission_execution as mx

# ── Stages (task Section 6), in the order they are appended to the result ─────────────────
STAGE_STARTING = "STARTING"
STAGE_READING_APPROVED_MISSION = "READING_APPROVED_MISSION"
STAGE_READING_PIXHAWK_MISSION = "READING_PIXHAWK_MISSION"
STAGE_READING_PLANNING_PACKAGE = "READING_PLANNING_PACKAGE"
STAGE_RECONCILING_MISSION = "RECONCILING_MISSION"
STAGE_REPROVING_AGENT_BINDING = "REPROVING_AGENT_BINDING"
STAGE_READING_HOME = "READING_HOME"
STAGE_READING_EVIDENCE = "READING_EVIDENCE"
STAGE_EVALUATING_FEASIBILITY = "EVALUATING_FEASIBILITY"
STAGE_EVALUATING_RISK = "EVALUATING_RISK"
STAGE_VERIFYING_FINAL_READINESS = "VERIFYING_FINAL_READINESS"
STAGE_COMPLETE = "COMPLETE"
STAGE_FAILED = "FAILED"

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_WARNING = "warning"

# ── The task's three-way reconciliation vocabulary (Section 10) ───────────────────────────
# Deliberately DISTINCT from mission_reconcile's own outcome vocabulary (RECONCILING /
# SYNCHRONIZED / PACKAGE_SYNC_REQUIRED / UNAPPROVED_MISSION / MISMATCH), which answers "which
# approved RECORD is the operator's active pointer" — a bookkeeping-repair question. This one
# answers "does the approved mission, the live Pixhawk route and Scout's package all agree,
# right now" — the question Full Refresh's response actually needs to report. Both verdicts are
# carried in the result (`mission.reconciliation` is this one; `mission.bookkeeping_reconciliation`
# is mission_reconcile's), because they can legitimately differ during a genuine restart repair.
MATCHED = "MATCHED"
CHECKING = "CHECKING"
EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
PIXHAWK_MISMATCH = "PIXHAWK_MISMATCH"
PACKAGE_SYNC_REQUIRED = "PACKAGE_SYNC_REQUIRED"
PACKAGE_INVALID = "PACKAGE_INVALID"


class Busy(Exception):
    """Another Full Refresh transaction already holds this vehicle."""


# ── Per-vehicle serialization (task Section 26) ────────────────────────────────────────────
# Same idiom as mission_publish.py's vehicle_publish_lock: FastAPI runs sync route handlers in a
# worker threadpool, so a plain non-reentrant lock acquired NON-BLOCKING is enough to make a
# second concurrent Full Refresh for the SAME vehicle fail fast with Busy rather than issuing a
# second, overlapping set of live Pixhawk/Scout reads (and a second binding-reproof POST).
_vehicle_locks: dict = {}
_locks_guard = threading.Lock()


def _vehicle_lock(vid):
    with _locks_guard:
        lock = _vehicle_locks.get(vid)
        if lock is None:
            lock = _vehicle_locks[vid] = threading.Lock()
        return lock


class vehicle_refresh_lock:
    """Context manager holding one vehicle's Full Refresh lock, or raising Busy at once."""

    def __init__(self, vid):
        self._lock = _vehicle_lock(vid)
        self._held = False

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            raise Busy("A Full Refresh is already running for this vehicle.")
        self._held = True
        return self

    def __exit__(self, *exc):
        if self._held:
            self._lock.release()
            self._held = False
        return False


def is_refreshing(vid):
    """True while a Full Refresh transaction holds this vehicle. Read-only; never acquires."""
    lock = _vehicle_lock(vid)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


class Deps:
    """Everything the transaction needs from the operator backend, injected — so the whole
    transaction is unit-testable without a FastAPI app or a real Scout.

    active_mission_id(vid)         -> the vehicle's ACTIVE PERSISTED original mission id, or None
    mission_record(mid)            -> the immutable revision-0 record dict, or None
    run_preflight(vid, base, *, fresh)
                                    -> mission_lifecycle.preflight(deps, vid, base, fresh=fresh) —
                                       the SAME fresh-evidence proof the Start transaction itself
                                       performs. Issues no write.
    reprove(base, mission_id)      -> scout_mission_execution.post_reprove_binding(base,
                                       mission_id) — the RAW transport result; run_full_refresh
                                       narrows it with scout_mission_execution
                                       .interpret_reprove_binding.
    replan_status(base)            -> scout_replan.get_status(base) — Scout's canonical replan
                                       status (carries energy_feasibility/risk when Scout reports
                                       them), always a LIVE read.
    home_view(vid)                 -> the current, read-only Home view for this vehicle (Scout's
                                       own home_status, mirrored — no vehicle contact of any kind)
    agent_state(vid, flask_base)   -> a best-effort, read-only GET of Scout's /agent/state, or
                                       None when the vehicle has no Flask (8080) API configured.
                                       Never gates anything; a failure here never fails the refresh.
    record_operation(entry)        -> append one entry to the full-refresh trace (diagnostics/
                                       logging, task Section 29). Optional.
    """

    def __init__(self, *, active_mission_id, mission_record, run_preflight, reprove,
                 replan_status, home_view, agent_state, record_operation=None):
        self.active_mission_id = active_mission_id
        self.mission_record = mission_record
        self.run_preflight = run_preflight
        self.reprove = reprove
        self.replan_status = replan_status
        self.home_view = home_view
        self.agent_state = agent_state
        self.record_operation = record_operation or (lambda entry: None)


def _now():
    return datetime.now(timezone.utc)


def _stage(stages, name, status, detail=None, **extra):
    entry = {"stage": name, "status": status, "detail": detail}
    entry.update(extra)
    stages.append(entry)
    return entry


def classify_reconciliation(*, approved_hash, pixhawk_usable, pixhawk_hash,
                             package_reachable, package_hash, package_valid):
    """The task's three-way reconciliation vocabulary (Section 10), computed purely from the
    canonical route-CONTENT hashes already gathered elsewhere in this operation (never the
    full-mission/Home-inclusive hash — see the caller, which reads `readback_hash` off
    `vehicle_mission`, itself sourced from `route_content_hash` only).

    Returns (outcome, detail). Never infers a MISMATCH from missing evidence — every absence
    resolves to EVIDENCE_UNAVAILABLE, distinct from a definite PIXHAWK_MISMATCH.

      approved_hash      the operator's approved/active mission's route_hash, or None
      pixhawk_usable      whether the fresh Pixhawk read-back could carry a proof at all
                          (reachable, not partial, carries a route hash — see
                          mission_reconcile.readback_facts, the same rule)
      pixhawk_hash         the fresh Pixhawk read-back's route_content_hash, or None
      package_reachable    whether Scout's planning-package evidence could be read at all
      package_hash          Scout's stored package route_hash, or None
      package_valid          Scout's own package consistency/validation verdict, tri-state
                             (None = Scout did not report one either way)
    """
    if not approved_hash:
        return EVIDENCE_UNAVAILABLE, ("No approved mission route hash is available to "
                                      "reconcile against.")
    if not pixhawk_usable or not pixhawk_hash:
        return EVIDENCE_UNAVAILABLE, ("The fresh Pixhawk route proof is unavailable or "
                                      "incomplete — reconciliation cannot proceed.")
    if approved_hash != pixhawk_hash:
        return PIXHAWK_MISMATCH, ("The flight controller's route does not match the approved "
                                  "mission.")
    if not package_reachable:
        return EVIDENCE_UNAVAILABLE, "Scout's planning package could not be read."
    if package_valid is False:
        return PACKAGE_INVALID, "Scout reports its stored planning package as invalid."
    if not package_hash:
        return EVIDENCE_UNAVAILABLE, "Scout did not report a planning-package route hash."
    if package_hash != approved_hash:
        return PACKAGE_SYNC_REQUIRED, ("Scout's planning package does not match the approved/"
                                       "Pixhawk route.")
    return MATCHED, ("The approved mission, the flight controller and Scout's planning package "
                     "all carry the same canonical route.")


def _envelope(op_id, slug, started):
    return {
        "ok": False, "operation_id": op_id, "read_only": True, "vehicle_id": slug,
        "supported": True, "started_at": started.isoformat(), "completed_at": None,
        "duration_s": None, "stages": [],
        "mission": {"approved": None, "pixhawk": None, "planning_package": None,
                    "reconciliation": None, "reconciliation_detail": None,
                    "bookkeeping_reconciliation": None},
        "binding": None, "home": None, "energy_feasibility": None, "risk": None,
        "readiness": None, "publish": None, "evidence": None,
        "error": None, "error_code": None,
    }


def _finish(out, stages, started, *, ok):
    completed = _now()
    out["ok"] = ok
    out["completed_at"] = completed.isoformat()
    out["duration_s"] = round((completed - started).total_seconds(), 3)
    _stage(stages, STAGE_COMPLETE if ok else STAGE_FAILED,
          STATUS_OK if ok else STATUS_FAILED,
          f"Full Refresh {'complete' if ok else 'incomplete'} in {out['duration_s']}s")
    return out


# ── The transaction ─────────────────────────────────────────────────────────────────────────
def run_full_refresh(deps, vid, base, flask_base, slug):
    """Run ONE Full Refresh transaction for `vid` and return the coherent snapshot (task
    Sections 1-18). `base` is the vehicle's Local Agent (8090) URL; `flask_base` is its Flask
    (8080) URL, or None when unconfigured — only the best-effort /agent/state read uses it.

    Read-only. Never called on a poll — only from the explicit Refresh button (or, per task
    Section 28, once at startup) — and callers are expected to serialize it per vehicle with
    `vehicle_refresh_lock` (see main.py's route)."""
    started = _now()
    op_id = uuid.uuid4().hex
    out = _envelope(op_id, slug, started)
    stages = out["stages"]
    _stage(stages, STAGE_STARTING, STATUS_OK, f"Full Refresh {op_id} starting for {slug}")

    # ── Step 1 (Section 7): approved mission. Missing => fail closed, no fabrication. ────────
    mission_id = deps.active_mission_id(vid)
    rec = deps.mission_record(mission_id) if mission_id else None
    if rec is None:
        _stage(stages, STAGE_READING_APPROVED_MISSION, STATUS_FAILED,
              "No active persisted mission record for this vehicle.")
        out["error_code"] = "NO_ACTIVE_MISSION"
        out["error"] = "This vehicle has no active persisted mission record — nothing to refresh."
        return _finish(out, stages, started, ok=False)

    approved_hash = rec.get("route_hash")
    approved = {
        "mission_id": mission_id, "route_hash": approved_hash,
        "route_count": len(rec.get("route_waypoints") or []),
        "created_at": rec.get("created_at"), "verified_at": rec.get("verified_at"),
        "upload_status": rec.get("upload_status"),
        "package_sync_state": rec.get("package_sync_state"),
    }
    out["mission"]["approved"] = approved
    _stage(stages, STAGE_READING_APPROVED_MISSION, STATUS_OK,
          f"Active persisted mission {mission_id}", mission_id=mission_id)

    # ── Step 5, attempted FIRST (Sections 11/12/21): the read-only binding reproof, so any
    # binding change it causes is what the fresh preflight below observes — never the reverse,
    # which would report a reproof result computed from evidence gathered before it ran. ───────
    reprove_result = mx.interpret_reprove_binding(deps.reprove(base, mission_id) or {})
    # The TRANSPORT verdict (accepted/rejected/unknown/unavailable/unsupported) — whether the
    # POST itself landed — distinct from `reprove_scout_outcome`, Scout's own reprove-outcome
    # WORD (REPROVED / ALREADY_PROVEN / PACKAGE_MISMATCH / … — see scout_mission_execution
    # .REPROVE_OUTCOMES), which is what task Section 10 actually maps.
    reprove_transport_outcome = reprove_result.get("outcome")
    reprove_scout_outcome = reprove_result.get("reprove_outcome")
    reprove_supported = reprove_result.get("reprove_supported")
    reprove_success = reprove_result.get("reprove_success")
    reprove_inconclusive = reprove_result.get("reprove_inconclusive")
    reprove_fail_closed = reprove_result.get("reprove_fail_closed")

    # ── Steps 2-4 (Sections 8-10) + the eligibility/binding checks: the SAME fresh-evidence
    # proof the Start transaction performs — a live Pixhawk read-back, a live planning-package
    # read, and reconciliation. Issues no write. ─────────────────────────────────────────────
    pre = deps.run_preflight(vid, base, fresh=True) or {}
    out["readiness"] = pre
    rd = pre.get("readiness") or {}
    vm = rd.get("vehicle_mission") or {}
    pk = rd.get("planning_package") or {}
    summary = pre.get("summary") or {}

    pixhawk_hash = vm.get("readback_hash")
    pixhawk_usable = bool(vm.get("readback_reachable") and not vm.get("readback_partial")
                          and pixhawk_hash)
    out["mission"]["pixhawk"] = {
        "reachable": vm.get("readback_reachable"), "partial": vm.get("readback_partial"),
        "route_hash": pixhawk_hash, "route_count": vm.get("readback_route_count"),
        "current_seq": vm.get("readback_current_seq"),
        "evidence_age_s": vm.get("readback_age_s"), "evidence_cached": vm.get("readback_cached"),
    }
    _stage(stages, STAGE_READING_PIXHAWK_MISSION,
          STATUS_OK if pixhawk_usable else STATUS_WARNING,
          "Live Pixhawk route proof obtained." if pixhawk_usable else
          "Live Pixhawk route proof unavailable or incomplete — reported honestly, not as a "
          "mismatch.")

    out["mission"]["planning_package"] = {
        "mission_id": pk.get("mission_id"), "route_hash": pk.get("route_hash"),
        "original_route_hash": approved_hash, "route_count": pk.get("route_count"),
        "stored": pk.get("stored"), "usable": pk.get("usable"),
        "validation": {"valid": pk.get("consistent")},
        "pixhawk_hash_used": pixhawk_hash, "scout_reachable": pk.get("scout_reachable"),
    }
    _stage(stages, STAGE_READING_PLANNING_PACKAGE,
          STATUS_OK if pk.get("scout_reachable") else STATUS_WARNING,
          "Scout planning package read." if pk.get("scout_reachable") else
          "Scout planning package could not be read.")

    bookkeeping = rd.get("reconciliation")
    out["mission"]["bookkeeping_reconciliation"] = bookkeeping
    # `package_valid` here is Scout's OWN "usable" verdict — independent of the hash comparison
    # done below. `pk["consistent"]` (shown to the operator as `validation.valid` above) already
    # FACTORS IN hash agreement, so passing it here would make every honest hash disagreement
    # misreport as PACKAGE_INVALID instead of PACKAGE_SYNC_REQUIRED.
    outcome, detail = classify_reconciliation(
        approved_hash=approved_hash, pixhawk_usable=pixhawk_usable, pixhawk_hash=pixhawk_hash,
        package_reachable=bool(pk.get("scout_reachable")), package_hash=pk.get("route_hash"),
        package_valid=pk.get("usable"))
    out["mission"]["reconciliation"] = outcome
    out["mission"]["reconciliation_detail"] = detail
    _stage(stages, STAGE_RECONCILING_MISSION,
          STATUS_OK if outcome == MATCHED else
          (STATUS_FAILED if outcome == PIXHAWK_MISMATCH else STATUS_WARNING),
          detail, reconciliation=outcome)

    binding_view = pre.get("binding") or {}
    binding_state = summary.get("binding_state")
    # `binding_state` is reported EXACTLY as Scout has it and is NEVER a success/failure signal
    # here — see the module docstring. A healthy idle mission reports UNBOUND; BOUND is expected
    # only once a run is actually RUNNING and owns the mission identity.
    out["binding"] = {
        "reproof_attempted": True,
        # Scout's own reprove-outcome WORD (REPROVED / ALREADY_PROVEN / PACKAGE_MISMATCH / … or
        # None when Scout never told us one) — this is what task Section 10 maps, never the bare
        # transport verdict.
        "reproof_outcome": reprove_scout_outcome,
        "reproof_success": bool(reprove_success),
        "reproof_inconclusive": bool(reprove_inconclusive),
        "reproof_fail_closed": bool(reprove_fail_closed),
        "reproof_transport_outcome": reprove_transport_outcome,
        "reproof_supported": reprove_supported,
        "reproof_scout_error_code": reprove_result.get("scout_error_code"),
        "binding_state": binding_state,
        "verified_route_hash": binding_view.get("verified_route_hash"),
        "bound_original_mission_id": binding_view.get("bound_original_mission_id"),
        "package_conflict_code": binding_view.get("conflict_code"),
        "blocks_new_mission": binding_view.get("blocks_new_mission"),
    }
    _stage(stages, STAGE_REPROVING_AGENT_BINDING,
          STATUS_OK if reprove_success else
          (STATUS_SKIPPED if not reprove_supported else
           STATUS_FAILED if reprove_fail_closed else STATUS_WARNING),
          (f"Scout {reprove_scout_outcome.lower()} the mission route; binding is currently "
           f"{binding_state or 'UNREPORTED'} (UNBOUND is the expected, healthy value for an idle "
           f"mission — BOUND is expected only once a run is RUNNING)."
           if reprove_success
           else "This Scout does not implement the read-only binding-reproof route yet — binding "
                f"is reported exactly as Scout's mission-execution status shows it "
                f"({binding_state or 'UNREPORTED'}), never fabricated." if not reprove_supported
           else f"Scout's binding reproof outcome was {reprove_scout_outcome or 'unresolved'} — "
                f"binding is reported exactly as Scout's mission-execution status shows it "
                f"({binding_state or 'UNREPORTED'}), never fabricated."),
          binding_state=binding_state, reproof_outcome=reprove_scout_outcome)

    # ── Step 6 (Section 13): Home — Scout's own home_status, mirrored, read-only. ─────────────
    home = deps.home_view(vid) or {}
    out["home"] = home
    _stage(stages, STAGE_READING_HOME,
          STATUS_OK if home.get("verified") else STATUS_WARNING,
          "Home verified." if home.get("verified") else
          (home.get("reason") or "Home is not verified."))

    # ── Step 7 (Section 14): stabilizing /agent/state evidence — best effort, never gates. ────
    evidence = deps.agent_state(vid, flask_base) if flask_base else None
    out["evidence"] = evidence
    evidence_reachable = bool(evidence and evidence.get("reachable"))
    _stage(stages, STAGE_READING_EVIDENCE,
          STATUS_OK if evidence_reachable else STATUS_SKIPPED,
          "Scout /agent/state read." if evidence_reachable else
          "Scout /agent/state unavailable — omitted; nothing was inferred from its absence.")

    # ── Steps 8-9 (Sections 15/16): energy feasibility + risk, Scout's own words verbatim. ────
    replan = deps.replan_status(base) or {}
    replan_body = replan.get("scout") if isinstance(replan.get("scout"), dict) else {}
    energy = replan_body.get("energy_feasibility")
    risk = replan_body.get("risk")
    out["energy_feasibility"] = energy
    out["risk"] = risk
    _stage(stages, STAGE_EVALUATING_FEASIBILITY,
          STATUS_OK if energy is not None else STATUS_SKIPPED,
          "Energy feasibility read from Scout." if energy is not None else
          "Scout did not report energy feasibility.")
    _stage(stages, STAGE_EVALUATING_RISK,
          STATUS_OK if risk is not None else STATUS_SKIPPED,
          "Risk read from Scout." if risk is not None else "Scout did not report risk.")

    # ── Step 10 (Section 17): final coherent snapshot. `publish` is shaped EXACTLY like
    # GET .../missions/publish so the existing frontend rendering (readinessLabel/preflightNote)
    # needs no new code to consume it. ────────────────────────────────────────────────────────
    out["publish"] = {
        "mission_id": mission_id, "record_present": True,
        "upload_status": rec.get("upload_status"), "route_hash": approved_hash,
        "route_waypoint_count": approved["route_count"],
        "package_sync_state": rec.get("package_sync_state"),
        "package_sync_error": rec.get("package_sync_error"),
        "package_synced_at": rec.get("package_synced_at"),
        "reconciliation": bookkeeping,
    }

    # `ok` is "this refresh ran to completion and produced a coherent, evidence-complete
    # snapshot" — NOT "the mission is Start-ready" and NOT "binding_state is BOUND". A healthy
    # PACKAGE_SYNC_REQUIRED / PIXHAWK_MISMATCH / not-yet-eligible refresh is a fully successful,
    # complete refresh that correctly reports NOT_READY, and a healthy idle refresh correctly
    # reports binding UNBOUND; `readiness.can_start` (the SAME field the Start transaction itself
    # gates on) is where Start eligibility is read, never this `ok` and never binding_state.
    #
    # The reprove round only sinks `ok` when Scout could not reach ANY verdict this round
    # (`reprove_inconclusive` — EVIDENCE_UNAVAILABLE / NO_CURRENT_PACKAGE / NO_CURRENT_MISSION /
    # BUSY / INTERNAL_ERROR): that is an unread input, exactly like an unreachable Pixhawk or an
    # unreachable package. A DEFINITE mismatch Scout proved (PACKAGE_MISMATCH / PIXHAWK_MISMATCH /
    # MISSION_ID_MISMATCH) is a conclusive answer — same as the Operator's own PIXHAWK_MISMATCH
    # reconciliation above — and does not, by itself, make the refresh incomplete.
    proof_complete = bool(pre.get("proof_complete"))
    refresh_ok = proof_complete and outcome != EVIDENCE_UNAVAILABLE and not reprove_inconclusive
    _stage(stages, STAGE_VERIFYING_FINAL_READINESS,
          STATUS_OK if refresh_ok else STATUS_WARNING,
          f"can_start={pre.get('can_start')}, reconciliation={outcome}, "
          f"proof_complete={proof_complete}, reprove_outcome={reprove_scout_outcome}")

    result = _finish(out, stages, started, ok=refresh_ok)
    deps.record_operation(result)
    return result
