"""quick_move.py — operator-side state for the `quick-move-v1` reporting contract.

WHAT THIS IS. A "Quick move" is ONE operator-authority action: send the selected surface
vehicle directly to a latitude/longitude the operator right-clicked on the Map, and have
Scout hold there in LOITER once it arrives. It is deliberately NOT part of mission
planning: no survey polygon, no approved geometry, no shoreline offset, no mission
readiness, no upload, no mission replacement, and nothing here ever arms the vehicle,
takes authority, disables a fence or resumes a mission. See QUICK_MOVE_HANDOFF.md.

WHAT THIS MODULE OWNS, and what it deliberately does not:
  * It validates a target coordinate and canonicalizes the command parameters.
  * It tracks, PER VEHICLE, which quick-move command this station currently considers
    authoritative, so a delayed status or a delayed command result for a SUPERSEDED move
    can never restore an old target on screen (see CORRELATION below).
  * It normalizes Scout's `payload.quick_move` block onto the fleet row with honest ages.
  * It answers the dispatch-time eligibility question (`eligibility()`), which POST
    /api/commands re-runs server-side — a UI check alone is never sufficient.
  * It NEVER decides that the vehicle arrived, never changes a flight mode, never
    cancels anything on the vehicle, and never infers completion from delivery. Only
    Scout reports movement, arrival and verified LOITER; this module displays what Scout
    said, with the freshness of the evidence behind it.

THREE FACTS KEPT APART EVERYWHERE, exactly as COMPANION_CONTRACT.md section 1 keeps its
three:
  1. DELIVERY   — did the command reach Scout at all? Answered by the existing command
                  queue (QUEUED -> SENT -> ACCEPTED/EXECUTED/...) and, independently and
                  more strongly, by Scout reporting this command_id back in its status
                  block (`delivery_confirmed`).
  2. MOVEMENT   — what is the vehicle doing now? Scout's `state` + `distance_remaining_m`.
  3. COMPLETION — did it ARRIVE and is it CONFIRMED HOLDING? Only a `COMPLETED` state
                  that ALSO carries `arrival_confirmed` and `loiter_verified` is published
                  as a confirmed completion. A bare `COMPLETED` is published as
                  CLAIMED_UNVERIFIED — Scout's claim is shown, never promoted.
  Command delivery or acknowledgement is NEVER presented as arrival.

CORRELATION AND SUPERSESSION. Every status Scout sends carries the `command_id` it refers
to. This station tracks exactly one CURRENT quick-move command per vehicle:
  * a status for the current id -> applied (subject to the `updated_at` ordering below);
  * a status for an id this station has already retired (a superseded or replaced move)
    -> REJECTED as `superseded_status`; the retired target is never redrawn;
  * a status for an id this station never issued -> REJECTED as `unknown_command_id`.
Within one command id, `updated_at` must strictly increase for a status to apply, and a
TERMINAL state (COMPLETED / CANCELLED / FAILED) is never walked back to a running one: a
buffered `MOVING` arriving after a `CANCELLED` cannot resurrect the movement.

SHORT-LIVED BY CONSTRUCTION. A quick move must not sit in an offline queue and execute
after reconnection — "go there" stops being the operator's intent within seconds of them
losing the link. main.py gives QUICK_MOVE its own short TTL and refuses to create one at
all unless the operator-side link is CONNECTED. This module supplies the reason codes.

EVIDENCE TIME. Age arithmetic is NOT re-implemented here. It reuses the station's one
freshness model (companion_telemetry.py's EVIDENCE TIME section, published verbatim in
COMPANION_CONTRACT.md section 3): a guaranteed lower bound `min_age_s` computed from two
single-clock deltas, a clearly-labelled `reporting_delay_s` ESTIMATE that conflates
transit delay with clock offset, and `estimated_age_s` for display only. Local elapsed
time runs on `time.monotonic()`, never wall clock. There is deliberately no "FRESH"
verdict for any single measurement, here or anywhere else in this station.

Tests: tests/test_quick_move.py. Fixture: scripts/quick_move_fixture.py (opt-in, loopback).
"""

import math

# The station's ONE evidence-time model. Importing it is deliberate: a second copy of
# min_age_s / reporting_delay_s arithmetic is exactly the drift COMPANION_CONTRACT.md
# section 3 exists to prevent. companion_telemetry.py publishes these as stable primitives.
from companion_telemetry import (
    evidence_freshness, evidence_of, mark_now, receipt_bundle, time_since,
)

SCHEMA = "quick-move-v1"

# --- bounds -------------------------------------------------------------------------
MAX_TEXT = 200
MAX_DISTANCE_REMAINING_M = 1000000.0     # a plausible-number guard, not a policy limit
MAX_ADVERTISED_DISTANCE_M = 100000.0     # ceiling on Scout's own advertised max_distance_m
# How far the target Scout echoes back may differ from the one the operator issued before
# the difference is reported as a mismatch. A few metres of float/representation noise is
# not a mismatch; a different destination is.
TARGET_MATCH_TOLERANCE_M = 5.0
# How long a TERMINAL quick move stays published after its last status arrived. The
# temporary target marker and the status card are meant to clear on their own; the event
# log keeps the permanent record. Not a timer that decides anything — purely how long the
# already-decided outcome remains on screen.
TERMINAL_RETENTION_S = 120.0
# How many retired command ids to remember per vehicle for `superseded_status`
# classification. Bounded: an unrecognised older id degrades to `unknown_command_id`,
# which is refused just as firmly — recognition is for the REASON, never for the refusal.
MAX_RETIRED_IDS = 32

# --- movement states (Scout's; the operator only ever displays them) -----------------
#   STARTING  — Scout has the command and is preparing to move (e.g. entering GUIDED).
#   MOVING    — under way toward the target.
#   ARRIVED   — Scout reports it is at the target; LOITER not yet verified.
#   COMPLETED — arrived AND Scout verified LOITER. Requires both evidence flags (below).
#   CANCELLED — superseded by a newer move, cancelled via LOITER, or stood down by Scout.
#   FAILED    — Scout could not perform or could not verify the move (`reason` required).
RUNNING_STATES = ("STARTING", "MOVING", "ARRIVED")
TERMINAL_STATES = ("COMPLETED", "CANCELLED", "FAILED")
STATES = frozenset(RUNNING_STATES + TERMINAL_STATES)

COMPLETION_CONFIRMED = "CONFIRMED"
COMPLETION_CLAIMED = "CLAIMED_UNVERIFIED"

# --- eligibility reason codes --------------------------------------------------------
# MACHINE codes, shared verbatim with operator/lib/quick-move.js (parity is pinned by
# tests/quick-move.test.mjs, which reads both sources). The prose attached to each code is
# authored once per side for its own audience; the CODES are the contract.
REASON_ORDER = (
    "not_reported",          # the vehicle has never sent a quick_move block at all
    "not_supported",         # it sent one, and it says supported:false
    "link_not_current",      # operator-side comm-state is not CONNECTED
    "authority_unreadable",  # control authority could not be read at all (fail closed)
    "not_operator",          # control authority is not a confirmed OPERATOR
    "disarmed",              # the vehicle is disarmed — it cannot move
    "armed_unknown",         # armed state is not currently known (fail closed)
    "nav_not_ready",         # Scout EXPLICITLY reports navigation readiness unavailable
    "bad_target",            # the clicked coordinate is not a usable WGS-84 position
    "too_far",               # beyond the max distance Scout itself advertised
    "busy",                  # a quick move from this station is already in flight
)
REASON_TEXT = {
    "not_reported": "This vehicle has not reported Quick move support.",
    "not_supported": "Scout reports Quick move is not available on this vehicle.",
    "link_not_current": "Scout is not connected — a Quick move is never queued for later delivery.",
    "authority_unreadable": "Control authority could not be read — a Quick move is never sent on an assumed authority.",
    "not_operator": "Take Control (OPERATOR) before commanding a Quick move.",
    "disarmed": "The vehicle is disarmed — it cannot move.",
    "armed_unknown": "The armed state is not currently known.",
    "nav_not_ready": "Scout reports navigation readiness is unavailable.",
    "bad_target": "That position is not a usable coordinate.",
    "too_far": "That position is beyond the Quick move range Scout advertises.",
    "busy": "A Quick move is already in flight for this vehicle.",
}


# --- primitives ----------------------------------------------------------------------

def _finite(value):
    """A finite float, or None. Bools are not numbers; NaN/inf are not measurements."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _text(value, limit=MAX_TEXT):
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    return s[:limit] if s else None


def _token(value):
    s = _text(value, 40)
    if not s:
        return None
    s = s.upper()
    return s if s.replace("_", "").isalnum() else None


def _tri_bool(value):
    """True / False / None. A missing or non-boolean value is UNKNOWN, never False —
    "Scout did not say" and "Scout said no" are different facts and are kept apart."""
    return value if isinstance(value, bool) else None


def latlng(raw):
    """A WGS-84 target from `{"lat":..., "lng":...}`, or None.

    Objects only, never a bare array: that is what removes the `[lat,lng]` vs GeoJSON
    `[lng,lat]` ambiguity (COMPANION_CONTRACT.md section 2 makes the same choice, for the
    same reason). `(0, 0)` is refused as the classic no-fix sentinel — Null Island is not a
    destination anyone right-clicks on a survey map."""
    if not isinstance(raw, dict):
        return None
    lat, lng = _finite(raw.get("lat")), _finite(raw.get("lng"))
    if lat is None or lng is None:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None
    if lat == 0.0 and lng == 0.0:
        return None
    return {"lat": lat, "lng": lng}


def meters_between(a, b):
    """Great-circle metres between two {lat,lng} dicts, or None. Mirrors
    operator/lib/home.js `metersBetween` so the distance the operator SEES before issuing
    and the distance this backend checks against Scout's advertised range agree."""
    if not a or not b:
        return None
    r, to_r = 6371000.0, math.pi / 180.0
    d_lat = (b["lat"] - a["lat"]) * to_r
    d_lng = (b["lng"] - a["lng"]) * to_r
    la, lb = a["lat"] * to_r, b["lat"] * to_r
    h = math.sin(d_lat / 2) ** 2 + math.cos(la) * math.cos(lb) * math.sin(d_lng / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


# --- per-vehicle state ---------------------------------------------------------------

def new_state():
    return {
        "reported": False,        # has this vehicle EVER sent a well-formed quick_move block
        "block_received": None,   # mark_now() of the last well-formed block (liveness)
        "capability": None,       # last advertised capability, or None if never advertised
        "current": None,          # the tracked quick move (see register_command)
        "retired": [],            # recently superseded/replaced command ids (bounded)
        "rejections": {"count": 0, "by_reason": {}, "last_reason": None, "last_at": None},
    }


def _reject(st, reason, at):
    r = st["rejections"]
    r["count"] += 1
    r["by_reason"][reason] = r["by_reason"].get(reason, 0) + 1
    r["last_reason"] = reason
    r["last_at"] = at


def _retire(st, command_id):
    if not command_id:
        return
    ids = st["retired"]
    if command_id in ids:
        return
    ids.append(command_id)
    del ids[:-MAX_RETIRED_IDS]


# --- operator-side command registration ----------------------------------------------

def register_command(store, vid, *, command_id, target, now_wall_s, now_mono_s):
    """Record that THIS station has just issued quick move `command_id` for vehicle `vid`,
    and retire whatever move it replaces.

    Returns the list of command ids that were superseded by this one, so the caller can
    terminate their queue records (an undelivered older move must never fire later) and log
    the supersession. Called from POST /api/commands AFTER the command record exists, so the
    id published here is the same id Scout will be handed and will report back."""
    st = store.setdefault(vid, new_state())
    superseded = []
    cur = st.get("current")
    if cur and cur["command_id"] != command_id:
        superseded.append(cur["command_id"])
        _retire(st, cur["command_id"])
    st["current"] = {
        "command_id": command_id,
        "requested_target": dict(target),
        "issued": mark_now(now_wall_s, now_mono_s),
        "delivery_confirmed": False,
        "cancel_requested": None,     # {"reason", "mark"} — a REQUEST, never a vehicle fact
        "status": None,               # last applied Scout status for this id
        "status_received": None,      # mark_now() of when that status arrived
    }
    return superseded


def request_cancel(store, vid, *, reason, now_wall_s, now_mono_s, command_id=None):
    """Record that the operator asked for the CURRENT quick move to stop — today that is
    the Stop / LOITER action, which is an ordinary SET_MODE_LOITER command.

    This is bookkeeping about the REQUEST only. It never marks the movement cancelled: only
    Scout reporting `CANCELLED` (or any other terminal state) ends the movement, and until
    it does the UI shows "stop requested", not "stopped". Returns the command id the request
    was recorded against, or None when there was nothing to stop."""
    st = store.get(vid)
    cur = st.get("current") if st else None
    if not cur:
        return None
    if command_id is not None and cur["command_id"] != command_id:
        return None
    status = cur.get("status")
    if status is not None and status["state"] in TERMINAL_STATES:
        # The move already ended. An operator pressing LOITER afterwards is doing something
        # perfectly reasonable, but it is not a stop request for THIS move, and recording it
        # as one would leave a finished card reading "stop requested" forever.
        return None
    cur["cancel_requested"] = {"reason": _text(reason) or "Stop requested by the operator.",
                               "mark": mark_now(now_wall_s, now_mono_s)}
    return cur["command_id"]


def forget_vehicle(store, vid):
    """Drop everything tracked for one vehicle (test/fixture isolation only)."""
    store.pop(vid, None)


# --- capability ----------------------------------------------------------------------

def _parse_capability(raw):
    """Scout's advertisement, or None when the block carries none.

    ABSENCE MEANS UNSUPPORTED, and that is load-bearing: a vehicle merely being present in
    the fleet never implies it can perform a guided move. Only an explicit
    `supported: true` enables the action anywhere in this station."""
    if not isinstance(raw, dict):
        return None
    supported = _tri_bool(raw.get("supported")) is True
    max_d = _finite(raw.get("max_distance_m"))
    if max_d is not None and not (0.0 < max_d <= MAX_ADVERTISED_DISTANCE_M):
        max_d = None
    return {
        "supported": supported,
        "reason": _text(raw.get("reason")),
        # Tri-state on purpose: None = Scout said nothing about navigation readiness, which
        # is NOT the same as reporting it unavailable and does not block the action.
        "nav_ready": _tri_bool(raw.get("nav_ready")),
        "max_distance_m": max_d,
        "vehicle_class": _token(raw.get("vehicle_class")),
    }


def capability_of(store, vid):
    """The capability this vehicle last advertised, or the absent-default. Never guesses."""
    st = store.get(vid)
    cap = st.get("capability") if st else None
    if cap is None:
        return {"supported": False, "reason": REASON_TEXT["not_reported"],
                "nav_ready": None, "max_distance_m": None, "vehicle_class": None,
                "source": "absent"}
    return {**cap, "source": "scout"}


# --- eligibility (the SERVER-side gate; mirrored for the UI in lib/quick-move.js) -----

def eligibility(store, vid, *, target, connected, authority, armed, vehicle_position=None,
                busy=False):
    """May a quick move be dispatched right now? -> (ok, code, message).

    Re-run at POST /api/commands dispatch even though the UI has already asked the same
    question: a UI check is a courtesy to the operator, never an authorization. Every input
    is a fact the backend can establish for itself — the caller passes them in only so this
    function stays pure and directly testable.

    `authority` is the value a LIVE read returned, or None when it could not be read at all.
    Both fail closed, and they are kept apart: "authority is LOCAL_AGENT" is a fact the
    operator can act on (Take Control), while "we could not read authority" is not — telling
    them to Take Control when the read itself failed would send them after the wrong problem.
    This station never dispatches a movement command on an assumed authority.

    `armed` is tri-state. None (not reported / not current) fails closed as `armed_unknown`
    — distinct from `disarmed`, because "we cannot tell" and "it is off" are different
    sentences to put in front of an operator."""
    cap = capability_of(store, vid)
    if cap["source"] == "absent":
        return False, "not_reported", REASON_TEXT["not_reported"]
    if not cap["supported"]:
        return False, "not_supported", cap["reason"] or REASON_TEXT["not_supported"]
    if not connected:
        return False, "link_not_current", REASON_TEXT["link_not_current"]
    if authority is None or str(authority).strip() == "":
        return False, "authority_unreadable", REASON_TEXT["authority_unreadable"]
    if str(authority).upper() != "OPERATOR":
        return False, "not_operator", REASON_TEXT["not_operator"]
    if armed is False:
        return False, "disarmed", REASON_TEXT["disarmed"]
    if armed is None:
        return False, "armed_unknown", REASON_TEXT["armed_unknown"]
    # EXPLICITLY unavailable only. `nav_ready: None` means Scout said nothing, and this
    # station does not invent a readiness failure out of a field nobody sent.
    if cap["nav_ready"] is False:
        return False, "nav_not_ready", REASON_TEXT["nav_not_ready"]
    goal = latlng(target)
    if goal is None:
        return False, "bad_target", REASON_TEXT["bad_target"]
    if cap["max_distance_m"] is not None and vehicle_position:
        d = meters_between(vehicle_position, goal)
        if d is not None and d > cap["max_distance_m"]:
            return (False, "too_far",
                    "That position is " + str(round(d)) + " m away; Scout advertises a "
                    + str(round(cap["max_distance_m"])) + " m Quick move range.")
    if busy:
        return False, "busy", REASON_TEXT["busy"]
    return True, None, None


# --- ingest --------------------------------------------------------------------------

def _parse_status(raw, packet_ts, receipt):
    """(status, reason). A structurally-invalid status is refused outright rather than
    partially applied — half a movement report is not a movement report."""
    if not isinstance(raw, dict):
        return None, "status_not_object"
    cmd_id = _text(raw.get("command_id"), 64)
    if not cmd_id:
        return None, "missing_command_id"
    state = _token(raw.get("state"))
    if state not in STATES:
        return None, "bad_state"
    updated, why = evidence_of(raw.get("updated_at"), packet_ts, receipt)
    if updated is None:
        return None, "bad_updated_at:" + str(why)
    target = latlng(raw.get("target"))
    if target is None:
        return None, "bad_target"
    dist = _finite(raw.get("distance_remaining_m"))
    if dist is not None and not (0.0 <= dist <= MAX_DISTANCE_REMAINING_M):
        dist = None
    return {
        "command_id": cmd_id,
        "state": state,
        "target": target,
        "distance_remaining_m": dist,
        # Tri-state: absent means Scout did not report it, which is never taken as proof of
        # arrival or of a verified hold.
        "arrival_confirmed": _tri_bool(raw.get("arrival_confirmed")),
        "loiter_verified": _tri_bool(raw.get("loiter_verified")),
        "mode": _token(raw.get("mode")),
        "reason": _text(raw.get("reason")),
        "updated": updated,
    }, None


HOLD_MODE = "LOITER"


def _mode_incoherent(status):
    """True when the report CONTRADICTS ITSELF: Scout says it verified the LOITER hold while
    also reporting the vehicle observed in some other mode.

    This is a real defect Scout hit and fixed (its `_handle_arrival()` checked the LOITER
    call's `verified` but discarded that same response's `observed_mode`, so `mode` kept
    reporting the last value sampled during the arrival poll — GUIDED). The operator checks
    it rather than trusting the fix, because a silent regression here produces the most
    dangerous possible screen: a confident "arrived and holding" over a vehicle that is
    still in GUIDED and may not be holding at all.

    Only an EXPLICIT contradiction counts. An absent `mode` is not one — Scout is allowed
    not to report it, and inventing a contradiction out of a missing field would be the
    same sin in the other direction."""
    return status["loiter_verified"] is True and status["mode"] not in (None, HOLD_MODE)


def _completion(status):
    """The operator's OWN verdict on a claimed completion — never Scout's word alone.

    COMPLETED is published as CONFIRMED only when Scout reports that it confirmed arrival,
    verified LOITER, AND the rest of the same report does not contradict that. A bare
    COMPLETED — or one whose own `mode` says the vehicle is not in the hold Scout claims to
    have verified — is CLAIMED_UNVERIFIED: Scout's claim stays visible (it is evidence), but
    the station never renders it as a finished, confirmed hold. This is the same rule
    mission upload follows for a read-back."""
    if status["state"] != "COMPLETED":
        return None
    if (status["arrival_confirmed"] is True and status["loiter_verified"] is True
            and not _mode_incoherent(status)):
        return COMPLETION_CONFIRMED
    return COMPLETION_CLAIMED


def ingest(store, vid, payload, *, packet_ts, received_at, received_mono):
    """Apply ONE accepted packet's `quick_move` block to vehicle `vid`'s own state.

    Call it ONLY for packets that passed main.py's per-vehicle monotonic envelope guard — a
    replayed older packet must not rewrite movement state any more than it may rewrite
    telemetry. Returns True when a status was APPLIED to the tracked current move (the
    caller's cue to log a state transition), False otherwise.

    An omitted or null `quick_move` key is NO INFORMATION and changes nothing: a Scout build
    that predates this contract keeps working untouched, and its vehicles simply never
    advertise the capability, so the action is never offered for them."""
    if vid is None or not isinstance(payload, dict):
        return False
    block = payload.get("quick_move")
    if block is None:
        return False
    st = store.setdefault(vid, new_state())
    if not isinstance(block, dict) or block.get("schema") != SCHEMA:
        _reject(st, "unsupported_schema", received_at)
        return False

    # Well-formed: liveness updates regardless of what the correlation check below decides
    # about the STATUS. "Scout is still telling us something about Quick move" and "this
    # particular status applies" are independent facts (COMPANION_CONTRACT.md section 4
    # draws the same line for its block-level liveness).
    st["reported"] = True
    st["block_received"] = mark_now(received_at, received_mono)

    if "capability" in block:
        cap = _parse_capability(block.get("capability"))
        if cap is None:
            _reject(st, "bad_capability", received_at)
        else:
            st["capability"] = cap

    if "active" not in block:
        return False                      # capability-only heartbeat: nothing to correlate
    active = block.get("active")
    cur = st.get("current")
    if active is None:
        # Scout reports nothing active. This is NOT completion and NOT cancellation — it
        # only means Scout has stopped reporting a movement. Whatever the last applied
        # status said stands, with its age; nothing is inferred to fill the gap.
        if cur is not None and cur.get("status") is None:
            _reject(st, "cleared_before_status", received_at)
        return False

    receipt = receipt_bundle(received_at, received_mono, packet_ts)
    status, why = _parse_status(active, packet_ts, receipt)
    if status is None:
        _reject(st, why, received_at)
        return False

    if cur is None or status["command_id"] != cur["command_id"]:
        # A status for a move this station is not tracking. Both branches refuse it; they
        # differ only in the reason recorded, never in the outcome — a retired target is
        # never redrawn, whether or not we still remember retiring it.
        _reject(st, "superseded_status" if status["command_id"] in st["retired"]
                else "unknown_command_id", received_at)
        return False

    prev = cur.get("status")
    if prev is not None:
        if prev["state"] in TERMINAL_STATES and status["state"] not in TERMINAL_STATES:
            # A buffered running status arriving after a terminal one must never resurrect
            # the movement — the outcome already happened.
            _reject(st, "terminal_state_regression", received_at)
            return False
        if status["updated"]["observed_at"] < prev["updated"]["observed_at"]:
            _reject(st, "stale_status", received_at)
            return False
        if status["updated"]["observed_at"] == prev["updated"]["observed_at"]:
            # An idempotent resend. Delivery is (still) confirmed and liveness already
            # refreshed above; the ORIGINAL receipt clock is kept so a repeat cannot
            # manufacture freshness.
            cur["delivery_confirmed"] = True
            return False

    cur["status"] = status
    cur["status_received"] = mark_now(received_at, received_mono)
    # Scout naming this command id back to us is the strongest delivery evidence there is —
    # stronger than the queue's own SENT/ACCEPTED, which only records what this station did.
    cur["delivery_confirmed"] = True
    return True


# --- publication ---------------------------------------------------------------------

def empty_block():
    """The block a vehicle that has never reported Quick move carries — same shape, no
    capability and no movement, so no consumer has to branch on "has this key appeared
    yet?". `supported: false` here is the honest default: absence means unsupported."""
    return {
        "schema": SCHEMA, "reported": False, "block_age_s": None,
        "capability": {"supported": False, "reason": REASON_TEXT["not_reported"],
                       "nav_ready": None, "max_distance_m": None, "vehicle_class": None,
                       "source": "absent"},
        "current": None,
        "rejections": {"count": 0, "by_reason": {}, "last_reason": None},
    }


def _status_view(cur, status, now_wall_s, now_mono_s):
    completion = _completion(status)
    mismatch = meters_between(cur["requested_target"], status["target"])
    return {
        "state": status["state"],
        "target": dict(status["target"]),
        # Metres between the target the OPERATOR issued and the one Scout echoes back.
        # Published as a number, not a verdict, plus the boolean the UI acts on — a Scout
        # driving to a different place than the operator clicked is the one failure this
        # display exists to make impossible to miss.
        "target_offset_m": None if mismatch is None else round(mismatch, 1),
        "target_mismatch": bool(mismatch is not None and mismatch > TARGET_MATCH_TOLERANCE_M),
        "distance_remaining_m": (None if status["distance_remaining_m"] is None
                                 else round(status["distance_remaining_m"], 1)),
        "arrival_confirmed": status["arrival_confirmed"],
        "loiter_verified": status["loiter_verified"],
        "mode": status["mode"],
        # The report contradicts itself: a verified LOITER hold reported alongside some
        # other observed mode. Published as its own flag rather than folded silently into
        # `completion`, so the operator is told WHY the hold is not being confirmed.
        "mode_incoherent": _mode_incoherent(status),
        "reason": status["reason"],
        "terminal": status["state"] in TERMINAL_STATES,
        # CONFIRMED only with BOTH pieces of evidence; CLAIMED_UNVERIFIED for a bare
        # COMPLETED; null while still running. Never derived from delivery or from a command
        # result.
        "completion": completion,
        "updated": evidence_freshness(status["updated"], now_wall_s, now_mono_s),
        # How long since THIS status reached the operator (monotonic). Distinct from the
        # observation age above: this is "how long since Scout last said anything about this
        # move", which is what goes stale when the link drops.
        "status_age_s": time_since(cur.get("status_received"), now_mono_s),
    }


def fleet_block(store, vid, now_wall_s, now_mono_s):
    """The `quick_move` block for one fleet row, ages computed at `(now_wall_s, now_mono_s)`.

    `now_wall_s` is threaded through for parity with `ingest()` and for the freshness
    model's absolute-time fields; every elapsed-time number is monotonic-derived."""
    st = store.get(vid)
    if not st:
        return empty_block()
    rej = st["rejections"]
    cap = capability_of(store, vid)
    cur = st.get("current")
    current = None
    if cur is not None:
        status = cur.get("status")
        view = None if status is None else _status_view(cur, status, now_wall_s, now_mono_s)
        # A finished move clears itself from the live display after a short retention, so a
        # stale target marker never lingers. Nothing is decided here — the outcome was
        # decided by Scout long before; this only stops showing it.
        age = time_since(cur.get("status_received"), now_mono_s)
        if not (view and view["terminal"] and age is not None and age > TERMINAL_RETENTION_S):
            cancel = cur.get("cancel_requested")
            current = {
                "command_id": cur["command_id"],
                "requested_target": dict(cur["requested_target"]),
                "requested_age_s": time_since(cur.get("issued"), now_mono_s),
                # False means only "Scout has not reported this id back yet" — it is never a
                # claim that delivery FAILED. The command record's own lifecycle carries the
                # queue side of that story; between them the UI shows uncertainty rather
                # than inventing either verdict.
                "delivery_confirmed": bool(cur.get("delivery_confirmed")),
                "cancel_requested": None if not cancel else {
                    "reason": cancel["reason"],
                    "age_s": time_since(cancel["mark"], now_mono_s),
                },
                "status": view,
            }
    return {
        "schema": SCHEMA,
        "reported": st["reported"],
        "block_age_s": time_since(st.get("block_received"), now_mono_s),
        "capability": cap,
        "current": current,
        "rejections": {"count": rej["count"], "by_reason": dict(rej["by_reason"]),
                       "last_reason": rej["last_reason"]},
    }
