"""Startup / reconnect reconciliation: which APPROVED mission is the vehicle actually carrying?

WHY THIS MODULE EXISTS
----------------------
The Operator restores two things from disk: the immutable approved mission records, and one
pointer per vehicle saying which of them is active. Nothing then ever re-examined that pointer.
So a station that came back up with a pointer at a SUPERSEDED record compared every piece of
fresh Scout/Pixhawk evidence against the wrong mission and reported

    "Agent package does not match the approved mission"

on a vehicle that was carrying an approved, persisted, verified mission the whole time — one
whose record was sitting in the very same store, one canonical route hash away. The only way
out was Plan → Finish & upload, which mints a NEW mission id and REWRITES the flight
controller's mission with content that was already on it. Re-uploading a mission to repair a
bookkeeping error is the most invasive possible fix for the least dangerous possible fault.

This module is the missing step. On fresh evidence it asks one question —

    which APPROVED record's canonical route is the one the flight controller reports right now?

— and repairs the OPERATOR'S OWN durable bookkeeping to agree with the answer.

WHAT IT MAY DO (all operator-local; none of it touches the vehicle)
------------------------------------------------------------------
  1. move the active-mission pointer to an already-approved record for the SAME vehicle whose
     canonical route hash is the one the flight controller reports;
  2. re-derive `upload_status: VERIFIED` for that record, because a live complete read-back
     whose route hash equals the approved hash IS the read-back proof — a fresher one than the
     historical upload's, in fact. This is what recovers a mission whose MISSION_UPLOAD result
     landed after a restart, when the in-memory command queue that would have projected it was
     already gone;
  3. recompute `package_sync_state` from LIVE Scout evidence, so a restored "a sync is owed"
     that Scout itself now contradicts stops being reported, and a restored SYNCED that Scout
     contradicts starts being reported.

WHAT IT MUST NEVER DO
---------------------
NO VEHICLE COMMAND, AND NO MISSION UPLOAD. There is no code path from here to the command queue,
to MISSION_UPLOAD, or to any Scout write. It reads evidence and edits the operator's own records.
That is what makes "startup does not re-upload the mission" a structural property rather than a
promise (tests/test_mission_reconcile.py asserts it against a Deps whose upload hooks raise).

NO SILENT ADOPTION. A route hash that NO approved record for this vehicle carries is never
adopted, never approved, and never made active. It is reported as UNAPPROVED_MISSION / MISMATCH
and left for the operator. Recovery here is only ever RE-IDENTIFICATION of something the
operator already approved and the store already holds.

NO VERDICT WITHOUT EVIDENCE. An unreachable, partial or hash-less read-back yields RECONCILING
and changes nothing. Declaring a mismatch from evidence you do not have is the failure mode this
module exists to remove, so it is not permitted to commit one of its own.

PER VEHICLE, ALWAYS. Every lookup is filtered by vehicle id and every candidate record is
re-checked for ownership here, not merely trusted from the caller's index.

IDENTITY: RECORD vs CONTENT
---------------------------
`mission_id` is the identity of a lifecycle RECORD. `route_hash` (mission-contract-v1
`route_content_hash`) is the identity of the mission CONTENT. Two records may legitimately
carry the same canonical route. Reconciliation therefore keys on the CONTENT hash and treats
mission ids as labels to be brought into agreement afterwards — never as evidence that the
routes differ.
"""
from __future__ import annotations

from datetime import datetime, timezone

import mission_contract


# ── Outcomes ──────────────────────────────────────────────────────────────────────────────
# Deliberately distinct. "I could not tell", "the labels disagree but the route does not" and
# "the vehicle is carrying something else entirely" are three different statements, and the
# whole defect this module fixes came from a UI that could only say the last one.
RECONCILING = "RECONCILING"                       # insufficient fresh evidence — no verdict yet
SYNCHRONIZED = "SYNCHRONIZED"                     # record == flight controller == Agent package
PACKAGE_SYNC_REQUIRED = "PACKAGE_SYNC_REQUIRED"   # record == flight controller; package does not
UNAPPROVED_MISSION = "UNAPPROVED_MISSION"         # the FC carries a route nothing approved matches
MISMATCH = "MISMATCH"                             # ditto, and an approved active record disagrees

# Reason codes (machine-readable; the UI renders its own prose from these).
NO_READBACK = "NO_READBACK"
READBACK_PARTIAL = "READBACK_PARTIAL"
READBACK_HASH_UNAVAILABLE = "READBACK_HASH_UNAVAILABLE"
READBACK_STALE = "READBACK_STALE"
NO_APPROVED_RECORD = "NO_APPROVED_RECORD"
NO_APPROVED_MATCH = "NO_APPROVED_MATCH"
PACKAGE_UNREACHABLE = "PACKAGE_UNREACHABLE"
PACKAGE_NOT_STORED = "PACKAGE_NOT_STORED"
PACKAGE_IDENTITY_MISMATCH = "PACKAGE_IDENTITY_MISMATCH"
PACKAGE_INCOMPLETE_EVIDENCE = "PACKAGE_INCOMPLETE_EVIDENCE"
UPLOAD_FAILED_RECORDED = "UPLOAD_FAILED_RECORDED"
ALREADY_CONSISTENT = "ALREADY_CONSISTENT"

# Actions, for the trace.
ACTION_REBIND = "REBOUND_ACTIVE_MISSION"
ACTION_VERIFY = "UPLOAD_STATUS_VERIFIED"
ACTION_SYNC_STATE = "PACKAGE_SYNC_STATE"

SYNC_STATE_REQUIRED = "REQUIRED"
SYNC_STATE_SYNCED = "SYNCED"

# How old a read-back may be and still serve as the proof that promotes a record to VERIFIED.
# The polling read-back cache is age-labelled and bounded (main.PIXHAWK_READBACK_TTL_S); this
# bound is stated here so the proof rule does not silently inherit a tuning change made for
# polling cost. Older than this and the module answers RECONCILING rather than deciding on
# evidence about the past.
MAX_PROOF_AGE_S = 10.0

# Upload statuses a live read-back proof may promote. FAILED is deliberately absent: a recorded
# upload failure is an operator-visible fact about a write that did not complete, and a later
# observation that the route is nevertheless present does not explain it. That case is reported,
# not repaired.
PROMOTABLE_UPLOAD_STATUSES = ("QUEUED", "ACCEPTED")


class Deps:
    """Everything reconciliation touches in the operator backend, injected.

    vehicle_records(vid)      -> every immutable approved mission record for THIS vehicle
    active_mission_id(vid)    -> the vehicle's active mission id, or None
    set_active(vid, mid)      -> move the durable active pointer for THIS vehicle
    persist()                 -> write the durable mission-store snapshot
    """

    def __init__(self, *, vehicle_records, active_mission_id, set_active, persist):
        self.vehicle_records = vehicle_records
        self.active_mission_id = active_mission_id
        self.set_active = set_active
        self.persist = persist


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _route_count(rec):
    return len(rec.get("route_waypoints") or [])


def _verdict(outcome, reason, detail, **extra):
    out = {
        "outcome": outcome,
        "conclusive": outcome != RECONCILING,
        "reason": reason,
        "detail": detail,
        "generated_at": _now_iso(),
        "actions": [],
        "active_mission_id": None,
        "active_route_hash": None,
        "rebound": False,
        "evidence": {},
    }
    out.update(extra)
    return out


def readback_facts(readback):
    """(usable, facts) from a normalized Pixhawk read-back.

    `usable` is True only for a read-back that can carry a proof: reachable, not partial, and
    carrying a route content hash, at an age within MAX_PROOF_AGE_S. Everything else is an
    absence of evidence, and this function says so rather than substituting a default."""
    rb = readback if isinstance(readback, dict) else None
    facts = {
        "reachable": bool(rb and rb.get("reachable")),
        "partial": bool(rb.get("partial")) if rb else None,
        "route_hash": (_str(rb.get("route_content_hash")) if rb else None),
        "route_count": (_as_int(rb.get("route_waypoint_count")) if rb else None),
        "item_count": (_as_int(rb.get("pixhawk_item_count")) if rb else None),
        "age_s": (rb.get("evidence_age_s") if rb else None),
        "cached": (rb.get("evidence_cached") if rb else None),
    }
    if rb is None or not facts["reachable"]:
        return False, facts
    if facts["partial"]:
        return False, facts
    if not facts["route_hash"]:
        return False, facts
    age = facts["age_s"]
    if isinstance(age, (int, float)) and age > MAX_PROOF_AGE_S:
        return False, facts
    return True, facts


def package_facts(evidence, *, reachable):
    """The Agent planning package's reported identity, tri-state throughout. A field Scout did
    not report is None — an unavailable comparison, never an agreement and never a difference."""
    ev = evidence if isinstance(evidence, dict) else {}
    return {
        "reachable": bool(reachable),
        "stored": ev.get("stored"),
        "usable": ev.get("usable"),
        "mission_id": _str(ev.get("mission_id")),
        "route_hash": _str(ev.get("route_hash")),
        "route_count": _as_int(ev.get("route_count")),
    }


def _choose(matches, active_id):
    """Which of several approved records carrying the SAME canonical route becomes active.

    Deterministic, and biased towards changing nothing: the currently active record wins if it
    is among them, so a healthy station never churns its pointer. Otherwise the most recently
    created record wins, with the mission id as the final tie-break so the choice cannot depend
    on dict ordering."""
    for rec in matches:
        if rec.get("mission_id") == active_id:
            return rec
    return sorted(matches,
                  key=lambda r: (str(r.get("created_at") or ""), str(r.get("mission_id") or "")),
                  reverse=True)[0]


def reconcile(deps, vid, *, readback, package_evidence=None, package_reachable=False):
    """Reconcile one vehicle's persisted approved missions against fresh observed state.

    Returns a verdict dict (see the outcome constants). Writes only the operator's own durable
    records, and only when fresh evidence PROVES the change. Issues no vehicle command and
    performs no mission upload — there is no code here that could.
    """
    records = [r for r in (deps.vehicle_records(vid) or [])
               if isinstance(r, dict) and r.get("vehicle_id") == vid]
    active_id_before = deps.active_mission_id(vid)
    usable, pix = readback_facts(readback)
    pkg = package_facts(package_evidence, reachable=package_reachable)
    evidence = {
        "pixhawk": pix,
        "package": pkg,
        "active_mission_id_before": active_id_before,
        "approved_record_count": len(records),
        "candidates": [{"mission_id": r.get("mission_id"), "route_hash": r.get("route_hash"),
                        "upload_status": r.get("upload_status"),
                        "package_sync_state": r.get("package_sync_state")}
                       for r in records],
    }

    # ── Case F: not enough fresh evidence. Decide NOTHING, change NOTHING. ────────────────
    if not usable:
        if not pix["reachable"]:
            reason, detail = NO_READBACK, ("The flight controller mission read-back is "
                                           "unreachable, so which mission it carries is unknown.")
        elif pix["partial"]:
            reason, detail = READBACK_PARTIAL, ("The mission read-back is incomplete and proves "
                                                "nothing about the route on the flight controller.")
        elif not pix["route_hash"]:
            reason, detail = READBACK_HASH_UNAVAILABLE, ("The read-back reported no route content "
                                                         "hash, so the routes cannot be compared.")
        else:
            reason, detail = READBACK_STALE, ("The read-back evidence is older than "
                                              f"{MAX_PROOF_AGE_S:g}s — evidence about the past.")
        return _verdict(RECONCILING, reason, detail,
                        active_mission_id=active_id_before, evidence=evidence)

    # ── Which APPROVED record is the flight controller carrying? ──────────────────────────
    # Keyed on CONTENT, never on the record label. Two records with different mission ids and
    # the same canonical route are the same mission content, and this is where that is honoured.
    matches = [r for r in records
               if _str(r.get("route_hash")) and _str(r.get("route_hash")) == pix["route_hash"]]

    if not matches:
        # Nothing the operator approved for this vehicle describes what is on the flight
        # controller. It is NOT adopted, NOT approved and NOT made active — whatever it is, the
        # operator has to say so. This is the one outcome that genuinely needs a new upload.
        outcome = MISMATCH if active_id_before else UNAPPROVED_MISSION
        reason = NO_APPROVED_MATCH if records else NO_APPROVED_RECORD
        detail = ("The route on the flight controller does not match any mission approved for "
                  "this vehicle. It will not be adopted automatically.")
        return _verdict(outcome, reason, detail,
                        active_mission_id=active_id_before,
                        active_route_hash=next((r.get("route_hash") for r in records
                                                if r.get("mission_id") == active_id_before), None),
                        evidence=evidence)

    chosen = _choose(matches, active_id_before)
    mid = chosen.get("mission_id")
    actions = []
    changed = False

    # ── Action 1: re-point the active mission at the record actually on the vehicle ───────
    if mid != active_id_before:
        deps.set_active(vid, mid)
        actions.append({"action": ACTION_REBIND, "from": active_id_before, "to": mid,
                        "route_hash": chosen.get("route_hash"),
                        "detail": "An already-approved record for this vehicle carries the exact "
                                  "canonical route the flight controller reports."})
        changed = True

    # ── Action 2: re-derive VERIFIED from the live read-back ──────────────────────────────
    # Same proof, fresher source. Guarded by a recomputation of the record's own hash so a
    # record whose stored hash no longer describes its waypoints can never be promoted.
    upload_status = chosen.get("upload_status")
    self_consistent = (_str(chosen.get("route_hash"))
                       == mission_contract.route_content_hash(chosen.get("route_waypoints") or []))
    if upload_status in PROMOTABLE_UPLOAD_STATUSES and self_consistent:
        chosen["upload_status"] = "VERIFIED"
        chosen["verified_at"] = chosen.get("verified_at") or _now_iso()
        chosen["verified_by"] = "RECONCILED_READBACK"
        actions.append({"action": ACTION_VERIFY, "mission_id": mid, "from": upload_status,
                        "detail": "A live, complete read-back reports this record's exact "
                                  "canonical route on the flight controller."})
        changed = True
        upload_status = "VERIFIED"

    # ── Action 3: recompute package_sync_state from LIVE Agent evidence ───────────────────
    expected_count = _route_count(chosen)
    sync_before = chosen.get("package_sync_state")
    package_proven = (pkg["stored"] is True and pkg["usable"] is not False
                      and pkg["mission_id"] is not None and pkg["mission_id"] == mid
                      and pkg["route_hash"] is not None and pkg["route_hash"] == chosen.get("route_hash")
                      and pkg["route_count"] is not None and pkg["route_count"] == expected_count)
    package_contradicted = bool(
        pkg["reachable"] and (
            pkg["stored"] is False
            or (pkg["mission_id"] is not None and pkg["mission_id"] != mid)
            or (pkg["route_hash"] is not None and pkg["route_hash"] != chosen.get("route_hash"))
            or (pkg["route_count"] is not None and pkg["route_count"] != expected_count)))

    if package_proven:
        if sync_before != SYNC_STATE_SYNCED:
            chosen["package_sync_state"] = SYNC_STATE_SYNCED
            chosen["package_sync_error"] = None
            chosen["package_synced_at"] = _now_iso()
            actions.append({"action": ACTION_SYNC_STATE, "mission_id": mid,
                            "from": sync_before, "to": SYNC_STATE_SYNCED,
                            "detail": "Scout's stored package proves the same mission id, route "
                                      "hash and route waypoint count."})
            changed = True
    elif package_contradicted:
        code = (PACKAGE_NOT_STORED if pkg["stored"] is False else PACKAGE_IDENTITY_MISMATCH)
        if sync_before != SYNC_STATE_REQUIRED or chosen.get("package_sync_error") != code:
            chosen["package_sync_state"] = SYNC_STATE_REQUIRED
            chosen["package_sync_error"] = code
            chosen["package_sync_attempted_at"] = _now_iso()
            actions.append({"action": ACTION_SYNC_STATE, "mission_id": mid,
                            "from": sync_before, "to": SYNC_STATE_REQUIRED, "code": code,
                            "detail": "Live Agent evidence contradicts the recorded package "
                                      "synchronization state."})
            changed = True

    if changed and deps.persist:
        deps.persist()

    evidence["active_mission_id_after"] = mid
    evidence["expected_route_count"] = expected_count
    evidence["matched_record_count"] = len(matches)

    common = {
        "active_mission_id": mid,
        "active_route_hash": chosen.get("route_hash"),
        "rebound": any(a["action"] == ACTION_REBIND for a in actions),
        "actions": actions,
        "evidence": evidence,
    }

    # ── The verdict ───────────────────────────────────────────────────────────────────────
    if package_proven:
        return _verdict(SYNCHRONIZED, ALREADY_CONSISTENT,
                        "The approved mission, the flight controller and the Agent planning "
                        "package all carry the same canonical route.", **common)
    if package_contradicted:
        detail = ("The flight controller carries the approved route; Scout's planning package "
                  "does not match it. Synchronizing the package sends a package only — it "
                  "issues no vehicle command and cannot re-upload the mission.")
        return _verdict(PACKAGE_SYNC_REQUIRED,
                        PACKAGE_NOT_STORED if pkg["stored"] is False else PACKAGE_IDENTITY_MISMATCH,
                        detail, **common)
    # The operator/flight-controller half is settled; the Agent half could not be read. That is
    # an unasked question, not a disagreement, so the verdict stays inconclusive.
    reason = PACKAGE_UNREACHABLE if not pkg["reachable"] else PACKAGE_INCOMPLETE_EVIDENCE
    out = _verdict(RECONCILING, reason,
                   "The approved mission matches the flight controller. The Agent package "
                   "comparison could not be completed.", **common)
    out["pixhawk_settled"] = True
    return out
