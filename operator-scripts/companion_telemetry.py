"""companion_telemetry.py — Scout's OPTIONAL UAV-companion block (`companion-v2`).

WHAT THIS IS
------------
Scout's Local Mission Agent will eventually coordinate a UAV that inspects ahead of the
survey route. The UAV talks to Scout; the operator only ever hears about it THROUGH Scout's
existing POST /agent/status path. This module is the operator-side contract for that report:
validation, per-parent storage, snapshot ordering, and freshness. The exact wire format is
written down for the USV side in COMPANION_CONTRACT.md (reference) and
SCOUT_INTEGRATION_HANDOFF.md (concise, for the Scout-side developer) — this file implements it.

A companion is NOT a fleet vehicle. It has no canonical fleet id, no command route, no
authority and no mission of its own on this station. It is state that belongs to ONE
parent USV record, is only ever written by that parent's own packets, and is published on
that parent's fleet row only.

`companion-v1` was PROPOSED but never adopted by any real Scout build, so this is a
deliberate, breaking schema revision rather than a silent meaning-change: a `companion-v1`
block is now rejected the same way any other unrecognised schema string is (`unsupported_schema`).
It lacked two things v2 fixes: (1) a snapshot-level ordering primitive (see SNAPSHOT ORDERING
below — a `companion-v1` snapshot had no way to tell a newer ENVELOPE carrying an OLDER
companion snapshot from a genuinely fresher one), and (2) a documented split between the age
of an observation and how long its envelope took to arrive (see EVIDENCE TIME below).

RULES THIS MODULE OBEYS
-----------------------
  * OMISSION IS NOT A CLEAR. A packet without `companions` changes nothing — the last-known
    assignment stays, and nothing about it is refreshed (its ages keep growing). Only an
    explicit block with `items: []` clears the assignment. Scout is expected to keep sending
    `items: []` on EVERY subsequent packet while genuinely unassigned (a snapshot is a
    statement of CURRENT truth, not a one-shot event) — see SNAPSHOT ORDERING for why a single
    lost "clear" packet must not leave a stale assignment on screen forever.
  * EVIDENCE TIME IS THREE DISTINCT NUMBERS, NEVER COLLAPSED INTO ONE, AND ONLY ONE OF THEM MAY
    EVER ESTABLISH STALENESS (see `_freshness`):
      - `min_age_s`      a mathematically guaranteed LOWER BOUND on how old an observation is
                         right now. Needs no clock-sync assumption: it is a Scout-clock delta
                         (`age_at_source_s`, envelope timestamp minus `observed_at`) plus a
                         LOCAL, MONOTONIC delta (elapsed time since WE physically received this
                         evidence). It can only understate the true age, never overstate it,
                         because it assumes the envelope was delivered with zero delay — which
                         is never literally true. This is the ONLY number allowed to establish
                         STALE (`min_age_s` already past the threshold means the true age is too,
                         since the true age can only be larger). A SMALL `min_age_s` does NOT
                         establish freshness — see `reporting_delay_s` for why.
      - `reporting_delay_s`  how long this envelope APPEARS to have taken to arrive, by
                         comparing the operator's own wall clock at receipt against Scout's
                         claimed send time. This CONFLATES real transit/buffering delay with any
                         Scout<->operator clock offset, and can be wrong in EITHER direction —
                         e.g. Scout's clock 30 s ahead plus a genuine 30 s delivery delay yields
                         a computed delay of exactly 0, hiding a real 30 s gap entirely. It is an
                         ESTIMATE, never proof of anything, clamped at zero (a negative raw value
                         means the envelope looks like it is from the future, flagged separately
                         as `clock_anomaly`, never treated as -N seconds of delay). NEITHER this
                         number NOR `estimated_age_s` below may be used to decide STALE vs not —
                         they are shown to the operator, clearly labelled as estimates, for their
                         own judgement, never consumed as ground truth by this module or the UI.
      - `estimated_age_s`    `min_age_s + reporting_delay_s` when both are known — a best-effort
                         correction that assumes the two clocks are roughly aligned. Exposed
                         alongside `min_age_s` for display only, never used to establish either
                         STALE or fresh.
    Without a VALIDATED clock-offset or delivery-delay-bound mechanism (this module has none,
    and none is invented here), an observation with a small `min_age_s` is not "fresh" — it is
    UNCERTAIN: nothing rules out it actually being much older, because a real, unaccounted-for
    delivery delay is always possible. `freshness_quality: "LOWER_BOUND"` names this precisely
    (a floor, never a ceiling — the previous name, "BOUNDED", read too easily as "bounded above",
    i.e. a maximum-age guarantee this module has never actually made). An observation whose
    envelope carried no timestamp gets `freshness_quality: "UNKNOWN"` and every one of the three
    numbers above is None — NOT a fabricated zero. Either way, "fresh" is not a value this module
    ever asserts about an individual observation; see the module's CLASSIFICATION note below and
    `operator/lib/companion.js`'s `evidenceView()`, which turns this into STALE / UNCERTAIN /
    UNKNOWN for display — never FRESH.
  * ASSUMPTIONS EVEN THE LOWER BOUND DEPENDS ON. `min_age_s`'s guarantee is not unconditional:
    (1) it assumes Scout's own clock does not jump backward BETWEEN composing `observed_at` and
    the envelope `timestamp` for the SAME packet — enforced by `CLOCK_TOLERANCE_S` rejecting a
    packet where it apparently did; (2) it assumes the operator's own monotonic clock never
    resets mid-process, which `time.monotonic()` guarantees by its OS/Python contract, but a
    process restart naturally resets it — handled by re-measuring from the new process's own
    receipt time, never by extrapolating across a restart; (3) it assumes Scout has correctly
    converted the UAV's own onboard observation time into SCOUT's clock before reporting
    `observed_at` — this module has NO way to detect an error in that conversion, and simply
    trusts it, exactly as it trusts every other Scout-reported fact it cannot independently
    verify. None of these are validated cross-host clock-synchronisation guarantees — they are
    the minimum a single, internally-consistent clock needs to hold, which is a much weaker (and
    achievable) assumption than "Scout and the operator agree on the time".
  * REPEATED/OLDER EVIDENCE NEVER GETS A FRESHER RECEIPT TIME. A duplicate (same `observed_at`)
    is a no-op; an older one is rejected. Either way the STORED receipt clock readings — the
    anchor `min_age_s` is computed from — are untouched, so replaying an old packet can never
    make its payload look fresher than the first time it arrived.
  * SNAPSHOT ORDERING PROTECTS THE COMPLETE-SET SEMANTICS. Per-hazard `revision` protects one
    OBJECT's content; it says nothing about MEMBERSHIP (a hazard can vanish from a list with no
    revision of its own). `payload.companions.session {id, seq, generation}` is the block-level
    ordering primitive: `seq` must strictly increase within a `generation` on ANY content
    change, including a removal. A block whose `seq` is behind the one already applied for its
    `generation` is rejected wholesale (membership untouched) even though the OUTER envelope
    passed main.py's own per-vehicle monotonic timestamp guard — that guard only proves the
    ENVELOPE is not a replay, not that the COMPANION PAYLOAD inside it is current (Scout's
    companion-reporting subsystem could hold a stale cached snapshot across an otherwise-healthy
    telemetry stream).
  * PUBLISHER GENERATIONS, NOT SESSION-ID TRUST. A publisher restart is recognised by
    `session.generation` — an integer SCOUT ITSELF persists across its own restarts and
    increases on every one — compared NUMERICALLY against the highest generation ever accepted
    for that vehicle. A LOWER generation is a RETIRED publisher and is rejected unconditionally,
    however recent the envelope that carries it: once generation 2 has been accepted, a delayed
    generation-1 snapshot can never modify companion membership, hazards, activity or
    measurement evidence again, in this process or (because the generation high-water mark is
    persisted — see OPERATOR RESTARTS below) across an operator restart either. `session.id` is
    kept as a human/debug-readable label but is NEVER compared against another id (lexically or
    otherwise) to decide ordering — only `generation`, a genuine total order, does that. A block
    reporting an UNCHANGED generation under a DIFFERENT id is self-contradictory (a generation
    is Scout's own promise that it only advances on a restart, and a restart is what gives it a
    fresh id too) and is rejected wholesale as `inconsistent_session_generation`, not trusted as
    new content under a borrowed generation number. See `ingest()`, `_session_conflict()` and
    `_apply_session()` for the exact policy.
  * THIS GENERATION CHECK DOES NOT DEPEND ON THE ENVELOPE HAVING A TIMESTAMP. Unlike the
    superseded "trust any different session id" policy, comparing `generation` numbers is pure
    integer comparison — it needs no envelope timestamp and inherits none of main.py's own
    per-vehicle timestamp guard's gaps. A Scout that never sends an envelope timestamp still
    gets full cross-generation replay protection; it only loses FRESHNESS information (see
    EVIDENCE TIME) and main.py's own envelope-level replay guard (a DIFFERENT, pre-existing,
    unrelated limitation — see COMPANION_CONTRACT.md's Limitations for what remains open there).
  * OPERATOR RESTARTS. The highest `generation` (and the `id`/`seq` that go with it) accepted
    per vehicle is persisted to `runtime_data/companion_sessions.json` (atomic write, same
    convention as the mission store) whenever a NEW generation is adopted — see `main.py`'s
    `_save_companion_sessions()`. At startup the operator restores ONLY this ordering triplet
    per vehicle (`seed_session()`) — never any companion identity, measurement or hazard data,
    which always starts genuinely empty after a restart, so restored ordering protection never
    presents restored evidence as current. A missing or corrupt persisted file fails CLOSED to
    "no persisted ordering state" (logged loudly): the station still runs, and an UNFAMILIAR
    session (one this process has no memory of, old or new) is then judged purely by whether its
    `generation` is higher than whatever this process has seen SINCE it started — a narrower,
    but still safe (never-fabricating), degradation. See COMPANION_CONTRACT.md's Limitations for
    the residual gap this leaves (a delayed retired-generation packet arriving in the SHORT
    window between an operator restart and the operator's next real update from Scout).
  * SCOUT'S WORDS, BOUNDED. Link/disposition/replan tokens outside the documented set become
    UNKNOWN — never a guess, and never "ACCEPTED"/"VERIFIED" by accident.
  * MALFORMED IS REJECTED, NOT REPAIRED. Nonfinite/out-of-range coordinates, self-intersecting
    or oversized polygons, over-long lists, future timestamps and a malformed/missing
    `session` are refused and counted — never silently coerced into "0 items = clear."

No FastAPI, no globals: the caller owns `store = {parent_vehicle_id: state}`.
"""

import math
import re

SCHEMA = "companion-v2"

# --- bounds (documented in COMPANION_CONTRACT.md §6) ---------------------------------
MAX_COMPANIONS = 4          # companions per parent block
MAX_HAZARDS = 32            # hazards per companion snapshot
MAX_RING_VERTICES = 64      # vertices per polygon (closing duplicate not counted)
MAX_EXTENT_M = 5000.0       # polygon bounding-box diagonal
MAX_UNCERTAINTY_M = 1000.0  # hazard horizontal uncertainty radius
MAX_ALTITUDE_M = 10000.0
MIN_ALTITUDE_M = -500.0
CLOCK_TOLERANCE_S = 2.0     # observed_at may lead the packet timestamp by this much (Scout's
                            # own clock, self-consistency only — see _evidence)
# How far NEGATIVE the raw (operator wall clock - envelope timestamp) delta may be before it is
# flagged as a clock anomaly rather than just "not much reporting delay". Deliberately more
# generous than CLOCK_TOLERANCE_S: this compares TWO DIFFERENT clocks (Scout's and the
# operator's), which may legitimately disagree by a few seconds without either being "broken".
REPORTING_DELAY_ANOMALY_TOLERANCE_S = 5.0
MAX_EVIDENCE_AGE_S = 86400.0  # older than a day at receipt is not an observation, it is a bug
MAX_TEXT = 200

LINK_STATES = frozenset({"CONNECTED", "DEGRADED", "LOST", "NEVER_CONNECTED", "UNKNOWN"})
ASSIGNMENT_STATES = frozenset({"ASSIGNED", "UNASSIGNED"})
ALTITUDE_REFS = frozenset({"AMSL", "AGL", "RELATIVE_HOME"})
DISPOSITION_STATES = frozenset({"REPORTED", "UNDER_REVIEW", "ACCEPTED", "REJECTED", "EXPIRED"})
REPLAN_STATES = frozenset({"NOT_REQUIRED", "PENDING", "IN_PROGRESS", "VERIFIED", "FAILED",
                           "BLOCKED"})

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,47}$")
_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_M_PER_DEG = 111320.0


# --- primitives -----------------------------------------------------------------------

def _finite(value):
    """A finite float, or None. Bools are not numbers; NaN/inf are not measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _int(value, lo=0, hi=2 ** 31):
    """A bounded non-negative integer (an integral float is accepted), else None."""
    f = _finite(value)
    if f is None or not f.is_integer():
        return None
    i = int(f)
    return i if lo <= i <= hi else None


def _text(value, limit=MAX_TEXT):
    if not isinstance(value, str):
        return None
    t = value.strip()
    return t[:limit] if t else None


def _ident(value):
    """A stable lowercase identifier (companion / hazard / session id), else None."""
    if not isinstance(value, str):
        return None
    t = value.strip().lower()
    return t if _ID_RE.match(t) else None


def _token(value):
    """Scout's uppercase token, or None when it is not a well-formed token."""
    if not isinstance(value, str):
        return None
    t = value.strip().upper()
    return t if _TOKEN_RE.match(t) else None


def _enum(value, allowed):
    """A documented token, or UNKNOWN. Absence and nonsense both read UNKNOWN — the
    operator never promotes an unrecognised word into a documented state."""
    t = _token(value)
    return t if t in allowed else "UNKNOWN"


def _latlng(lat, lng):
    """(lat, lng) WGS-84 decimal degrees, or None. (0, 0) is the classic no-fix sentinel
    and is refused rather than plotted in the Gulf of Guinea."""
    la, ln = _finite(lat), _finite(lng)
    if la is None or ln is None:
        return None
    if not (-90.0 <= la <= 90.0 and -180.0 <= ln <= 180.0):
        return None
    if la == 0.0 and ln == 0.0:
        return None
    return la, ln


def _point_of(obj):
    return _latlng(obj.get("lat"), obj.get("lng")) if isinstance(obj, dict) else None


# --- geometry ---------------------------------------------------------------------------

def _orient(a, b, c):
    v = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
    return 0 if abs(v) < 1e-15 else (1 if v > 0 else -1)


def _segments_cross(p1, p2, q1, q2):
    """Proper or touching intersection of two segments (collinear overlap included)."""
    o1, o2 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    o3, o4 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    if o1 != o2 and o3 != o4:
        return True

    def on(a, b, c):
        return (min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
                and min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))
    return ((o1 == 0 and on(p1, p2, q1)) or (o2 == 0 and on(p1, p2, q2))
            or (o3 == 0 and on(q1, q2, p1)) or (o4 == 0 and on(q1, q2, p2)))


def _ring(raw):
    """[{lat, lng}, ...] → [(lat, lng), ...] (open ring), or None when malformed.

    3..MAX_RING_VERTICES vertices (a closing vertex equal to the first is dropped), every
    vertex valid, non-zero area, bounding-box diagonal ≤ MAX_EXTENT_M, and SIMPLE (no two
    non-adjacent edges touch) — a bow-tie has no defined inside and is refused."""
    if not isinstance(raw, list):
        return None
    pts = []
    for v in raw:
        p = _point_of(v)
        if p is None:
            return None
        pts.append(p)
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not (3 <= len(pts) <= MAX_RING_VERTICES):
        return None
    if len(set(pts)) != len(pts):
        return None
    area2 = sum(pts[i][1] * pts[(i + 1) % len(pts)][0] - pts[(i + 1) % len(pts)][1] * pts[i][0]
                for i in range(len(pts)))
    if abs(area2) < 1e-14:
        return None
    lats = [p[0] for p in pts]
    lngs = [p[1] for p in pts]
    dy = (max(lats) - min(lats)) * _M_PER_DEG
    dx = (max(lngs) - min(lngs)) * _M_PER_DEG * math.cos(math.radians(sum(lats) / len(lats)))
    if math.hypot(dx, dy) > MAX_EXTENT_M:
        return None
    n = len(pts)
    for i in range(n):
        a1, a2 = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or j == (i + 1) % n:
                continue                       # adjacent edges share a vertex by design
            if _segments_cross(a1, a2, pts[j], pts[(j + 1) % n]):
                return None
    return pts


def _geometry(raw):
    """{"type": "point", lat, lng} | {"type": "polygon", "ring": [...]} → normalized, or None."""
    if not isinstance(raw, dict):
        return None
    kind = _token(raw.get("type"))
    if kind == "POINT":
        p = _point_of(raw)
        return {"type": "point", "lat": p[0], "lng": p[1]} if p else None
    if kind == "POLYGON":
        ring = _ring(raw.get("ring"))
        return {"type": "polygon", "ring_latlng": [[a, b] for a, b in ring]} if ring else None
    return None


# --- evidence time ------------------------------------------------------------------------
#
# THE THREE NUMBERS, AND WHY THERE ARE THREE
# -------------------------------------------
# A naive "age = envelope.timestamp - observed_at, then add elapsed time since we received the
# packet" silently assumes the envelope was delivered the instant it was sent. On this station's
# own intermittent/store-and-forward link that assumption is routinely false: a Local Agent can
# compose an envelope, then sit on it for tens of seconds (or longer, across a real outage)
# before the POST actually reaches the operator. Under the naive formula, an observation that
# was fresh when SENT still reads as fresh the moment it FINALLY arrives, no matter how long it
# was in flight — exactly the failure this module has to avoid.
#
# The fix is to never collapse "how stale was this at send time" and "how long did delivery take"
# into one number, and to be explicit about which parts of the calculation are exact (same-clock
# or monotonic deltas) versus which are an ESTIMATE that assumes the two hosts' clocks are
# roughly aligned:
#
#   age_at_source_s     envelope.timestamp - observed_at            (Scout clock only, EXACT)
#   elapsed_since_receipt_s   now_monotonic - receipt_monotonic     (operator clock only, EXACT,
#                                                                     immune to wall-clock jumps)
#   min_age_s = age_at_source_s + elapsed_since_receipt_s            GUARANTEED LOWER BOUND —
#       needs no cross-clock assumption at all, because both terms are single-clock deltas. It
#       can only ever UNDERSTATE the true age (real delivery delay is >= 0), never overstate it.
#
#   reporting_delay_s = max(0, receipt_wall - envelope.timestamp)   an ESTIMATE — this compares
#       the OPERATOR's wall clock against SCOUT's envelope timestamp, which conflates genuine
#       transit/buffering delay with any clock offset between the two hosts. Computed ONCE per
#       packet (every piece of evidence in the same envelope shares it) and clamped at zero: a
#       negative raw value does not mean "negative delay", it means the two clocks disagree (or
#       one has jumped) — flagged separately as `clock_anomaly`, never silently zeroed away.
#
#   estimated_age_s = min_age_s + reporting_delay_s                  a best-effort correction,
#       assuming rough clock alignment. DISPLAY ONLY — see CLASSIFICATION below.
#
# `freshness_quality` is "LOWER_BOUND" when age_at_source_s is known (min_age_s is a trustworthy
# floor) or "UNKNOWN" when it is not (legacy packet with no envelope timestamp at all) — in the
# UNKNOWN case every one of the three numbers above is None; there is no fallback to "assume it
# just arrived", which is exactly the bug this module exists to close.
#
# CLASSIFICATION: STALE, UNCERTAIN, or UNKNOWN — never a system-asserted "FRESH".
# A real (if unmeasured) delivery delay can always exist, and `reporting_delay_s` cannot reliably
# detect it (see its own comment above). So the only classification this module — or the UI layer
# built on it — may ever assert automatically is:
#   STALE      min_age_s is known AND already past the caller's threshold. Safe, because the true
#              age can only be >= min_age_s.
#   UNCERTAIN  quality is LOWER_BOUND but min_age_s has NOT crossed the threshold. This does NOT
#              mean fresh — it means nothing yet PROVES it is stale, which is a materially weaker
#              claim, and is presented as such (see operator/lib/companion.js's evidenceView).
#   UNKNOWN    quality is UNKNOWN (no envelope timestamp) — no age claim at all is possible.
# `reporting_delay_s`/`estimated_age_s` are still computed and published in full, clearly labelled
# as estimates, so an operator can use their own judgement — they simply never drive the
# STALE/UNCERTAIN/UNKNOWN verdict itself.

def _mark(wall_s, mono_s):
    """One operator-side receipt-clock reading: wall (for diagnostics/absolute display) paired
    with monotonic (for elapsed-time arithmetic that a system-clock jump cannot corrupt)."""
    return {"wall": wall_s, "mono": mono_s}


def _elapsed(mark, now_mono_s):
    """Local elapsed time since `mark`, computed on the MONOTONIC clock only. None if unmarked."""
    return None if mark is None else round(max(0.0, now_mono_s - mark["mono"]), 1)


def _receipt(received_at, received_mono, packet_ts):
    """Everything about THIS delivery that every piece of evidence in the same packet shares —
    computed once per `ingest()` call and threaded through `_evidence()`. See module docstring
    and the block comment above for what `reporting_delay_s` does and does not prove."""
    mark = _mark(received_at, received_mono)
    if packet_ts is None:
        return {"mark": mark, "reporting_delay_s": None, "clock_anomaly": False}
    raw = received_at - packet_ts
    return {"mark": mark, "reporting_delay_s": max(0.0, raw),
            "clock_anomaly": raw < -REPORTING_DELAY_ANOMALY_TOLERANCE_S}


def _evidence(raw_ts, packet_ts, receipt):
    """(evidence, reason). Evidence carries `observed_at` (Scout clock, preserved verbatim so a
    consumer always has the source timestamp — never only a derived age), the Scout-clock-only
    `age_at_source_s`, and the shared `receipt` bundle this evidence arrived with.

    Without a packet timestamp, `age_at_source_s` is None (UNKNOWN quality) rather than guessed
    from the operator's clock — this is the "legacy packet with no envelope timestamp" case."""
    t = _finite(raw_ts)
    if t is None:
        return None, "missing_observed_at"
    if packet_ts is None:
        return {"observed_at": t, "age_at_source_s": None, "receipt": receipt}, None
    age = packet_ts - t
    if age < -CLOCK_TOLERANCE_S:
        return None, "future_observed_at"
    if age > MAX_EVIDENCE_AGE_S:
        return None, "observed_at_too_old"
    return {"observed_at": t, "age_at_source_s": max(0.0, age), "receipt": receipt}, None


def _freshness(ev, now_wall_s, now_mono_s):
    """The published freshness facts for one piece of evidence — see the module-level comment
    block (CLASSIFICATION) for what each field means, why there are three separate age numbers,
    and why none of them may assert "fresh".

    `freshness_quality` is "LOWER_BOUND" (min_age_s is a trustworthy FLOOR — never mistake this
    for a maximum-age guarantee; the previous name, "BOUNDED", invited exactly that mistake) or
    "UNKNOWN" (no envelope timestamp at all — every number below is None, not a fabricated zero)."""
    if not ev:
        return None
    receipt = ev.get("receipt") or {}
    elapsed = _elapsed(receipt.get("mark"), now_mono_s)
    src = ev.get("age_at_source_s")
    min_age = None if src is None or elapsed is None else round(src + elapsed, 1)
    delay = receipt.get("reporting_delay_s")
    estimated = None if min_age is None or delay is None else round(min_age + delay, 1)
    return {
        "observed_at": ev.get("observed_at"),
        "min_age_s": min_age,
        "reporting_delay_s": None if delay is None else round(delay, 1),
        "estimated_age_s": estimated,
        "freshness_quality": "LOWER_BOUND" if src is not None else "UNKNOWN",
        "clock_anomaly": bool(receipt.get("clock_anomaly")),
    }


def _since(mark, now_mono_s):
    """Local elapsed time (monotonic) since a bookkeeping `mark` (block/status/hazards receipt,
    or an explicit clear) — the SAME "no wall-clock jump" guarantee as `_elapsed`, reused for
    the liveness fields (`block_age_s`, `status_age_s`, `hazards_age_s`)."""
    return _elapsed(mark, now_mono_s)


# --- per-vehicle state ------------------------------------------------------------------

def new_state():
    return {
        "reported": False,          # has this parent EVER sent a well-formed block
        "block_received": None,     # _mark() of the last well-formed block (liveness)
        "cleared_at": None,         # _mark() of the last APPLIED explicit `items: []`
        "companions": {},           # {companion_id: record}
        "session": None,            # {"id","seq","generation","started":_mark()|None} — see
                                    # SNAPSHOT ORDERING / PUBLISHER GENERATIONS. `started` is
                                    # None for a session SEEDED from persisted ordering state at
                                    # startup (see seed_session) rather than actually observed.
        "rejections": {"count": 0, "by_reason": {}, "last_reason": None, "last_at": None},
    }


def _reject(st, reason, at):
    r = st["rejections"]
    r["count"] += 1
    r["by_reason"][reason] = r["by_reason"].get(reason, 0) + 1
    r["last_reason"] = reason
    r["last_at"] = at


def _newer(prev, new):
    """Keep `new` only if it is strictly newer evidence than `prev`. A duplicate (equal
    observed_at) or an older one leaves `prev` — and its original receipt clock — untouched, so
    repeated/out-of-order delivery can never manufacture freshness (see module docstring)."""
    return prev is None or new["observed_at"] > prev["observed_at"]


# --- snapshot session/seq/generation (block-level ordering; see the module doc's SNAPSHOT
# ORDERING / PUBLISHER GENERATIONS / OPERATOR RESTARTS sections) --------------------------

def _validate_session(raw_session):
    """(session_id, seq, generation, reason). A `companion-v2` block MUST carry a well-formed
    `session {id, seq, generation}` — this is what makes the schema revision worth making.
    `id` follows the same identifier grammar as companion/hazard ids (a UUID fits comfortably)
    and is a debug label ONLY, never compared for ordering. `seq` and `generation` are bounded
    non-negative integers with no required starting value: `seq` need only be monotonic
    non-decreasing WITHIN a generation; `generation` must be monotonic non-decreasing across
    Scout's own restarts of its companion-reporting subsystem — see PUBLISHER GENERATIONS."""
    if not isinstance(raw_session, dict):
        return None, None, None, "missing_snapshot_session"
    sid = _ident(raw_session.get("id"))
    if sid is None:
        return None, None, None, "missing_snapshot_session"
    seq = _int(raw_session.get("seq"))
    if seq is None:
        return None, None, None, "bad_snapshot_seq"
    generation = _int(raw_session.get("generation"))
    if generation is None:
        return None, None, None, "bad_session_generation"
    return sid, seq, generation, None


def _session_conflict(cur, sid, generation):
    """True when this block's session is SELF-CONTRADICTORY against the tracked one: the same
    `generation` cannot legitimately be reported under two different `id`s — a generation is
    Scout's own promise that it only advances on a genuine restart, and a restart is exactly
    what gives it a fresh id too. Checked BEFORE liveness updates in `ingest()`, so a report
    that violates its own side of the contract is distrusted wholesale, not merely rejected for
    membership purposes."""
    return cur is not None and generation == cur["generation"] and sid != cur["id"]


def _apply_session(st, sid, seq, generation, received_at, received_mono):
    """(outcome, apply_membership). Assumes `_session_conflict()` was already checked and was
    False. `outcome` is one of "new" / "advance_generation" / "advance_seq" / "unchanged" /
    "stale_seq" / "retired_generation" — the caller uses "new"/"advance_generation" to decide
    whether the change is worth persisting (see main.py's `_save_companion_sessions`).

    POLICY — publisher generations, not session-id trust:
    `session.generation` is an integer Scout persists across its own process restarts and
    increases on every one (see COMPANION_CONTRACT.md / SCOUT_INTEGRATION_HANDOFF.md). Ordering
    is PURE NUMERIC COMPARISON of `generation` — never a comparison of `id` strings, and NOT
    dependent on the outer envelope having a timestamp at all (contrast the superseded "trust
    any different session id" policy, which relied entirely on main.py's own per-vehicle
    timestamp guard and inherited that guard's gaps). A LOWER generation than the one already
    accepted for this vehicle is a RETIRED publisher and is rejected unconditionally, however
    recent the envelope carrying it — this is what stops a delayed snapshot from an already-
    superseded publisher run from ever modifying companion state again, in this process or (via
    the persisted high-water mark — see OPERATOR RESTARTS) across a restart of THIS process too.
    """
    cur = st["session"]
    if cur is None or generation > cur["generation"]:
        st["session"] = {"id": sid, "seq": seq, "generation": generation,
                         "started": _mark(received_at, received_mono)}
        return ("new" if cur is None else "advance_generation"), True
    if generation < cur["generation"]:
        _reject(st, "retired_session_generation", received_at)
        return "retired_generation", False
    # generation == cur["generation"] and sid == cur["id"] (conflict already ruled out) — normal
    # seq-level ordering WITHIN one generation, unchanged from before.
    if seq > cur["seq"]:
        cur["seq"] = seq
        return "advance_seq", True
    if seq == cur["seq"]:
        return "unchanged", True     # idempotent resend — safe, harmless to re-merge
    _reject(st, "stale_snapshot_seq", received_at)
    return "stale_seq", False


# --- measurement parsers (each returns (value, reason)) ---------------------------------

def _parse_position(raw, packet_ts, receipt):
    if not isinstance(raw, dict):
        return None, "bad_position"
    ll = _latlng(raw.get("lat"), raw.get("lng"))
    if ll is None:
        return None, "bad_coordinates"
    ev, why = _evidence(raw.get("observed_at"), packet_ts, receipt)
    if ev is None:
        return None, why
    heading = _finite(raw.get("heading_deg"))
    if heading is not None and not (0.0 <= heading <= 360.0):
        heading = None
    alt = _finite(raw.get("altitude_m"))
    ref = _token(raw.get("altitude_ref"))
    # An altitude without an explicit reference is not a measurement anyone can use: 25 m AGL
    # and 25 m AMSL are different aircraft positions. Drop it rather than guess the datum.
    if alt is None or ref not in ALTITUDE_REFS or not (MIN_ALTITUDE_M <= alt <= MAX_ALTITUDE_M):
        alt, ref = None, None
    return {**ev, "lat": ll[0], "lng": ll[1],
            "heading_deg": None if heading is None else heading % 360.0,
            "altitude_m": alt, "altitude_ref": ref}, None


def _parse_battery(raw, packet_ts, receipt):
    if not isinstance(raw, dict):
        return None, "bad_battery"
    pct = _finite(raw.get("remaining_pct"))
    if pct == -1:
        return None, "battery_unknown"      # MAVLink "unknown" sentinel — absence, not 0 %
    if pct is None or not (0.0 <= pct <= 100.0):
        return None, "bad_battery"
    ev, why = _evidence(raw.get("observed_at"), packet_ts, receipt)
    if ev is None:
        return None, why
    return {**ev, "remaining_pct": pct}, None


def _parse_inspection(raw, packet_ts, receipt):
    if not isinstance(raw, dict):
        return None, "bad_inspection"
    pct = _finite(raw.get("progress_pct"))
    if pct is None or not (0.0 <= pct <= 100.0):
        return None, "bad_inspection"
    ev, why = _evidence(raw.get("observed_at"), packet_ts, receipt)
    if ev is None:
        return None, why
    return {**ev, "progress_pct": pct}, None


def _parse_optional_time(raw_ts, packet_ts, receipt):
    ev, _ = _evidence(raw_ts, packet_ts, receipt) if raw_ts is not None else (None, None)
    return ev


def _parse_hazard(raw, packet_ts, receipt):
    if not isinstance(raw, dict):
        return None, "bad_hazard"
    hid = _ident(raw.get("hazard_id"))
    if hid is None:
        return None, "bad_hazard_id"
    rev = _int(raw.get("revision"))
    if rev is None:
        return None, "bad_hazard_revision"
    source = _text(raw.get("source"), 64)
    if source is None:
        return None, "missing_hazard_source"
    ev, why = _evidence(raw.get("observed_at"), packet_ts, receipt)
    if ev is None:
        return None, why
    geom = _geometry(raw.get("geometry"))
    if geom is None:
        return None, "bad_hazard_geometry"
    unc = None
    if raw.get("uncertainty_m") is not None:
        unc = _finite(raw.get("uncertainty_m"))
        if unc is None or not (0.0 <= unc <= MAX_UNCERTAINTY_M):
            return None, "bad_uncertainty"
    excl = None
    if raw.get("exclusion") is not None:
        excl = _geometry(raw.get("exclusion"))
        if excl is None or excl["type"] != "polygon":
            return None, "bad_exclusion"

    disp_raw = raw.get("disposition") if isinstance(raw.get("disposition"), dict) else {}
    disposition = {
        "state": _enum(disp_raw.get("state"), DISPOSITION_STATES),
        "reason": _text(disp_raw.get("reason")),
        "decided": _parse_optional_time(disp_raw.get("decided_at"), packet_ts, receipt),
    }
    replan = None
    if isinstance(raw.get("replan"), dict):
        rp = raw["replan"]
        replan = {
            "state": _enum(rp.get("state"), REPLAN_STATES),
            "mission_revision": _int(rp.get("mission_revision")),
            "route_hash": _text(rp.get("route_hash"), 100),
            "reason": _text(rp.get("reason")),
            "updated": _parse_optional_time(rp.get("updated_at"), packet_ts, receipt),
        }
    return {
        "hazard_id": hid, "revision": rev, "source": source,
        "kind": _token(raw.get("kind")) or "UNKNOWN",
        "observed": ev, "geometry": geom, "uncertainty_m": unc, "exclusion": excl,
        "disposition": disposition, "replan": replan,
    }, None


# --- merge ------------------------------------------------------------------------------

_MEASUREMENTS = (("position", _parse_position), ("battery", _parse_battery),
                 ("inspection", _parse_inspection))


def _merge_hazards(st, prev_hazards, raw_list, packet_ts, receipt):
    """A COMPLETE snapshot of this companion's hazards → the new {hazard_id: hazard}.

    Absent from the snapshot = removed. Within the snapshot a repeated id collapses to its
    highest revision. Against the stored set a lower revision never regresses a higher one,
    and an equal revision keeps the stored copy (a revision's content is immutable). A
    malformed entry whose id we already hold keeps the stored copy rather than deleting it."""
    received_at = receipt["mark"]["wall"]
    out = {}
    for raw in raw_list:
        hz, why = _parse_hazard(raw, packet_ts, receipt)
        if hz is None:
            _reject(st, why, received_at)
            hid = _ident(raw.get("hazard_id")) if isinstance(raw, dict) else None
            if hid and hid in prev_hazards and hid not in out:
                out[hid] = prev_hazards[hid]
            continue
        hid = hz["hazard_id"]
        if hid in out:
            _reject(st, "duplicate_hazard_id", received_at)
        prev = prev_hazards.get(hid)
        candidate = hz
        if prev is not None and prev["revision"] >= hz["revision"]:
            if prev["revision"] > hz["revision"]:
                _reject(st, "stale_hazard_revision", received_at)
            candidate = prev                  # never regress; equal revision is immutable
        if hid not in out or candidate["revision"] > out[hid]["revision"]:
            out[hid] = candidate
    return out


def _merge_companion(st, prev, raw, cid, parent, vtype, packet_ts, receipt):
    received_at = receipt["mark"]["wall"]
    received_mono = receipt["mark"]["mono"]
    rec = dict(prev) if prev else {
        "companion_id": cid, "link": {"state": "UNKNOWN", "last_peer_contact": None},
        "activity": None, "position": None, "battery": None, "inspection": None,
        "hazards": {}, "hazards_reported": False, "hazards_received": None,
    }
    rec["vehicle_type"] = vtype
    rec["parent_id"] = parent
    rec["display_name"] = _text(raw.get("display_name"), 48) or cid
    rec["assignment_state"] = _enum(raw.get("assignment"), ASSIGNMENT_STATES)
    rec["status_received"] = _mark(received_at, received_mono)

    # Link — Scout's CURRENT classification of its own Scout↔UAV link (taken as-is from the
    # newest accepted packet), plus the newest peer-contact time ever reported (monotonic in
    # OBSERVATION time — never regressed by a later packet reporting an older contact).
    link_raw = raw.get("link") if isinstance(raw.get("link"), dict) else {}
    last = (prev or {}).get("link", {}).get("last_peer_contact") if prev else None
    if link_raw.get("last_peer_contact_at") is not None:
        ev, why = _evidence(link_raw.get("last_peer_contact_at"), packet_ts, receipt)
        if ev is None:
            _reject(st, why, received_at)
        elif _newer(last, ev):
            last = ev
    rec["link"] = {"state": _enum(link_raw.get("state"), LINK_STATES), "last_peer_contact": last}

    if "activity" in raw:
        act = raw.get("activity")
        rec["activity"] = None if not isinstance(act, dict) else {
            "state": _token(act.get("state")) or "UNKNOWN",
            "task_id": _text(act.get("task_id"), 64),
            "route_revision": _int(act.get("route_revision")),
        }

    for key, parser in _MEASUREMENTS:
        if key not in raw:
            continue                       # omitted: keep last-known, do not refresh
        if raw[key] is None:
            rec[key] = None                # explicit null: Scout says there is no valid value
            continue
        new, why = parser(raw[key], packet_ts, receipt)
        if new is None:
            _reject(st, why, received_at)
            continue
        if _newer(rec.get(key), new):
            rec[key] = new
        elif new["observed_at"] < rec[key]["observed_at"]:
            _reject(st, "stale_observation", received_at)
        # equal observed_at: a duplicate — no-op, receipt clock of the ORIGINAL arrival is kept

    if "hazards" in raw:
        hz = raw.get("hazards")
        if hz is None:
            hz = []
        if not isinstance(hz, list):
            _reject(st, "hazards_not_list", received_at)
        elif len(hz) > MAX_HAZARDS:
            _reject(st, "too_many_hazards", received_at)
        else:
            rec["hazards"] = _merge_hazards(st, rec.get("hazards") or {}, hz, packet_ts, receipt)
            rec["hazards_reported"] = True
            rec["hazards_received"] = _mark(received_at, received_mono)
    return rec


def ingest(store, vid, payload, *, packet_ts, received_at, received_mono, resolve_parent):
    """Apply ONE accepted packet's companion block to vehicle `vid`'s own state.

    `packet_ts`     — the envelope's Scout-clock send time (None if the packet has none)
    `received_at`   — operator WALL-CLOCK epoch seconds of arrival (diagnostics, absolute time)
    `received_mono` — operator MONOTONIC-CLOCK reading at the same moment (elapsed-time math —
                      see the module docstring's EVIDENCE TIME section for why)
    `resolve_parent` — the station's canonical-id function; a companion whose declared parent
                       does not resolve to `vid` is refused (it is not this vehicle's).

    Call it ONLY for packets that passed the per-vehicle monotonic guard: a replayed older
    packet must not rewrite companion state either. See SNAPSHOT ORDERING / PUBLISHER
    GENERATIONS in the module docstring for the ADDITIONAL, companion-specific ordering this
    function enforces on top of that outer guard.

    Returns True when a NEW publisher generation was just adopted for this vehicle (first-ever
    session, or a genuine generation advance) — the caller's signal that this is worth
    persisting (see main.py's `_save_companion_sessions`); False in every other case (nothing
    ordering-relevant changed, or the block was rejected outright)."""
    if vid is None or not isinstance(payload, dict):
        return False
    block = payload.get("companions")
    if block is None:
        return False                         # omitted (or null): no information, no change
    st = store.setdefault(vid, new_state())
    if not isinstance(block, dict) or block.get("schema") != SCHEMA:
        _reject(st, "unsupported_schema", received_at)
        return False
    items = block.get("items")
    if not isinstance(items, list):
        _reject(st, "items_not_list", received_at)
        return False
    if len(items) > MAX_COMPANIONS:
        _reject(st, "too_many_companions", received_at)
        return False
    sid, seq, generation, why = _validate_session(block.get("session"))
    if why:
        # A malformed/missing session makes the WHOLE block not well-formed (like a bad schema
        # string) — no liveness update either, matching unsupported_schema/items_not_list.
        _reject(st, why, received_at)
        return False
    if _session_conflict(st["session"], sid, generation):
        # Self-contradictory (same generation, different id) — distrusted wholesale, no
        # liveness either: Scout's own report violates the contract it is supposed to keep.
        _reject(st, "inconsistent_session_generation", received_at)
        return False

    # Schema- and session-VALID (and internally consistent): this is a well-formed block, so
    # liveness (Scout is still telling us SOMETHING about companions) updates regardless of what
    # the generation/seq check below decides about MEMBERSHIP. These two concerns are
    # deliberately independent — see the module docstring's note on why `status_received` still
    # refreshes on an unchanged (or even rejected-for-membership) seq resend, distinct from
    # per-measurement `observed_at`, which never does.
    st["reported"] = True
    st["block_received"] = _mark(received_at, received_mono)

    receipt = _receipt(received_at, received_mono, packet_ts)
    outcome, apply_membership = _apply_session(st, sid, seq, generation, received_at, received_mono)
    if not apply_membership:
        return False   # stale seq or a retired generation — membership untouched, already logged

    prev_all = st["companions"]
    out = {}
    for raw in items:
        cid = _ident(raw.get("companion_id")) if isinstance(raw, dict) else None
        if cid is None:
            _reject(st, "bad_companion_id", received_at)
            continue
        if cid in out:
            _reject(st, "duplicate_companion_id", received_at)
            continue
        parent = resolve_parent(raw.get("parent_vehicle_id"))
        if parent is None or parent != vid:
            # Scout itself says this companion is not its own (or says nothing). It is not
            # stored here, and it is never re-homed onto the vehicle it names either — only a
            # parent's OWN packets may describe its companions.
            _reject(st, "parent_mismatch", received_at)
            continue
        vtype = _token(raw.get("vehicle_type"))
        if vtype is None:
            _reject(st, "bad_vehicle_type", received_at)
            if cid in prev_all:
                out[cid] = prev_all[cid]    # keep last-known, unrefreshed
            continue
        out[cid] = _merge_companion(st, prev_all.get(cid), raw, cid, parent, vtype,
                                    packet_ts, receipt)
    # The item list is a COMPLETE snapshot of Scout's companions: one not listed is gone.
    st["companions"] = out
    if not items:
        st["cleared_at"] = _mark(received_at, received_mono)
    return outcome in ("new", "advance_generation")


# --- publication ------------------------------------------------------------------------

def empty_block():
    """The block a vehicle that never reported companions carries — same shape, no items."""
    return {"schema": SCHEMA, "reported": False, "block_age_s": None, "cleared": False,
            "session": None, "items": [],
            "rejections": {"count": 0, "by_reason": {}, "last_reason": None}}


def _hazard_view(h, now_wall_s, now_mono_s):
    d, r = h["disposition"], h["replan"]
    return {
        "hazard_id": h["hazard_id"], "revision": h["revision"], "source": h["source"],
        "kind": h["kind"], "observed": _freshness(h["observed"], now_wall_s, now_mono_s),
        "geometry": h["geometry"], "uncertainty_m": h["uncertainty_m"],
        "exclusion": h["exclusion"],
        "disposition": {"state": d["state"], "reason": d["reason"],
                        "decided": _freshness(d["decided"], now_wall_s, now_mono_s)},
        "replan": None if r is None else {
            "state": r["state"], "mission_revision": r["mission_revision"],
            "route_hash": r["route_hash"], "reason": r["reason"],
            "updated": _freshness(r["updated"], now_wall_s, now_mono_s)},
    }


def _item_view(rec, now_wall_s, now_mono_s):
    def meas(key, fields):
        m = rec.get(key)
        if not m:
            return None
        fresh = _freshness(m, now_wall_s, now_mono_s)
        return {**{f: m[f] for f in fields}, **fresh}

    hazards = [_hazard_view(h, now_wall_s, now_mono_s) for h in rec["hazards"].values()]
    # Newest observation first (by the honest lower-bound age); an unknown age sorts last
    # rather than posing as newest.
    hazards.sort(key=lambda h: (h["observed"] is None or h["observed"]["min_age_s"] is None,
                                (h["observed"] or {}).get("min_age_s") or 0.0, h["hazard_id"]))
    link = rec["link"]
    return {
        "companion_id": rec["companion_id"],
        "vehicle_type": rec["vehicle_type"],
        "display_name": rec["display_name"],
        "parent_id": rec["parent_id"],
        "assignment_state": rec["assignment_state"],
        # How long since a Scout packet last LISTED this companion — the currency of every
        # Scout-reported claim below (link state, activity, assignment). Refreshes on every
        # accepted (non-stale-seq) block, independent of whether the CONTENT changed.
        "status_age_s": _since(rec.get("status_received"), now_mono_s),
        "link": {"state": link["state"],
                 "last_peer_contact": _freshness(link["last_peer_contact"], now_wall_s, now_mono_s)},
        "activity": rec["activity"],
        "position": meas("position", ("lat", "lng", "heading_deg", "altitude_m", "altitude_ref")),
        "battery": meas("battery", ("remaining_pct",)),
        "inspection": meas("inspection", ("progress_pct",)),
        "hazards_reported": rec["hazards_reported"],
        "hazards_age_s": _since(rec.get("hazards_received"), now_mono_s),
        "hazards": hazards,
    }


def fleet_block(store, vid, now_wall_s, now_mono_s):
    """The `companions` block for one fleet row, ages computed at `(now_wall_s, now_mono_s)`.

    `now_wall_s` is currently unused for elapsed-time math (every "how long ago" number is
    monotonic-clock-derived — see the module docstring) but is threaded through for parity with
    `ingest()` and so a future wall-clock-only diagnostic has it on hand without a signature
    change."""
    st = store.get(vid)
    if not st:
        return empty_block()
    rej = st["rejections"]
    session = st["session"]
    return {
        "schema": SCHEMA,
        "reported": st["reported"],
        "block_age_s": _since(st.get("block_received"), now_mono_s),
        "cleared": st["cleared_at"] is not None and not st["companions"],
        "session": None if session is None else {
            "id": session["id"], "seq": session["seq"], "generation": session["generation"],
            # None when this session was SEEDED from a persisted snapshot at startup rather
            # than actually observed in this process yet (see seed_session) — an honest "we
            # don't know how long ago this began", never a fabricated age.
            "age_s": _since(session.get("started"), now_mono_s),
        },
        "items": [_item_view(r, now_wall_s, now_mono_s) for r in st["companions"].values()],
        "rejections": {"count": rej["count"], "by_reason": dict(rej["by_reason"]),
                       "last_reason": rej["last_reason"]},
    }


# --- ordering-state persistence (survives an OPERATOR restart; see the module docstring's
# OPERATOR RESTARTS section) ---------------------------------------------------------------
#
# This module owns no file I/O (see the module docstring) — main.py does the actual reading and
# writing, using its own runtime-data directory and atomic-write conventions (the same ones the
# mission store uses). The two functions below are the pure boundary: exporting ONLY the
# ordering triplet worth persisting, and importing it back into a FRESH store without ever
# restoring companion identity, measurement or hazard data — a restored session establishes
# ordering PROTECTION, never presents restored EVIDENCE as current.

SESSION_SNAPSHOT_VERSION = 1


def session_snapshot(store):
    """{vehicle_id: {"id", "seq", "generation"}} for every vehicle with an established session —
    the ONLY thing worth persisting across an operator restart. Deliberately excludes
    everything else in `store`: companion identity, measurements, hazards and liveness are live
    evidence and must never be replayed as if they were still current after a restart."""
    out = {}
    for vid, st in store.items():
        s = st.get("session")
        if s is not None:
            out[vid] = {"id": s["id"], "seq": s["seq"], "generation": s["generation"]}
    return out


def seed_session(store, vid, *, session_id, seq, generation):
    """Restore ONLY the ordering metadata for `vid` from a prior process's persisted state —
    never any companion/measurement/hazard data, which always starts genuinely empty after a
    restart. Call this once per vehicle at startup, BEFORE any packet has been ingested for it
    in this process; `ingest()` then treats the seeded triplet as the current baseline exactly
    as if this process itself had accepted it, so a retired generation is rejected even though
    THIS process never actually saw the transition happen.

    `st["reported"]` is deliberately left False and `st["companions"]` empty: from this
    process's own point of view nothing has been reported yet, and "Scout is currently
    reporting" is a claim only THIS process's own received traffic may make."""
    st = store.setdefault(vid, new_state())
    st["session"] = {"id": session_id, "seq": seq, "generation": generation, "started": None}


def validate_session_snapshot(data):
    """{vehicle_key: {"id", "seq", "generation"}} from a persisted companion-sessions snapshot,
    or raise ValueError. Mirrors main.py's own `_validate_mission_store`: the WHOLE file is
    validated before anything is adopted, so one corrupt entry fails the file CLOSED instead of
    partially restoring ordering protection — a half-restored ordering state is exactly the kind
    of silent, partial protection the caller must never present as the real thing.

    Vehicle keys are returned exactly as given in `data` (main.py resolves them to canonical
    vehicle ids before calling `seed_session`; this module has no vehicle-identity concept of
    its own — see the module docstring, "No FastAPI, no globals")."""
    if not isinstance(data, dict):
        raise ValueError("snapshot root is not an object")
    if data.get("version") != SESSION_SNAPSHOT_VERSION:
        raise ValueError(f"unsupported snapshot version {data.get('version')!r} "
                         f"(this build reads {SESSION_SNAPSHOT_VERSION})")
    raw_vehicles = data.get("vehicles")
    if not isinstance(raw_vehicles, dict):
        raise ValueError("vehicles is not an object")
    out = {}
    for key, rec in raw_vehicles.items():
        if not isinstance(rec, dict):
            raise ValueError(f"entry for {key!r} is not an object")
        sid = _ident(rec.get("id"))
        seq = _int(rec.get("seq"))
        generation = _int(rec.get("generation"))
        if sid is None or seq is None or generation is None:
            raise ValueError(f"entry for {key!r} has an invalid id/seq/generation")
        out[key] = {"id": sid, "seq": seq, "generation": generation}
    return out
