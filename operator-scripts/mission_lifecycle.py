"""ONE transaction per operator intent: authority orchestration + Scout mission-execution.

WHY THIS MODULE EXISTS
----------------------
Normal mission operation used to require the operator to press "Release Control" on the Map,
walk to the Agent page, and press Start there — two unrelated commands, on two pages, with the
operator personally responsible for getting the internal authority hand-off right. That is a
control-architecture defect, not a UI inconvenience: the station knows perfectly well that
mission execution needs LOCAL_AGENT authority, so it must arrange that itself, verify it, and
report it as ONE operation with phases.

This module is that layer. It is the ONLY place the authority-plus-lifecycle transaction is
implemented — Map.js does not reimplement it, the Agent page does not reimplement it, and
main.py's routes are thin adapters over the four entry points below:

    run_start(deps, vid, base, supplied_mission_id=None)
    run_pause(deps, vid, base)
    run_resume(deps, vid, base)
    run_stop(deps, vid, base)

THE RULES IT ENFORCES (each one exists because its opposite is unsafe)
---------------------------------------------------------------------
1. The Start proxy ALWAYS forwards the ACTIVE PERSISTED mission id. A UI-supplied id is never
   trusted when an active record exists: it must match, or the request is rejected HERE,
   before Scout is contacted at all. Scout's own MISSION_ID_MISMATCH is a second line of
   defence, not the first.
2. Authority is TRANSFERRED AND READ BACK before Scout Start is contacted. A POST that was not
   confirmed by a subsequent read is not a transfer, and a Start is never issued on top of it.
3. An UNKNOWN write is NEVER resent. It is reconciled by READING canonical status — resending
   a Start could re-run a whole Home/AUTO transaction the vehicle already performed.
4. Authority is returned to OPERATOR after a failed Start ONLY when the backend can PROVE it is
   safe: a definite pre-action refusal AND a canonical status read showing Scout resting in a
   pre-start state with no active operation and no replanning. Unknown, uncertain, running,
   mid-transaction or post-command failures never take authority back. Never guess.
5. Pause keeps LOCAL_AGENT. Resume re-acquires it only if it was lost. Stop performs NO
   authority write at all: Scout returns supervisory authority to OPERATOR inside its own stop
   transaction, and this layer READS IT BACK and reports whether it could confirm it.
6. Low-level AUTO / MANUAL / RTL / ARM / DISARM are NEVER used to implement any of this, and
   neither is the legacy raw Pixhawk stop. The only vehicle-facing calls are Scout's own
   mission-execution transactions. Stop in particular sends no LOITER, no mission upload, no
   rewind, no replan reset, no experiment clear and no rearm — Scout does all of it.

WHAT IT IS NOT: a second mission-execution FSM. Every lifecycle fact reported here is Scout's
own canonical status or the body Scout returned; nothing is inferred, defaulted or rounded up.

DEPENDENCY INJECTION: everything the transaction needs from the operator backend (the active
mission record, the readiness evidence, the authority proxy) arrives as a `Deps` of callables,
so the whole layer is unit-testable without a FastAPI app and cannot reach around into main.py.
"""
from __future__ import annotations

import inspect

import scout_mission_execution as mx

AUTHORITY_OPERATOR = "OPERATOR"
AUTHORITY_LOCAL_AGENT = "LOCAL_AGENT"

# Phase names. A response carries the phases that actually ran, in order, so the operator sees
# ONE operation ("Start Mission") with its authority-transfer and Start phases — never two
# unrelated commands, and never a Start whose authority step is invisible.
PHASE_MISSION = "mission-resolution"
PHASE_PRECONDITIONS = "preconditions"
PHASE_AUTHORITY = "authority-transfer"
PHASE_OPERATION = "scout-operation"
PHASE_VERIFY = "verification"
PHASE_RESTORE = "authority-restore"

# Phase/operation statuses.
OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"
WITHHELD = "withheld"

# An operation refused by the OPERATOR backend before Scout was contacted. Distinct from
# Scout's own `rejected`: nothing left this station, so nothing can have taken effect.
OUTCOME_BLOCKED = "blocked"

# Which states a Start may be attempted from. NOT_READY is included because Scout reports it
# precisely when authority is not yet LOCAL_AGENT — the condition this transaction resolves.
STARTABLE_STATES = frozenset({"READY", "NOT_READY", "NOT_STARTED"}) | mx.STOPPED_STATES


class Deps:
    """The operator-backend facts and side effects the transaction needs, injected.

    active_mission_id(vid)    -> the vehicle's ACTIVE PERSISTED original mission id, or None
    mission_record(mid)       -> the immutable revision-0 record dict, or None
    readiness(vid, base, *, fresh=False)
                              -> the combined mission/replanning readiness evidence dict.
                                 `fresh=True` must bypass every evidence cache and pay for a live
                                 read (see main._compute_replan_readiness / _pixhawk_readback).
                                 A `readiness` callable that does not accept the keyword is called
                                 without it, so an older injection still works — it simply cannot
                                 be asked for a fresh read.
    get_authority(vid)        -> {"authority", "reachable", "available", ...} (live Scout read)
    set_authority(vid, value) -> {"ok", "authority", ...} (live Scout write)
    """

    def __init__(self, *, active_mission_id, mission_record, readiness,
                 get_authority, set_authority):
        self.active_mission_id = active_mission_id
        self.mission_record = mission_record
        self.readiness = readiness
        self.get_authority = get_authority
        self.set_authority = set_authority
        # Resolved ONCE, from the signature — never by catching a TypeError, which would silently
        # swallow a genuine TypeError raised inside the callable and answer with stale evidence.
        try:
            self._readiness_takes_fresh = "fresh" in inspect.signature(readiness).parameters
        except (TypeError, ValueError):
            self._readiness_takes_fresh = False

    def readiness_evidence(self, vid, base, *, fresh=False):
        """The readiness evidence, optionally forcing a live re-read of everything it is computed
        from. `fresh=True` is used by the Start transaction and by nothing else."""
        if fresh and self._readiness_takes_fresh:
            return self.readiness(vid, base, fresh=True)
        return self.readiness(vid, base)


def _text(value):
    """A READABLE string for anything Scout or the backend hands us — never the JavaScript-style
    "[object Object]" and never Python's dict repr leaking into operator-facing copy."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [p for p in (_text(v) for v in value) if p]
        return "; ".join(parts) or None
    if isinstance(value, dict):
        for key in ("message", "detail", "reason", "error", "code", "error_code"):
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
        parts = [f"{k}={_text(v)}" for k, v in value.items() if _text(v) is not None]
        return " · ".join(parts) or None
    return str(value)


def _error_code(value):
    """Scout's machine error CODE from a structured error, or the string it sent. Distinct from
    _text, which prefers the human message — the code is the part the operator quotes and acts
    on, so it must survive on its own."""
    if isinstance(value, dict):
        for key in ("code", "error_code", "error"):
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
        return None
    if isinstance(value, str):
        return value.strip() or None
    return None


def _phase(name, status, detail=None, **extra):
    out = {"phase": name, "status": status, "detail": _text(detail)}
    out.update(extra)
    return out


def _envelope(operation, vid_slug, mission_id=None):
    return {
        "ok": False,
        "operation": operation,
        "vehicle_id": vid_slug,
        "mission_id": mission_id,
        "requested_mission_id": mission_id,
        "outcome": None,
        "phases": [],
        "authority": {"before": None, "after": None, "required": None, "verified": None},
        "error": None,
        "scout_error_code": None,
        "scout_error_message": None,
        "reconciliation": None,
        "scout": None,
        "supported": True,
    }


def _blocked(env, code, message, *, phase=PHASE_PRECONDITIONS, **extra):
    """Refused by the OPERATOR before Scout was contacted. `blocked` is deliberately its own
    outcome: `rejected` means Scout refused, and conflating the two would tell the operator the
    vehicle answered when nothing ever left this station."""
    env["outcome"] = OUTCOME_BLOCKED
    env["ok"] = False
    env["error"] = _text(message)
    env["error_code"] = code
    env["phases"].append(_phase(phase, FAILED, message, code=code, **extra))
    return env


# ── Mission identity ──────────────────────────────────────────────────────────────────────
def resolve_mission_id(deps, vid, supplied):
    """(mission_id, error) for the vehicle's ACTIVE PERSISTED original mission.

    The persisted active record WINS. A UI-supplied id is accepted only when it names that same
    record; anything else is a local rejection with an explicit reason, because a browser must
    never be able to point a Start at a route the operator did not approve — not even to a real
    mission belonging to another vehicle or an older revision."""
    active = deps.active_mission_id(vid)
    supplied = (supplied or "").strip() or None if isinstance(supplied, str) else supplied
    if active:
        if supplied and supplied != active:
            return None, ("MISSION_ID_MISMATCH",
                          f"The requested mission {supplied} is not this vehicle's active "
                          f"mission {active}. The Operator forwards the active persisted "
                          f"mission only.")
        return active, None
    if supplied:
        return None, ("NO_ACTIVE_MISSION",
                      f"This vehicle has no active persisted mission record, so the supplied "
                      f"mission {supplied} cannot be verified against one. Finalize and upload "
                      f"a mission before starting.")
    return None, ("NO_ACTIVE_MISSION",
                  "This vehicle has no active persisted mission record. Finalize and upload a "
                  "mission before starting.")


# ── Start eligibility, as Scout reports it (with the ONE authority deferral) ───────────────
def start_eligibility(summary):
    """Whether Scout's canonical status permits a Start ONCE the Operator has transferred
    authority. Returns {eligible, deferred_on_authority, execution_ready, reason, source}.

    SCOUT IS THE AUTHORITY ON THIS, and it now says so explicitly. `can_start` alone is no
    longer the input — it conflated two independent facts and produced the exact misreading
    this station had to stop making:

        start_eligible=true + authority_blocks_start=true

    is the NORMAL pre-Start condition, not a broken mission. Scout does not seize LOCAL_AGENT
    authority by itself; the Operator's Start transaction acquires and verifies it as its first
    phase. Rendering that as AUTHORITY_NOT_LOCAL_AGENT — "not ready" — told the operator to go
    and fix, by hand, the one thing the button was about to do.

    So the rule is:

        execution_ready=true                      -> eligible now, under LOCAL_AGENT
        start_eligible=true, authority blocks      -> eligible, DEFERRED to the authority phase
        start_eligible=true                        -> eligible
        start_eligible=false                       -> blocked, with SCOUT'S OWN start_block_reason
        the contract is absent (an older Scout)    -> the previous can_start reading, unchanged

    The three guards ahead of it are not second-guessing Scout; each reads a DIFFERENT Scout
    field, and a status that says both "start_eligible" and "the replanning controller owns the
    vehicle" is self-contradictory. We fail closed on a contradiction. Nothing here re-derives
    Scout's preconditions, and none of the operator-owned evidence gates is affected."""
    if not summary.get("present"):
        return {"eligible": False, "deferred_on_authority": False, "execution_ready": False,
                "source": "status",
                "reason": "Scout mission-execution status is unavailable — no Start can be "
                          "issued against an unknown lifecycle state"}
    state = (summary.get("state") or "").upper()
    if summary.get("replanning_active"):
        return {"eligible": False, "deferred_on_authority": False, "execution_ready": False,
                "source": "replanning",
                "reason": "The replanning controller owns the vehicle"}
    if summary.get("active_operation_id"):
        return {"eligible": False, "deferred_on_authority": False, "execution_ready": False,
                "source": "operation",
                "reason": f"Scout is already processing operation "
                          f"{summary['active_operation_id']}"}
    if summary.get("mission_execution_enabled") is False:
        return {"eligible": False, "deferred_on_authority": False, "execution_ready": False,
                "source": "disabled",
                "reason": "Mission execution is disabled on Scout"}

    # ── Scout's explicit contract, when it reports one ────────────────────────────────────
    if summary.get("eligibility_reported"):
        blocks_authority = summary.get("authority_blocks_start") is True
        if summary.get("execution_ready") is True:
            return {"eligible": True, "deferred_on_authority": False, "execution_ready": True,
                    "source": "scout", "reason": None}
        if summary.get("start_eligible") is True:
            if blocks_authority:
                authority = (summary.get("authority_status") or "").upper() or "not LOCAL_AGENT"
                return {
                    "eligible": True, "deferred_on_authority": True, "execution_ready": False,
                    "source": "scout",
                    "reason": f"Scout reports the mission is eligible to start while authority "
                              f"is {authority}. The Start transaction acquires and verifies "
                              f"LOCAL_AGENT authority first; Scout arbitrates the Start itself."}
            return {"eligible": True, "deferred_on_authority": False, "execution_ready": False,
                    "source": "scout", "reason": None}
        # NOT eligible — and the reason is Scout's, verbatim, not a re-derivation of it.
        return {"eligible": False, "deferred_on_authority": False, "execution_ready": False,
                "source": "scout",
                "reason": _text(summary.get("start_block_reason"))
                          or (f"Scout reports the mission is not eligible to start"
                              + (f" in {state}" if state else ""))}

    # ── An older Scout: the previous reading, unchanged ───────────────────────────────────
    if summary.get("can_start") is True:
        return {"eligible": True, "deferred_on_authority": False, "execution_ready": False,
                "source": "can_start", "reason": None}
    if state not in STARTABLE_STATES:
        return {"eligible": False, "deferred_on_authority": False, "execution_ready": False,
                "source": "can_start",
                "reason": f"Scout is in {state or 'an unreported state'}, which is not a state "
                          f"a mission can be started from"}
    authority = (summary.get("authority_status") or "").upper()
    if authority and authority != AUTHORITY_LOCAL_AGENT:
        return {"eligible": True, "deferred_on_authority": True, "execution_ready": False,
                "source": "can_start",
                "reason": f"Scout reports can_start=false while authority is {authority}; the "
                          f"Start transaction transfers authority to LOCAL_AGENT first and "
                          f"Scout arbitrates the Start itself"}
    return {"eligible": False, "deferred_on_authority": False, "execution_ready": False,
            "source": "can_start",
            "reason": "Scout reports can_start=false" +
                      (f" in {state}" if state else "")}


# ── Mission/package binding and replacement conflicts (Scout's word, compared not recomputed) ─
def binding_view(summary):
    """What Scout says about the binding between the package it holds and the mission it is
    executing, plus any replacement conflict. Returns {state, conflict_code, blocks_new_mission,
    message} — or a null view when Scout reports neither.

    `blocks_new_mission` is the one derived bit, and it is deliberately narrow: a newly uploaded
    mission must NOT be shown as ready while Scout says the PREVIOUS run still owns the vehicle.
    Every way out of that is one SCOUT owns — let the run finish, abort it with Scout's own Stop,
    or explicitly rearm the controller. Nothing here is emulated locally; inventing a fourth
    remedy would be a second lifecycle."""
    state = summary.get("binding_state")
    code = summary.get("package_conflict_code")
    if not state and not code:
        return {"state": None, "conflict_code": None, "blocks_new_mission": False,
                "message": None, "reported": False}
    conflict = summary.get("package_conflict") or {}
    blocks = (code in mx.ACTIVE_CONFLICT_CODES) or state == mx.BINDING_STALE_MISMATCH
    message = None
    if blocks:
        message = ("A new mission was uploaded while another mission is active on this vehicle. "
                   "Finish the active mission, stop it, or rearm the mission controller before "
                   "starting the new one.")
        detail = _text(conflict.get("message") or conflict.get("detail"))
        if detail:
            message = f"{message} Scout reports: {detail}"
    return {"state": state, "conflict_code": code, "blocks_new_mission": blocks,
            "message": message, "reported": True,
            "bound_original_mission_id": summary.get("bound_original_mission_id"),
            "package_mission_id": summary.get("package_mission_id"),
            "package_route_hash": summary.get("package_route_hash"),
            "verified_route_hash": summary.get("verified_route_hash"),
            "execution_state": _text(conflict.get("execution_state"))}


# ── Proof completeness: was there enough evidence to CALL this a verdict? ─────────────────
# A precondition check answers `ok:false` for two very different reasons, and collapsing them is
# what makes a stable vehicle flicker:
#
#   a PROVEN failure   the evidence was read and it says no — the hash does not match, the
#                      record is not VERIFIED, Scout reports a state a Start cannot run from.
#   an UNREAD input    the evidence could not be obtained this round. The read-back is served
#                      from a 10 s cache (main.PIXHAWK_READBACK_TTL_S), so roughly every tenth
#                      poll pays for a live MAVLink download; a download that times out or
#                      arrives partial leaves `readback_reachable:false` / `readback_partial:
#                      true`, and — because the package hash chain is anchored on the read-back
#                      — takes `planning_package.hash_match` and `replanning_ready` down WITH it.
#                      One missing read produces three "failures", none of which is a fact about
#                      the vehicle.
#
# The second case is NOT a proof and must never overwrite one. It is reported here as explicit
# structure — `proof_complete` / `readiness_refreshing` / `readiness_reason_code` — so no
# consumer has to guess it from blocker wording, which is exactly the mistake this whole change
# exists to remove.
#
# THIS CHANGES NO GATE. `can_start` stays `all(checks ok)` and the Start transaction stays
# fail-closed: an incomplete proof blocks a Start just as firmly as a proven failure, because an
# unread precondition is not a satisfied one. The distinction is only about what may be
# REMEMBERED between two polls.
EVIDENCE_STATUS_UNAVAILABLE = "STATUS_UNAVAILABLE"
EVIDENCE_READBACK_UNAVAILABLE = "READBACK_UNAVAILABLE"
EVIDENCE_READBACK_PARTIAL = "READBACK_PARTIAL"
EVIDENCE_PACKAGE_UNAVAILABLE = "PACKAGE_UNAVAILABLE"

EVIDENCE_TEXT = {
    EVIDENCE_STATUS_UNAVAILABLE: "Scout's mission-execution status could not be read",
    EVIDENCE_READBACK_UNAVAILABLE: "The Pixhawk mission read-back could not be obtained",
    EVIDENCE_READBACK_PARTIAL: "The Pixhawk mission read-back arrived incomplete",
    EVIDENCE_PACKAGE_UNAVAILABLE: "Scout's planning package could not be read",
}


def proof_completeness(readiness, summary):
    """Whether every input the precondition checks are computed FROM was actually obtained.

    Returns {proof_complete, readiness_refreshing, readiness_reason_code, readiness_reason}.

    `readiness_refreshing` is the narrower claim: the missing input is one this very request
    tried to re-acquire (`readback_cached:false` — a live download was attempted and did not
    yield usable evidence). A CACHED failure is still incomplete, but it is reported as
    unavailable rather than as a refresh, because calling a Scout that has been down for a
    minute "refreshing" would be its own small lie.

    Derived from the evidence fields only — never from blocker or limitation text."""
    rd = readiness if isinstance(readiness, dict) else {}
    vm = rd.get("vehicle_mission") or {}
    pk = rd.get("planning_package") or {}

    code = None
    if not (summary or {}).get("present"):
        code = EVIDENCE_STATUS_UNAVAILABLE
    elif vm.get("readback_reachable") is False:
        code = EVIDENCE_READBACK_UNAVAILABLE
    elif vm.get("readback_partial") is True:
        code = EVIDENCE_READBACK_PARTIAL
    # scout_supported:false is an OLDER SCOUT — a permanent, complete answer, not a gap.
    elif pk.get("scout_reachable") is False and pk.get("scout_supported") is not False:
        code = EVIDENCE_PACKAGE_UNAVAILABLE

    if code is None:
        return {"proof_complete": True, "readiness_refreshing": False,
                "readiness_reason_code": None, "readiness_reason": None}
    refreshing = (code in (EVIDENCE_READBACK_UNAVAILABLE, EVIDENCE_READBACK_PARTIAL)
                  and vm.get("readback_cached") is False)
    return {"proof_complete": False, "readiness_refreshing": bool(refreshing),
            "readiness_reason_code": code, "readiness_reason": EVIDENCE_TEXT[code]}


# ── Start preconditions (evidence the OPERATOR owns, checked before any write) ─────────────
def start_preconditions(deps, vid, base, *, mission_id, summary, fresh=False):
    """The five required conditions, each answered from evidence rather than assumption.

    Returned as a list of checks so the SAME function computes the read-only preflight and the
    Start enforcement — the informational display and the gate can never disagree about what the
    conditions ARE, because there is only one of them.

    `fresh` is what distinguishes the two callers, and it matters. The read-only preflight is
    allowed to answer from the bounded read-back cache (main.PIXHAWK_READBACK_TTL_S) — it is
    information, it labels its own evidence age, and it changes nothing. The START transaction
    passes fresh=True and pays for a live Pixhawk mission download, because it is about to
    authorize vehicle writes and a 10-second-old hash is not a proof that the route on the flight
    controller is the approved one RIGHT NOW."""
    record = deps.mission_record(mission_id) if mission_id else None
    rd = deps.readiness_evidence(vid, base, fresh=fresh) or {}
    vm = rd.get("vehicle_mission") or {}
    pk = rd.get("planning_package") or {}
    elig = start_eligibility(summary)

    verified = bool(record and record.get("upload_status") == "VERIFIED")
    checks = [
        {"key": "mission_record_verified", "label": "Mission record VERIFIED",
         "ok": verified,
         "detail": None if verified else
                   ("No active mission record" if not record else
                    f"Mission upload status is {_text(record.get('upload_status')) or 'unknown'},"
                    f" not VERIFIED")},
        {"key": "readback_hash_match", "label": "Pixhawk readback hash match",
         "ok": bool(vm.get("readback_hash_match")),
         "detail": None if vm.get("readback_hash_match") else
                   ("Pixhawk read-back is unreachable — the route on the flight controller "
                    "cannot be confirmed" if not vm.get("readback_reachable") else
                    "The Pixhawk read-back hash does not match the approved route")},
        {"key": "planning_package", "label": "Planning package stored, usable and consistent",
         "ok": bool(pk.get("stored") and pk.get("usable") and pk.get("consistent")),
         "detail": None if (pk.get("stored") and pk.get("usable") and pk.get("consistent"))
                   else (_text(pk.get("consistency")) or
                         ("No planning package is stored on Scout" if not pk.get("stored")
                          else "The stored planning package is not consistent with the approved "
                               "mission"))},
        {"key": "replanning_ready", "label": "Scout replanning readiness",
         "ok": bool(rd.get("replanning_ready")),
         "detail": None if rd.get("replanning_ready") else
                   "Scout does not report replanning readiness for this mission"},
        {"key": "start_eligibility", "label": "Scout Start eligibility",
         "ok": bool(elig["eligible"]), "detail": elig["reason"],
         "deferred_on_authority": elig["deferred_on_authority"],
         "execution_ready": elig["execution_ready"], "source": elig["source"]},
    ]
    # Scout's own binding/replacement verdict. Added as a CHECK rather than folded into
    # eligibility so the operator sees which of the two refused: a mission that is perfectly
    # well prepared but cannot start because the PREVIOUS run still owns the vehicle is a
    # different situation, with a different remedy, from one that is not prepared.
    binding = binding_view(summary)
    if binding["blocks_new_mission"]:
        checks.append({
            "key": "mission_binding", "label": "Scout mission/package binding",
            "ok": False,
            "detail": binding["message"] or f"Scout reports binding {binding['state']}",
            "binding_state": binding["state"], "conflict_code": binding["conflict_code"]})
    blockers = [f"{c['label']}: {c['detail']}" if c["detail"] else c["label"]
                for c in checks if not c["ok"]]
    return {
        # UNCHANGED and deliberately independent of proof completeness: an unread precondition
        # is not a satisfied one, so `ok` stays false either way and Start stays fail-closed.
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "blockers": blockers,
        "mission_id": mission_id,
        "readiness": rd,
        "start_eligibility": elig,
        "binding": binding,
        **proof_completeness(rd, summary),
    }


def preflight(deps, vid, base, *, fresh=False):
    """Read-only Start preflight: the resolved mission identity plus the five precondition
    checks, computed by the SAME code the Start transaction enforces. Issues no write of any kind.

    NOT A POLLED ENDPOINT, and no longer treated as one. The station calls this ONCE at a moment
    where the answer can have changed — vehicle selection, after a mission upload, after a package
    sync, on reconnect, or from an explicit Refresh — and displays it as INFORMATION. Calling it
    on a refresh interval made a stable vehicle's Start button appear and disappear every few
    seconds, because its Pixhawk read-back evidence is served through a 10 s cache and every tenth
    call therefore paid for a live MAVLink download that could time out. Start availability is
    decided from stable lifecycle facts; the gate that matters is run_start's own fresh proof.

    Evidence here is read through the bounded cache by default (fresh=False): this is a display,
    it labels its own evidence age, and it authorizes nothing. `fresh=True` is for an explicit,
    bounded, read-only re-proof operation (the Full Refresh transaction, mission_full_refresh.py)
    that deliberately wants the SAME live-evidence proof the Start transaction would perform —
    still issuing no write, because start_preconditions/readiness_evidence never do.

    Carries the proof-completeness triple alongside the verdict (see proof_completeness): a
    consumer must be able to tell "the vehicle is not ready" from "this round could not read the
    evidence", and it must be able to tell them apart from the PAYLOAD, not from the wording of
    a blocker. `can_start` itself is unaffected — an incomplete proof never enables Start."""
    mission_id, err = resolve_mission_id(deps, vid, None)
    status = mx.get_status(base)
    summary = mx.summarize_status(status)
    if err is not None:
        code, message = err
        return {
            "ok": False, "mission_id": None, "can_start": False,
            "error_code": code, "error": message,
            "checks": [], "blockers": [message],
            "binding": binding_view(summary),
            "authority_will_be_acquired": False, "execution_ready": False,
            # A DEFINITE answer the operator owns: there is no active persisted mission record.
            # Nothing was left unread, so this is a complete proof of "not ready".
            "proof_complete": True, "readiness_refreshing": False,
            "readiness_reason_code": None, "readiness_reason": None,
            "summary": summary, "authority": deps.get_authority(vid),
        }
    pre = start_preconditions(deps, vid, base, mission_id=mission_id, summary=summary, fresh=fresh)
    return {
        "ok": pre["ok"], "mission_id": mission_id, "can_start": pre["ok"],
        "error_code": None, "error": None,
        "checks": pre["checks"], "blockers": pre["blockers"],
        "readiness": pre["readiness"], "start_eligibility": pre["start_eligibility"],
        "binding": pre["binding"],
        # Surfaced at the top level so the Map card does not have to dig for the ONE distinction
        # that changes what it says: Start is available, and pressing it will take agent control.
        "authority_will_be_acquired": bool(pre["start_eligibility"]["deferred_on_authority"]),
        "execution_ready": bool(pre["start_eligibility"]["execution_ready"]),
        "proof_complete": pre["proof_complete"],
        "readiness_refreshing": pre["readiness_refreshing"],
        "readiness_reason_code": pre["readiness_reason_code"],
        "readiness_reason": pre["readiness_reason"],
        "summary": summary, "authority": deps.get_authority(vid),
    }


# ── Authority (POST, then READ BACK — a POST alone is never a transfer) ────────────────────
def acquire_authority(deps, vid, target):
    """Transfer authority to `target` and VERIFY it by reading it back.

    The read-back is the whole point: Scout's POST reply is an acknowledgement of the request,
    and this station's contract is that authority is whatever a subsequent READ reports. A
    verified:false phase stops the transaction — a Start is never issued on an unverified
    transfer."""
    before = deps.get_authority(vid) or {}
    before_val = _authority_value(before)
    if before_val == target and before.get("reachable") is not False:
        return _phase(PHASE_AUTHORITY, OK,
                      f"Authority is already {target} — no transfer was needed",
                      requested=target, before=before_val, observed=target,
                      verified=True, changed=False)

    post = deps.set_authority(vid, target) or {}
    if not post.get("ok"):
        return _phase(PHASE_AUTHORITY, FAILED,
                      _text(post.get("message") or post.get("error"))
                      or f"Scout did not accept the request for {target} authority",
                      requested=target, before=before_val,
                      observed=_authority_value(post), verified=False, changed=False)

    after = deps.get_authority(vid) or {}
    observed = _authority_value(after)
    verified = observed == target and after.get("reachable") is not False
    return _phase(
        PHASE_AUTHORITY, OK if verified else FAILED,
        f"Authority verified as {target} by read-back" if verified else
        (f"Authority read back as {observed or 'unknown'} after requesting {target} — the "
         f"transfer is NOT verified"),
        requested=target, before=before_val, observed=observed,
        verified=verified, changed=True)


def _authority_value(payload):
    raw = (payload or {}).get("authority")
    return str(raw).upper() if raw else None


# ── Operation execution + reconciliation (shared by all four transactions) ─────────────────
def _run_operation(env, base, operation, fn, *, mission_id=None):
    """Issue ONE Scout write, interpret it, and reconcile an UNKNOWN by READING status.

    Never resends. Returns the interpreted result; the caller decides what the outcome means
    for authority."""
    result = mx.interpret_operation(fn(base))
    outcome = result.get("operational_outcome")
    reconciliation = None
    if outcome == mx.OUTCOME_UNKNOWN:
        reconciliation = mx.reconcile(base, operation, expected_mission_id=mission_id)
        result["reconciliation"] = reconciliation

    # Merge Scout's FULL interpreted result into the envelope. The transaction is a superset of
    # the raw proxy response, not a lossy summary of it: `operational_outcome`, Scout's body
    # under `scout`, `home_result`, `sequence`, `route_hash`, `reachable` and the rest stay
    # exactly where every existing consumer already reads them, and the phases are added on top.
    # The envelope's OWN fields are re-applied afterwards so a transport-level key can never
    # clobber the operation identity or the outcome the operator is shown.
    owned = {k: env[k] for k in ("operation", "vehicle_id", "mission_id", "requested_mission_id",
                                 "phases", "authority")}
    env.update(result)
    env.update(owned)

    env["outcome"] = outcome
    env["ok"] = outcome == mx.OUTCOME_ACCEPTED
    env["scout_error_message"] = _text(result.get("scout_error_message"))
    env["supported"] = result.get("supported", True)
    env["reconciliation"] = reconciliation
    env["resulting_state"] = result.get("current_state")
    env["error"] = _text(result.get("error"))
    env["phases"].append(_phase(
        PHASE_OPERATION, OK if env["ok"] else FAILED,
        _text(result.get("scout_error_message") or result.get("error"))
        or (f"Scout accepted the {operation}" if env["ok"] else
            f"Scout {operation}: {outcome}"),
        outcome=outcome, http_status=result.get("http_status"),
        scout_error_code=result.get("scout_error_code"),
        resulting_state=result.get("current_state"),
        verified_mode=result.get("verified_mode"),
        reconciliation=reconciliation))
    return result


# ── START ─────────────────────────────────────────────────────────────────────────────────
def run_start(deps, vid, base, vid_slug, *, supplied_mission_id=None):
    """The Start transaction: resolve identity -> require evidence -> transfer and VERIFY
    LOCAL_AGENT -> Scout Start -> reconcile -> restore OPERATOR only if provably safe."""
    env = _envelope("start", vid_slug)
    env["authority"]["required"] = AUTHORITY_LOCAL_AGENT

    mission_id, err = resolve_mission_id(deps, vid, supplied_mission_id)
    if err is not None:
        code, message = err
        env["requested_mission_id"] = supplied_mission_id
        return _blocked(env, code, message, phase=PHASE_MISSION)
    env["mission_id"] = env["requested_mission_id"] = mission_id
    env["phases"].append(_phase(PHASE_MISSION, OK,
                                f"Active persisted mission {mission_id}", mission_id=mission_id))

    status = mx.get_status(base)
    summary = mx.summarize_status(status)
    if not summary.get("supported"):
        env["supported"] = False
        return _blocked(env, "MISSION_EXECUTION_UNSUPPORTED",
                        "This Scout does not implement the mission-execution lifecycle")
    # THE AUTHORITATIVE PROOF, and the reason the station no longer polls a copy of it. Computed
    # here, at Start time, from a FRESH read: a live Pixhawk mission download, a live planning
    # package read and a live canonical status. Nothing below this line runs unless every one of
    # the five conditions passes, and NO vehicle write — not the authority hand-off, not Scout's
    # Start — happens before it.
    pre = start_preconditions(deps, vid, base, mission_id=mission_id, summary=summary, fresh=True)
    env["preconditions"] = pre["checks"]
    env["blockers"] = pre["blockers"]
    env["proof_complete"] = pre["proof_complete"]
    env["readiness_reason_code"] = pre["readiness_reason_code"]
    if not pre["ok"]:
        # FAIL-CLOSED, and the lifecycle is left exactly where it was resting: `blocked` means the
        # Operator refused before Scout was contacted, so no vehicle write of any kind was issued
        # and no state was moved. An incomplete proof lands here too — an unread precondition is
        # not a satisfied one.
        return _blocked(env, "START_PRECONDITIONS_NOT_MET",
                        "Start preconditions are not met: " + "; ".join(pre["blockers"]),
                        checks=pre["checks"])
    env["phases"].append(_phase(PHASE_PRECONDITIONS, OK,
                                "Mission record, Pixhawk read-back, planning package, "
                                "replanning readiness and Scout Start eligibility all confirmed "
                                "from a fresh read",
                                checks=pre["checks"], fresh=True))

    auth = acquire_authority(deps, vid, AUTHORITY_LOCAL_AGENT)
    env["phases"].append(auth)
    env["authority"]["before"] = auth.get("before")
    env["authority"]["after"] = auth.get("observed")
    env["authority"]["verified"] = auth.get("verified")
    if not auth.get("verified"):
        # Nothing was sent to Scout's Start route. The vehicle is untouched, and the operator
        # is told exactly which half of the transaction failed.
        env["outcome"] = OUTCOME_BLOCKED
        env["ok"] = False
        env["error_code"] = "AUTHORITY_NOT_VERIFIED"
        env["error"] = (auth.get("detail")
                        or "Control authority could not be verified as LOCAL_AGENT — Scout Start "
                           "was not contacted")
        return env

    result = _run_operation(env, base, "start", lambda b: mx.post_start(b, mission_id),
                            mission_id=mission_id)
    env["phases"].append(_restore_operator_if_proven_safe(deps, vid, base, result))
    return env


def _restore_operator_if_proven_safe(deps, vid, base, result):
    """Return authority to OPERATOR after a failed Start — ONLY on proof, never on a guess.

    The proof has two independent parts, and BOTH must hold:
      (a) the failure is a definite PRE-ACTION refusal (a 409 rejection, or a Scout error code
          raised before it commanded anything). A post-command failure — LOITER_NOT_VERIFIED,
          SET_HOME_FAILED, PACKAGE_SYNC_FAILED, AUTO_NOT_VERIFIED, PROGRESSION_UNCONFIRMED —
          leaves the vehicle in a state only Scout can describe, so authority stays put;
      (b) a canonical status read shows Scout RESTING in a pre-start state, with no active
          operation, not mid-transaction, and with replanning inactive.

    An UNKNOWN outcome fails (a) by construction: we do not know whether the Start took effect,
    and taking authority back from a vehicle that may be running a mission is exactly the class
    of "helpful" guess that gets hardware hurt."""
    outcome = result.get("operational_outcome")
    if outcome == mx.OUTCOME_ACCEPTED:
        return _phase(PHASE_RESTORE, SKIPPED,
                      "Start accepted — LOCAL_AGENT authority is retained for the run",
                      restored=False)
    if outcome == mx.OUTCOME_UNKNOWN:
        return _phase(PHASE_RESTORE, WITHHELD,
                      "The Start outcome is UNKNOWN — authority is never taken back on an "
                      "unconfirmed operation. Use Take Control explicitly if you need the wheel.",
                      restored=False)
    if outcome in (mx.OUTCOME_UNAVAILABLE, mx.OUTCOME_UNSUPPORTED):
        return _phase(PHASE_RESTORE, WITHHELD,
                      "Scout could not be reached or does not support this route — the vehicle "
                      "state is unknown, so authority is left as it is.",
                      restored=False)

    code = (result.get("scout_error_code") or "").upper()
    if outcome == mx.OUTCOME_FAILED and code not in mx.PRE_ACTION_ERROR_CODES:
        return _phase(PHASE_RESTORE, WITHHELD,
                      f"Scout reported {code or 'a vehicle-level failure'} after it began the "
                      f"start transaction — the vehicle state is Scout's to describe, so "
                      f"authority is not taken back automatically.",
                      restored=False)

    summary = mx.summarize_status(mx.get_status(base))
    if not summary.get("present"):
        return _phase(PHASE_RESTORE, WITHHELD,
                      "Scout status could not be read after the failed start — authority is "
                      "left as it is rather than changed on an assumption.",
                      restored=False)
    state = (summary.get("state") or "").upper()
    if summary.get("active_operation_id") or state in mx.IN_TRANSACTION_STATES:
        return _phase(PHASE_RESTORE, WITHHELD,
                      f"Scout is still processing ({state or 'transitional'}) — authority stays "
                      f"with the Local Agent.", restored=False)
    if summary.get("replanning_active"):
        return _phase(PHASE_RESTORE, WITHHELD,
                      "The replanning controller owns the vehicle — authority stays with the "
                      "Local Agent.", restored=False)
    if state not in mx.PRE_START_STATES:
        return _phase(PHASE_RESTORE, WITHHELD,
                      f"Scout reports {state} — the run is not in a pre-start resting state, so "
                      f"authority is not taken back automatically.", restored=False)

    restored = acquire_authority(deps, vid, AUTHORITY_OPERATOR)
    return _phase(PHASE_RESTORE, restored["status"],
                  ("Start failed before Scout commanded the vehicle and Scout is resting in "
                   f"{state} — OPERATOR authority restored."
                   if restored["status"] == OK else restored.get("detail")),
                  restored=restored["status"] == OK, observed=restored.get("observed"),
                  proof={"state": state, "active_operation_id": None,
                         "scout_error_code": code or None, "outcome": outcome})


# ── PAUSE ─────────────────────────────────────────────────────────────────────────────────
def run_pause(deps, vid, base, vid_slug):
    """Pause: authority is UNTOUCHED (the mission is still the Local Agent's to run), Scout
    performs the pause transaction, and the result is verified against canonical status —
    PAUSED with a verified LOITER, or an explicit "not verified" the operator can see."""
    env = _envelope("pause", vid_slug)
    env["authority"]["required"] = AUTHORITY_LOCAL_AGENT
    before = deps.get_authority(vid) or {}
    env["authority"]["before"] = env["authority"]["after"] = _authority_value(before)
    env["phases"].append(_phase(
        PHASE_AUTHORITY, SKIPPED,
        "Pause holds the mission — authority stays with the Local Agent",
        requested=None, observed=_authority_value(before), verified=None, changed=False))

    result = _run_operation(env, base, "pause", mx.post_pause)
    env["phases"].append(_verify_state(base, result, expected=("PAUSED",),
                                       expected_mode="LOITER", operation="pause"))
    return env


# ── RESUME ────────────────────────────────────────────────────────────────────────────────
def run_resume(deps, vid, base, vid_slug):
    """Resume: verify authority is STILL LOCAL_AGENT and re-acquire it only if it is not, then
    let Scout run its resume transaction."""
    env = _envelope("resume", vid_slug)
    env["authority"]["required"] = AUTHORITY_LOCAL_AGENT

    auth = acquire_authority(deps, vid, AUTHORITY_LOCAL_AGENT)
    env["phases"].append(auth)
    env["authority"]["before"] = auth.get("before")
    env["authority"]["after"] = auth.get("observed")
    env["authority"]["verified"] = auth.get("verified")
    if not auth.get("verified"):
        env["outcome"] = OUTCOME_BLOCKED
        env["ok"] = False
        env["error_code"] = "AUTHORITY_NOT_VERIFIED"
        env["error"] = (auth.get("detail")
                        or "Control authority could not be verified as LOCAL_AGENT — Scout "
                           "Resume was not contacted")
        return env

    result = _run_operation(env, base, "resume", mx.post_resume)
    env["phases"].append(_verify_state(base, result, expected=("RUNNING",),
                                       expected_mode="AUTO", operation="resume"))
    return env


def _verify_state(base, result, *, expected, expected_mode, operation):
    """Confirm an accepted operation against Scout's CANONICAL status rather than the body it
    just returned. Reports verified true/false with what was actually observed; it never
    downgrades Scout's own accepted verdict to a failure, and never rounds an unverified
    observation up to a success."""
    if result.get("operational_outcome") != mx.OUTCOME_ACCEPTED:
        return _phase(PHASE_VERIFY, SKIPPED,
                      f"The {operation} was not accepted — there is nothing to verify",
                      verified=None)
    summary = mx.summarize_status(mx.get_status(base))
    if not summary.get("present"):
        return _phase(PHASE_VERIFY, WITHHELD,
                      f"Scout accepted the {operation} but its status could not be read back — "
                      f"the resulting state is unconfirmed",
                      verified=False, observed_state=None, observed_mode=None)
    state = (summary.get("state") or "").upper()
    mode = (summary.get("mode") or summary.get("verified_mode") or "").upper()
    ok = state in expected and (not expected_mode or mode == expected_mode)
    return _phase(
        PHASE_VERIFY, OK if ok else WITHHELD,
        (f"Scout reports {state} in mode {mode or 'unreported'} — verified"
         if ok else
         f"Scout accepted the {operation} but reports {state or 'no state'} in mode "
         f"{mode or 'unreported'}, not {'/'.join(expected)} in {expected_mode}"),
        verified=ok, observed_state=state or None, observed_mode=mode or None,
        expected_state=list(expected), expected_mode=expected_mode)


# ── STOP ──────────────────────────────────────────────────────────────────────────────────
# SCOUT OWNS THE WHOLE STOP TRANSACTION. It is a SAFE ABORT, not a raw Pixhawk stop and not a
# mission deletion: verified LOITER → verify the active mission identity → restore the immutable
# original mission if a verified revised route is installed → rewind the original to its start →
# verify the rewind → reset mission-execution / replan / test state → clear the simulated
# experiment injection → invalidate the prior runtime Home → return supervisory authority to
# OPERATOR → re-prove the mission evidence.
#
# EVERYTHING THIS OPERATOR-SIDE TRANSACTION DOES is: forward the intent with the active persisted
# mission id, re-read canonical status, and report Scout's own evidence. It sends NO LOITER, NO
# mission upload, NO rewind, NO replan reset, NO experiment clear, NO rearm and — the change from
# the previous contract — NO authority write. Scout hands authority back itself; the Operator
# OBSERVES that hand-off by read-back and says plainly when it cannot confirm it.
def run_stop(deps, vid, base, vid_slug):
    """Stop: ONE Scout transaction, forwarded and evidenced. The Operator reimplements no part
    of the sequence and writes no authority of its own."""
    env = _envelope("stop", vid_slug)
    # Stop RETURNS authority; it does not require the Operator to hold or take any. The required
    # value is stated as the END state so the response reads honestly rather than claiming the
    # Local Agent must keep the wheel through an abort.
    env["authority"]["required"] = AUTHORITY_OPERATOR
    mission_id = deps.active_mission_id(vid)
    env["mission_id"] = env["requested_mission_id"] = mission_id
    before = deps.get_authority(vid) or {}
    env["authority"]["before"] = env["authority"]["after"] = _authority_value(before)
    env["stop"] = mx.stop_evidence({})

    result = _run_operation(env, base, "stop", lambda b: mx.post_stop(b, mission_id),
                            mission_id=mission_id)
    # Scout's evidence from the operation body, when it carried any. The verification phase below
    # replaces it with the canonical status' block whenever that one is reported.
    if isinstance(result.get("stop"), dict) and result["stop"].get("reported"):
        env["stop"] = result["stop"]

    if result.get("operational_outcome") == mx.OUTCOME_UNSUPPORTED:
        env["supported"] = False
        # Replace the transport's generic "route not implemented" with the operator-facing
        # sentence: what is missing, and — just as importantly — what is NOT an acceptable
        # substitute for it. The generic string invites someone to improvise one.
        env["error"] = ("This Scout does not implement POST /agent/mission_execution/stop. "
                        "Stop is unavailable — it is not emulated from a low-level LOITER, the "
                        "raw Pixhawk stop is not offered, and Rearm is not a substitute. Pause "
                        "holds the mission without ending it.")
        env["error_code"] = "STOP_NOT_SUPPORTED"
        env["phases"].append(_phase(PHASE_VERIFY, SKIPPED,
                                    "Nothing was stopped — there is no evidence to verify",
                                    verified=None))
        env["phases"].append(_phase(PHASE_RESTORE, SKIPPED,
                                    "No authority change — nothing was stopped", restored=False))
        return env

    verify, evidence = _verify_stop(base, result)
    if evidence.get("reported"):
        env["stop"] = evidence
    env["phases"].append(verify)
    env["phases"].append(_observe_authority_after_stop(deps, vid, result, evidence, verify))
    env["authority"]["after"] = (env["phases"][-1].get("observed")
                                 or env["authority"]["after"])
    return env


def _verify_stop(base, result):
    """Re-read Scout's CANONICAL status after an accepted Stop and report its own evidence.

    Returns (phase, evidence). This phase VERIFIES; it never repairs. If the rewind was not
    verified, or the original mission could not be restored, that is reported exactly as Scout
    stated it — the Operator does not follow it with a Rearm, a Resume, an AUTO, a re-upload or
    a second Stop, because each of those would be this station inventing a recovery for a vehicle
    whose state only Scout can describe.

    A successful Stop normally rests in NOT_READY with start_eligible / authority_blocks_start
    true. That is the EXPECTED landing, not a failure, and is reported as such."""
    outcome = result.get("operational_outcome")
    empty = mx.stop_evidence({})
    if outcome not in (mx.OUTCOME_ACCEPTED, mx.OUTCOME_UNKNOWN):
        return (_phase(PHASE_VERIFY, SKIPPED,
                       "The stop was not accepted — there is nothing to verify", verified=None),
                empty)

    summary = mx.summarize_status(mx.get_status(base))
    if not summary.get("present"):
        return (_phase(PHASE_VERIFY, WITHHELD,
                       "Scout accepted the stop but its status could not be read back — the "
                       "reset is UNCONFIRMED and nothing about it is assumed",
                       verified=False, observed_state=None, observed_mode=None), empty)

    ev = summary.get("stop") or empty
    state = (summary.get("state") or "").upper()
    mode = (summary.get("mode") or summary.get("verified_mode") or "").upper()
    held = ev.get("hold_verified") is True or mode == "LOITER"
    proven = [k for k in ("hold_verified", "rewind_verified", "replan_reset",
                          "experiment_cleared") if ev.get(k) is True]
    gaps = [k for k in ("hold_verified", "rewind_verified") if ev.get(k) is False]

    # SUSPENDED is Scout's documented FAILURE landing for a stop that got past the safe hold.
    # The vehicle is being held; the reset is incomplete; the operator sees Scout's own code.
    if state == "SUSPENDED":
        # Scout's CODE first, then its message. `_text` on a {code, message} prefers the prose,
        # and the code is the part an operator quotes, searches for and acts on — losing it
        # turns a specific failure (STOP_REWIND_NOT_VERIFIED) into an anonymous sentence.
        code = _error_code(summary.get("last_error")) or _text(ev.get("outcome"))
        message = _text(summary.get("last_error"))
        why = " — ".join(dict.fromkeys(p for p in (code, message) if p)) \
            or "the reset did not complete"
        return (_phase(PHASE_VERIFY, FAILED,
                       f"Scout reports SUSPENDED after the stop: {why}"
                       + (" — the vehicle is being HELD in LOITER and the reset is incomplete"
                          if held else ""),
                       verified=False, observed_state=state, observed_mode=mode or None,
                       held_in_loiter=held, scout_error_code=code, stop=ev), ev)

    ok = (ev.get("ready_for_start") is True
          or mx._is_success_outcome(ev.get("outcome"))
          or (state in mx.PRE_START_STATES and not gaps))
    detail = (f"Scout reports {state or 'no state'} in mode {mode or 'unreported'}"
              + (f" — {', '.join(proven)}" if proven else "")
              + ("; a NEW Start is eligible (authority is back with the OPERATOR)"
                 if summary.get("start_eligible") is True else ""))
    if not ok:
        detail = (f"Scout accepted the stop but reports {state or 'no state'} in mode "
                  f"{mode or 'unreported'} — the reset is not confirmed"
                  + (f" ({', '.join(gaps)} = false)" if gaps else ""))
    return (_phase(PHASE_VERIFY, OK if ok else WITHHELD, detail,
                   verified=ok, observed_state=state or None, observed_mode=mode or None,
                   held_in_loiter=held, start_eligible=summary.get("start_eligible"),
                   authority_blocks_start=summary.get("authority_blocks_start"), stop=ev), ev)


def _observe_authority_after_stop(deps, vid, result, evidence, verify):
    """OBSERVE — never perform — the authority hand-off Scout's Stop transaction makes itself.

    Scout returns supervisory authority to OPERATOR as one of its own steps, so this phase reads
    authority back and reports what it found. It issues NO authority write: doing so would be the
    Operator reimplementing a step of Scout's transaction, and a hand-off this station performed
    would prove nothing about the one Scout was supposed to make. When the read-back disagrees
    with Scout's `authority_after`, that disagreement is stated rather than papered over — Take
    Control remains the operator's explicit manual override."""
    outcome = result.get("operational_outcome")
    if outcome not in (mx.OUTCOME_ACCEPTED, mx.OUTCOME_UNKNOWN):
        return _phase(PHASE_RESTORE, SKIPPED,
                      "The stop was not accepted — authority is unchanged",
                      restored=False, observed=_authority_value(deps.get_authority(vid) or {}))

    observed = _authority_value(deps.get_authority(vid) or {})
    claimed = (evidence.get("authority_after") or "").upper() or None
    if observed == AUTHORITY_OPERATOR:
        return _phase(PHASE_RESTORE, OK,
                      "Scout returned supervisory authority to the OPERATOR as part of its stop "
                      "transaction; the Operator read it back and confirmed it. The next Start "
                      "hands authority to the Local Agent again.",
                      restored=True, observed=observed, claimed=claimed, written=False)
    if claimed == AUTHORITY_OPERATOR:
        return _phase(PHASE_RESTORE, WITHHELD,
                      f"Scout reports it returned authority to OPERATOR, but the read-back says "
                      f"{observed or 'unknown'}. The Operator does not write authority as part of "
                      f"a stop — use Take Control explicitly if you need the wheel.",
                      restored=False, observed=observed, claimed=claimed, written=False)
    return _phase(PHASE_RESTORE, WITHHELD,
                  f"Authority reads back as {observed or 'unknown'} after the stop. Scout owns "
                  f"the hand-off and the Operator does not perform it — use Take Control "
                  f"explicitly if you need the wheel.",
                  restored=False, observed=observed, claimed=claimed, written=False)


# ── HTTP status for a transaction envelope ────────────────────────────────────────────────
def status_code(env):
    """The honest HTTP status. `blocked` is 409: the OPERATOR refused it, definitively, and
    nothing reached the vehicle. Everything else mirrors the mission-execution outcome model,
    including 202 for an UNKNOWN that must be reconciled rather than retried."""
    outcome = env.get("outcome")
    if outcome == OUTCOME_BLOCKED:
        return 409
    if outcome == mx.OUTCOME_UNSUPPORTED:
        return 200
    if outcome == mx.OUTCOME_UNAVAILABLE:
        return 503
    if outcome == mx.OUTCOME_UNKNOWN:
        return 202
    if outcome == mx.OUTCOME_REJECTED:
        return 409 if env.get("http_status") == 409 else 400
    return 200
