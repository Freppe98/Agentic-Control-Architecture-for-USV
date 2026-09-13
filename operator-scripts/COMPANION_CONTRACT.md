# COMPANION_CONTRACT.md — `companion-v2` (reference)

**Status: PROPOSED. Implemented on the operator station; NOT yet implemented on Scout.**
Nothing in this document means a UAV, a UAV radio link, onboard perception or
obstacle replanning exists or works. It fixes the *report format* so that when Scout's
Local Mission Agent starts coordinating a UAV, the operator station already knows how to
receive, validate, order and age what Scout says about it.

This is the exhaustive reference. For a concise, example-first document written for the
developer implementing the Scout side, see **`SCOUT_INTEGRATION_HANDOFF.md`**.

Operator implementation: `companion_telemetry.py` (backend — read its module docstring
first, it is the authoritative rationale for everything below), `operator/lib/companion.js`
(view policy), `operator/components/CompanionPanel.js` and the Map page.
Tests: `tests/test_companion_telemetry.py`, `tests/companion.test.mjs`.
Fixture: `scripts/companion_fixture.py` (opt-in, loopback only).

**v2 is a deliberate, breaking revision over v1.** `companion-v1` was proposed but never
adopted by any real Scout build, so this is not a silent meaning-change of a schema anyone
relies on: a `companion-v1`-labelled block is now rejected exactly like any other
unrecognised schema string (`unsupported_schema`). v1 lacked two things v2 fixes:
1. **Snapshot-level ordering.** v1 had no way to tell a newer *envelope* carrying an
   *older* companion snapshot from a genuinely fresher one — only per-hazard `revision`
   existed, which protects one object's content but says nothing about membership (a
   hazard vanishing from a list has no revision of its own to compare).
2. **Honest freshness.** v1 collapsed "how stale was this observation" into a single
   `age_s` computed as `envelope.timestamp − observed_at` (Scout clock) plus elapsed time
   since the operator received the packet. That formula silently assumes the envelope was
   delivered instantly: an observation sent immediately but delayed 30 s in transit (e.g.
   buffered during a comms drop-out — this station's whole reason for existing) read as
   ~0 s old the moment it finally arrived.

**This revision (companion-v2, second pass) closes two further gaps found while exercising
the first v2 implementation, before Scout ever adopted it:**
3. **A retired publisher could come back.** The original v2 ordering trusted ANY different
   `session.id` as a genuine restart. That allowed: session A accepted → session B (a real
   restart) supersedes it → a DELAYED session-A snapshot, arriving inside a still-newer
   envelope, was trusted again as "another restart" and could silently un-do B's state. §4
   now orders sessions by an explicit `generation` integer, never by comparing ids.
4. **A small lower bound was being read as "fresh".** `reporting_delay_s` conflates real
   transit delay with Scout↔operator clock offset (§3) — the two can cancel out (Scout's
   clock 30 s ahead plus a genuine 30 s delivery delay computes as 0 s of delay), so it was
   never safe to use for a "not stale" verdict either, only `min_age_s` is. §3 now names the
   resulting three-way state explicitly: **STALE** (proven), **UNCERTAIN** (not proven
   either way — the common, honest case), or **UNKNOWN** (no timestamp at all). There is no
   **FRESH** state — this contract does not claim one, and Scout should not expect the
   operator UI to ever display one for an individual measurement.

---

## 1. Model

- The UAV talks to **Scout**. The operator hears about the UAV **only through Scout's
  existing `POST /agent/status`**. No new endpoint, no new transport.
- **This is a reporting contract, not a radio protocol.** It says nothing about how Scout
  talks to the UAV, and it is emphatically NOT a requirement to broadcast full hazard
  polygons over a radio link at every heartbeat. A future UAV↔Scout radio gateway may use
  acknowledged updates, bounded/chunked transfers, and periodic full snapshots with
  lightweight heartbeats in between — none of that is designed here. What IS fixed is the
  Scout→operator side: only a **completely assembled and validated** snapshot may ever
  enter `payload.companions` — the operator never sees or assembles fragments, and never
  has to reconcile a partial transfer. Three separate facts, kept apart everywhere in this
  document: **link receipt** (did Scout get anything from the UAV at all — `link.state` +
  `last_peer_contact`), **observation freshness** (how old is what it got — the freshness
  object, §3), and **application acceptance** (did the OPERATOR's own validation accept
  this report — `rejections`, and whether it advanced `companions.session.seq`). A
  companion status packet reaching Scout does not by itself prove the operator accepted it.
- A companion is **not a fleet vehicle**. It has no fleet id, no command route, no control
  authority and no mission on the operator station. It is state that belongs to ONE parent
  USV and is published only on that parent's fleet row.
- Scout owns the facts: assignment, link state, hazard validation, the decision to replan,
  and the verified mission-replacement transaction. The operator **displays** Scout's
  statements with the freshness of their evidence. It never modifies the operator's own
  mission, planning draft or approved geometry because of a hazard, and never infers that
  an upload succeeded.
- **A hazard disappearing from the operator's display is a DIFFERENT fact from Scout
  removing an exclusion from its own onboard plan.** The operator only ever reflects what
  Scout currently *reports*; if a hazard stops appearing in a snapshot, that means Scout
  stopped reporting it to the operator — it is never proof Scout stopped avoiding it in its
  own flight plan, which remains entirely Scout's own decision and is invisible to this
  contract. Likewise, an inspected area is never "confirmed safe" by anything here.

## 2. Conventions

| Item | Rule |
|---|---|
| Location | `payload.companions` inside the existing status envelope. |
| Schema | `"schema": "companion-v2"` — any other value (including `companion-v1`) rejects the whole block. |
| Coordinates | WGS-84 decimal degrees as **objects** `{"lat": 56.7003, "lng": 13.0035}`. Never bare arrays: that removes the `[lat,lng]` vs GeoJSON `[lng,lat]` ambiguity. `lat ∈ [-90, 90]`, `lng ∈ [-180, 180]`; `(0, 0)` is refused as a no-fix sentinel. |
| Timestamps | Unix epoch **seconds** (float), on **Scout's clock** — the same clock as the envelope `timestamp` (`time.time()`). Scout must convert UAV-side times to its own clock before reporting. |
| Envelope timestamp | Required for freshness AND for the ordering guarantee (§4). Without it, every companion freshness reads `UNKNOWN` (never guessed), and a session change carries no ordering protection at all (§4). |
| Units | metres, degrees (heading 0–360, true north), percent 0–100. |
| Altitude | `altitude_m` **with** `altitude_ref` ∈ `AMSL` · `AGL` · `RELATIVE_HOME`. An altitude without a valid reference is dropped. |
| Identifiers | `companion_id`, `hazard_id`, `session.id`: lowercase `[a-z0-9][a-z0-9_.:-]{0,47}`, stable for the life of the object. |
| `session.generation` | Non-negative integer. Scout **MUST persist it across its own process restarts** and increase it by at least 1 on every restart of the companion-reporting subsystem — see §4. It is never reset to 0 by a restart. |
| Tokens | Uppercase `[A-Z][A-Z0-9_]*`. Unrecognised link / disposition / replan tokens are shown as UNKNOWN — never promoted. |
| Numbers | Finite only. NaN / Infinity are refused (and stripped at ingest, station-wide). Booleans are not numbers. |

## 3. Freshness — three numbers, never collapsed into one

Every timestamped fact (`position.observed_at`, `battery.observed_at`,
`inspection.observed_at`, `link.last_peer_contact_at`, a hazard's own `observed_at`, its
`disposition.decided_at`, its `replan.updated_at`) is reported the same way — one field,
`observed_at`, on Scout's clock — and the operator turns it into THREE distinct,
separately-published numbers. **Scout only ever sends `observed_at`; everything below is
computed by the operator and described here so both sides agree on what the published
numbers mean.**

```
age_at_source_s          = envelope.timestamp − observed_at        (Scout clock only, EXACT)
elapsed_since_receipt_s   = now_monotonic − receipt_monotonic       (operator clock only, EXACT,
                                                                      immune to wall-clock jumps)
min_age_s  = age_at_source_s + elapsed_since_receipt_s
  → a GUARANTEED LOWER BOUND. Needs no cross-clock assumption at all — both terms are
    single-clock deltas. It can only ever UNDERSTATE the true age (real delivery delay is
    always ≥ 0), never overstate it, because it assumes the envelope was delivered the
    instant it was sent.

reporting_delay_s = max(0, receipt_wall − envelope.timestamp)
  → an ESTIMATE, computed once per envelope and shared by every piece of evidence in it.
    This compares the OPERATOR's wall clock against SCOUT's envelope timestamp, which
    CONFLATES genuine transit/buffering delay with any clock offset between the two hosts.
    It is never proof of transit time and is never used as a substitute for min_age_s — it
    supplements it. A raw (unclamped) value more negative than −5 s (the envelope looks
    like it is from the future relative to the operator's own clock) is flagged separately
    as `clock_anomaly` rather than silently produced as "0 s of delay".

estimated_age_s = min_age_s + reporting_delay_s
  → a best-effort correction, assuming the two clocks are roughly aligned. Exposed
    ALONGSIDE min_age_s, never instead of it, so a consumer can always fall back to the
    honest lower bound.
```

Published shape (replaces every old single `age_s` field):

```jsonc
{
  "observed_at": 1786175326.5,        // source timestamp, preserved verbatim
  "min_age_s": 0.1,                   // guaranteed lower bound
  "reporting_delay_s": 30.0,          // estimate — conflates transit delay with clock offset
  "estimated_age_s": 30.1,            // min_age_s + reporting_delay_s — display-only
  "freshness_quality": "LOWER_BOUND", // or "UNKNOWN"
  "clock_anomaly": false
}
```

`freshness_quality` is `"LOWER_BOUND"` when the envelope carried a timestamp (all three
numbers above are meaningful) or `"UNKNOWN"` when it did not — in the `UNKNOWN` case every
one of `min_age_s` / `reporting_delay_s` / `estimated_age_s` is `null`. There is no fallback
to "assume it just arrived": that silent assumption is exactly the bug this model exists to
close. `observed_at` is still preserved even when quality is `UNKNOWN`, so the raw source
timestamp is never lost, only its relationship to "now" is unknown.

**This value was renamed from `"BOUNDED"` in the first companion-v2 pass.** `"BOUNDED"` read
as if the age were bounded on *both* ends (a known maximum age, i.e. proof of freshness) —
it never was. All it ever meant is "a valid lower bound exists." `"LOWER_BOUND"` says exactly
that and nothing more; it must never be read as a guarantee the observation is recent.

**Classification: STALE, UNCERTAIN, or UNKNOWN — there is deliberately no system-asserted
"FRESH".** Only `min_age_s` — the guaranteed, single-clock lower bound — may ever be used to
decide staleness, because it is the only one of the three numbers immune to clock-offset
error. Concretely:

- **STALE** — `min_age_s` exceeds the staleness threshold (`EVIDENCE_STALE_S`). This is a
  PROVEN claim: the true age is *at least* `min_age_s`, so it is definitely too old.
- **UNCERTAIN** — `min_age_s` is at or below the threshold. This does **not** mean the
  observation is fresh — it only means the lower bound failed to prove staleness. The true
  age could still be much larger; `min_age_s` is a floor, not an estimate.
- **UNKNOWN** — no envelope timestamp at all, so not even a lower bound exists. Treated with
  the SAME "cannot be presented as current" protection as STALE (see `evidenceView()` in
  `operator/lib/companion.js`): not knowing the age at all is a weaker position than having a
  known lower bound, never a better one.

**Why UNCERTAIN can never be promoted to FRESH — the counterexample this model is built
around:** `reporting_delay_s = max(0, receipt_wall − envelope.timestamp)` compares the
operator's wall clock against Scout's envelope timestamp — it CONFLATES genuine transit
delay with clock offset between the two hosts, and the two can cancel out. If Scout's clock
runs 30 s ahead of the operator's, and the envelope is genuinely delayed 30 s in delivery
(e.g. buffered during a comms drop-out), `reporting_delay_s` computes to **0 s** — a
genuinely 30-second-old-by-the-time-it-arrives observation looks like it arrived instantly.
`estimated_age_s` (which adds `reporting_delay_s` to `min_age_s`) can therefore be
misleadingly small in exactly the scenario this station exists to survive. This is why
`estimated_age_s` is a **display-only estimate** and is NEVER consulted by any automated
stale/fresh decision anywhere in this station — only `min_age_s` is.

**Assumptions even the lower bound (`min_age_s`) depends on** — it is the strongest number
this contract has, but it is not unconditional:
1. **Scout's own clock is self-consistent** between the moment it stamped `observed_at` and
   the moment it stamped the envelope `timestamp` — both computed from the same clock
   without an intervening jump.
2. **The operator's local clock is continuous** across `elapsed_since_receipt_s` — this is
   why that arithmetic runs on `time.monotonic()`, never wall-clock time, so an operator-side
   NTP step or manual clock change cannot corrupt it.
3. **Scout's UAV→Scout timestamp conversion is trusted as reported** — this contract has no
   way to independently verify that Scout correctly translated the UAV's own onboard clock
   into Scout's clock before stamping `observed_at`; a bug on that translation step degrades
   every number derived from it, including `min_age_s`.

None of these three assumptions is validated by anything in this contract or its
implementation — they are the trust boundary of the whole freshness model, stated here so
neither side mistakes `min_age_s` for something unconditionally true.

**What the frontend does with this:** `operator/lib/companion.js`'s `evidenceView()` is the
one place these numbers become a classification and display text. A reporting delay over
5 s is called out explicitly in the text ("at least Ns ago (delivery delay ~Ds)") as a
labelled *estimate*, never folded into a verdict. UNCERTAIN evidence gets a light caveat in
the detail panel ("recency not confirmed" / "not a confirmed current fix" / "current
reachability is not independently confirmed") — distinct from the amber "LAST KNOWN"
treatment, which stays reserved for PROVEN-stale (and UNKNOWN) evidence. Local elapsed-time
arithmetic (`elapsed_since_receipt_s`, and the equivalent used for the block/status/
hazards-list "how long since Scout last confirmed this" liveness fields) runs on Python's
`time.monotonic()`, never wall-clock time.

**What Scout can optionally improve on this:** nothing today — there is no field for Scout
to supply its own validated clock-offset or delivery-delay bound yet (P3 in
`BACKEND_ROADMAP.md`), and this revision deliberately does not invent one: without a
justified upper bound on delivery delay, current freshness stays UNCERTAIN rather than being
guessed at. If a future need justifies such a mechanism, it would be additive and optional;
legacy packets without it keep exactly the behaviour described here — a useful lower bound
and a clearly-labelled estimate, never a fabricated "fresh" verdict.

## 4. Snapshot ordering (session/generation/seq) — on top of per-hazard revisions

Per-hazard `revision` (§7) protects the content of ONE hazard object; it says nothing about
*membership* — a hazard can vanish from a snapshot with no revision of its own to compare.
`companions.session {id, seq, generation}` is the block-level ordering primitive that
protects the *complete set* of companions and, within each companion, the complete set of
hazards — including across a publisher restart.

```jsonc
"companions": {
  "schema": "companion-v2",
  "session": { "id": "boot-3f9a2c1e", "seq": 42, "generation": 3 },
  "items": [ ... ]
}
```

- `session.id` — identifies ONE continuous run of Scout's companion-reporting subsystem
  (simplest: regenerate at every process start — a random id, or boot-time + PID). Required.
  **Never compared against another id to decide which is newer** — ids are opaque, and
  ordering them lexicographically would be meaningless (a `session.id` starting with `"a"`
  is not "older" than one starting with `"z"`). Its only job is to detect an inconsistent
  generation (see below).
- `session.generation` — a non-negative integer that Scout **MUST persist across its own
  process restarts** (e.g. a small local counter file) and **increase by at least 1 every
  time the companion-reporting subsystem starts**, including the very first start after a
  fresh install (start from generation 0 or 1, Scout's choice, and always increase from
  there). This is the ONLY thing the operator uses to decide "is this a newer publisher run
  than the one I am tracking" — never the envelope timestamp, never the session id.
- `session.seq` — a non-negative integer, no required starting value, that Scout **MUST
  strictly increase on ANY content change within the current session/generation —
  including a companion or hazard being removed**, which has no revision of its own to
  signal the change. Repeating the *exact same* content with the *same* `seq` is normal and
  expected (a steady heartbeat); the operator treats that as idempotent.

**Ordering policy (`companion_telemetry.py`'s `_apply_session`), compared against the
per-vehicle tracked `{id, seq, generation}`:**

| This block's `generation`, compared to tracked | This block's `id` | Operator does |
|---|---|---|
| strictly greater | any | **adopted as a genuine restart** — membership replaces from this block onward, `seq` counter resets to this block's value, tracked `id`/`generation` updated |
| equal | same as tracked `id` | ordinary same-session handling: `seq` greater → applies (§6); `seq` equal → idempotent, liveness still refreshes; `seq` lower → **rejected**, counted under `stale_snapshot_seq` |
| equal | **different** from tracked `id` | **rejected wholesale as `inconsistent_session_generation`** — two different publisher identities claiming the same generation number is a contradiction the operator cannot resolve; membership untouched, and **no liveness update either** (this block is not trusted even as evidence Scout is alive) |
| strictly lower | any | **rejected wholesale as `retired_session_generation`**, regardless of whether the id is one already seen or a completely unfamiliar one — see "unfamiliar but older" below |
| missing/malformed `session` (no `id`, no/negative `seq`, no/negative `generation`, `session` not an object) | — | the WHOLE block is not well-formed — rejected like a bad `schema` string, no liveness update either |

**Why generation, not session-id trust, and why this does not depend on the envelope having
a timestamp.** The first companion-v2 pass trusted ANY different `session.id` as a genuine
restart, reasoning that `companion_telemetry.ingest()` only runs for envelopes that already
passed `main.py`'s own per-vehicle monotonic timestamp guard. That reasoning had a hole: the
outer guard only proves the *envelope* is not older than one already accepted — it says
nothing about whether the *session snapshot carried inside it* is current. A concrete failure
this exposed: session A is accepted; session B (a real restart) supersedes it; a **delayed**
session-A snapshot then arrives inside a still-newer envelope (e.g. buffered during a
transient hiccup in Scout's own reporting path) — the old id-trust rule would read "different
id from what I'm tracking" and treat the stale A snapshot as *another* restart, silently
undoing B's state. The generation check closes this: a lower generation is rejected
unconditionally, independent of the envelope timestamp, independent of whether the id has
been seen before, and independent of how much wall-clock time has passed. **This is why the
per-vehicle envelope-timestamp guard in `main.py` staying intact and unweakened does not by
itself make session ordering safe — the generation check is a second, independent layer on
top of it, not a substitute for it, and does not depend on it being reachable.**

**"Unfamiliar but older" — a session id this operator process has never seen before, at a
generation lower than the one it is tracking** (e.g. because the operator itself restarted
and never observed Scout's most recent generation directly, so has no prior memory of *any*
id at that generation): still rejected, by generation number alone, never by id recognition.
Retired-id tracking is not how this works and is not needed for it to work — the operator
does not need to have seen an id before to reject it; it only needs to know the generation
number is not higher than the highest one it has tracked for that vehicle. This is exactly
why generation is compared numerically rather than membership being checked against a set of
previously-seen ids: a set of "known retired ids" would fail exactly this case (an id it has
never seen, at a generation that is nonetheless not new).

**Operator restarts — session ordering survives them.** On every accepted generation change
(a genuine restart), the operator persists ONLY the ordering triple `{id, seq, generation}`
per vehicle to `runtime_data/companion_sessions.json` (atomic write: temp file + `os.replace`
+ `fsync`, the same convention `_save_mission_store()` uses) — never the companion, hazard,
position, battery or activity data itself. On the operator's own next startup, this file is
loaded and, for each vehicle it names, only the ordering triple is restored
(`companion_telemetry.seed_session()`); the companion's displayed data starts genuinely empty
("no companion reported") until Scout reports again — a restored ordering triple is never
presented as current information about the companion. If the file is missing, unreadable, or
fails validation (wrong version, malformed vehicle entry, negative/non-integer field), the
operator logs the problem and starts with **no** persisted ordering state for the affected
vehicle(s) — it never guesses or falls back to trusting an unverified snapshot, matching the
fail-closed pattern `_load_mission_store()` already uses for the mission store. One practical
consequence Scout should know about: persistence happens only when a generation actually
changes, not on every `seq` bump, so a `seq`-level replay arriving in the short window right
after an operator restart but before Scout's next real packet is not fully protected — a
retired-*generation* replay is always protected regardless of timing, because rejecting it
does not depend on the operator having observed that generation change itself.

A companion-v1 payload (no `session` at all) is simply rejected under this contract — see
the top of this document for why that gap was not worth patching around.

**Residual, separate limitation — read this if you are Scout:** all of the above assumes the
per-vehicle envelope-timestamp guard in `main.py` is working normally. That guard has its own
pre-existing, unrelated limitation — described immediately below — which is a property of
that outer guard, not of the session/generation mechanism, and is not fixed by it.

**Found while testing (fixture `clock_discontinuity`), documented, not fixed here:** the
existing per-vehicle envelope-timestamp guard in `main.py` (unrelated to companion telemetry,
pre-dating this contract) only rejects a timestamp that goes BACKWARD relative to the
highest one already accepted for that vehicle. It has no upper bound: an envelope whose own
`timestamp` is far in the future is *accepted*, and that raises the vehicle's high-water mark
into the future. Every subsequent packet with a normal (real-time) timestamp is then rejected
by that OUTER guard — before it ever reaches `companion_telemetry.py` — until real time
catches up. This is a property of the pre-existing guard, not of the companion module, which
deliberately does not touch or duplicate it (per the scope boundary stated throughout this
document). `companion_telemetry.ingest()`'s own session/seq ordering is unaffected and would
behave correctly if reached — it simply is not reached while the outer guard is rejecting the
envelope outright. See `tests/test_companion_telemetry.py`'s
`test_clock_discontinuity_via_the_real_endpoint_poisons_the_outer_guard_not_this_module` and
`scripts/companion_fixture.py`'s `clock_discontinuity` scenario (deliberately last in
`ALL_ORDER` for exactly this reason).

## 5. Block shape

```jsonc
"companions": {
  "schema": "companion-v2",
  "session": { "id": "boot-3f9a2c1e", "seq": 42, "generation": 3 },
  "items": [ /* COMPLETE list of Scout's companions, max 4 */ {
    "companion_id": "uav-1",               // required
    "vehicle_type": "UAV",                 // required token
    "parent_vehicle_id": "usv-2",          // required; must resolve to the REPORTING vehicle
    "display_name": "UAV-1",               // optional, ≤ 48 chars (defaults to companion_id)
    "assignment": "ASSIGNED",              // ASSIGNED | UNASSIGNED
    "link": {                              // Scout's OWN view of its Scout↔UAV link
      "state": "CONNECTED",                // CONNECTED | DEGRADED | LOST | NEVER_CONNECTED | UNKNOWN
      "last_peer_contact_at": 1786175326.6 // when Scout last received ANYTHING from the UAV; null = never
    },
    "activity": {                          // optional; null clears
      "state": "INSPECTING",               // IDLE | STANDBY | LAUNCHING | TRANSIT | INSPECTING | HOLDING | RETURNING | LANDING | LANDED | UNKNOWN
      "task_id": "insp-0001",              // optional
      "route_revision": 3                  // optional: the Scout mission revision the task was planned against
    },
    "position": {                          // optional; null clears
      "lat": 56.7003, "lng": 13.0035,
      "heading_deg": 45.0,                 // optional
      "altitude_m": 25.0, "altitude_ref": "AGL",   // optional, together
      "observed_at": 1786175326.5          // required: UAV fix time on Scout's clock
    },
    "battery":    { "remaining_pct": 71.0, "observed_at": 1786175326.5 },  // optional; -1 = unknown
    "inspection": { "progress_pct": 40.0,  "observed_at": 1786175326.5 },  // optional; Scout's number
    "hazards": [ /* COMPLETE list for this companion, max 32 — see §7 */ ]
  } ]
}
```

**Do not** send signal strength unless Scout measures it; this contract deliberately has no
RSSI field, and the operator never manufactures one. Link *state* and *last peer contact*
are the two facts; the operator additionally shows how old the report itself is.

The four facts the operator keeps apart and never derives from one another:
**assignment** (`assignment`), **reachability** (`link`), **task activity** (`activity`),
**fresh position evidence** (`position.observed_at`).

## 6. Omission, clearing and removal

| Scout sends | Operator does |
|---|---|
| no `companions` key (every pre-companion Scout) | nothing — last-known assignment kept, **not refreshed**; its ages keep growing. |
| `"companions": null` | same as omission. |
| `{"schema": "companion-v2", "session": {...}, "items": []}` at a NEW/higher `seq` | **explicit clear**: Scout has no companions. Indicator disappears. |
| `items` without a previously listed companion, in a snapshot that APPLIES (§4) | that companion is **removed** (the list is a complete snapshot). |
| a companion without the `position` / `battery` / `inspection` / `activity` / `hazards` key | that part is kept as last-known, **not refreshed**. |
| that key with `null` | that part is **cleared** ("not reported"). |
| `"hazards": []` in an APPLIED snapshot | all hazards of that companion cleared. |
| a hazard missing from an APPLIED `hazards` list | that hazard is **removed**. |
| a structurally broken transfer (e.g. `items` is not a list, `session` malformed) | rejected wholesale — **NEVER interpreted as an empty/clear snapshot**, even if it superficially resembles one. |

Updates are **complete snapshots**, not increments. **Scout must keep sending its current
truth on every packet, including "no companions" — clearing is a STATE, not a one-shot
event.** (An earlier draft of this contract said "send `items: []` once"; that was wrong,
and is corrected here: a single lost "clear" packet must never leave a stale assignment on
screen forever, and it does not, as long as Scout keeps resending its current truth exactly
as it does for every other continuously-reported field like `telemetry` or `mission_state`.
The very next repeated `items: []` at an advancing `seq` clears the state normally — nothing
special has to happen for this to work, but only if Scout actually keeps sending it.) The
same principle applies to a removed hazard: Scout keeps omitting it from every subsequent
snapshot, not just the one packet where it first disappeared.

If Scout stops reporting entirely, every companion fact stays on screen as **last known**
with its age (and ages keep growing correctly, on the monotonic clock — see §3); Scout's
last `CONNECTED` link claim is never presented as current.

## 7. Hazards

```jsonc
{
  "hazard_id": "hz-buoy-1",             // required, stable
  "revision": 2,                        // required int ≥ 0 — MUST increase on ANY change
  "source": "uav-1:eo_camera",          // required, ≤ 64 chars: which companion/sensor produced it
  "kind": "BUOY",                       // token: BUOY | DEBRIS | VESSEL | SHALLOWS | UNKNOWN | …
  "observed_at": 1786175320.0,          // required: when the observation was made (Scout clock)
  "geometry": { "type": "point", "lat": 56.7006, "lng": 13.0042 },
  //   or     { "type": "polygon", "ring": [ {"lat":…,"lng":…}, … ] }
  "uncertainty_m": 8.0,                 // optional, 0–1000
  "exclusion": { "type": "polygon", "ring": [ … ] },   // optional proposed/accepted exclusion area
  "disposition": {                      // Scout's decision about the observation
    "state": "ACCEPTED",                // REPORTED | UNDER_REVIEW | ACCEPTED | REJECTED | EXPIRED
    "reason": "Confirmed against radar", "decided_at": 1786175322.0
  },
  "replan": {                           // optional; absent = "not reported"
    "state": "IN_PROGRESS",             // NOT_REQUIRED | PENDING | IN_PROGRESS | VERIFIED | FAILED | BLOCKED
    "mission_revision": 4,              // for VERIFIED: the revision Scout verified on the vehicle
    "route_hash": "sha256:…",           // for VERIFIED: its route hash
    "reason": "…", "updated_at": 1786175324.0
  }
}
```

- **`uncertainty_m`** — horizontal radius in metres within which the true hazard boundary
  lies with ~95 % confidence, as estimated by the UAV/Scout. For a point it is drawn as a
  circle of that radius; for a polygon it is shown as text. The operator never pads it.
- **Polygons**: 3–64 vertices (a closing vertex equal to the first is allowed), simple (no
  self-intersection), non-zero area, bounding-box diagonal ≤ 5 km.
- **`exclusion`** is the area proposed to be avoided. It is drawn as *proposed* (amber,
  dashed) until `disposition.state` is exactly `ACCEPTED`, then as Scout's accepted
  exclusion (magenta, solid). It is never merged into the operator's mission or plan.
- **Revisions are immutable**: `(hazard_id, revision)` identifies one exact content. A lower
  revision never replaces a higher one; a second copy of the same revision is ignored; a
  repeated id within one list collapses to its highest revision. Bump the revision when the
  geometry, disposition or replan state changes. Revision protects ONE object's content —
  it does NOT protect against the object disappearing from a list altogether; that is what
  `session.seq` (§4) is for.

### Lifecycle (every state is Scout's; the operator only displays it)

```
REPORTED ─► UNDER_REVIEW ─► ACCEPTED ─► replan: PENDING ─► IN_PROGRESS ─► VERIFIED
                        │            │                               └─► FAILED / BLOCKED
                        │            └─► replan: NOT_REQUIRED
                        └─► REJECTED            (any) ─► EXPIRED
```

| What the operator sees | Requires Scout to send |
|---|---|
| Hazard reported | `disposition.state` REPORTED / UNDER_REVIEW (or missing → "Scout response not reported") |
| Hazard accepted | `disposition.state = ACCEPTED` — nothing else implies it |
| Replanning in progress | `replan.state` PENDING / IN_PROGRESS |
| Revised mission verified | `replan.state = VERIFIED` (+ `mission_revision` / `route_hash`) — **VERIFIED is a Scout-reported outcome and MUST only be sent after Scout's own read-back/hash verification of the revised mission on the vehicle; the operator never infers it from anything else** |
| Replanning failed / blocked | `replan.state` FAILED / BLOCKED (+ `reason`) |

The operator never infers VERIFIED from receiving a hazard, an ACCEPTED disposition, or a
changed route; missing outcome evidence stays **not reported**. Companion activity, hazard
disposition and mission-verification outcome are three DISTINCT facts and are never merged
or inferred from one another anywhere in this station.

## 8. Validation and bounds

| Rule | On violation |
|---|---|
| `schema` ≠ `companion-v2` (including `companion-v1`), `items` not a list, > 4 companions | whole block rejected; last-known kept, no liveness update |
| missing/malformed `session` (no `id`, no/negative `seq`, no/negative `generation`, `session` not an object) | whole block rejected as `bad_session_generation` (or the equivalent bad-`id`/bad-`seq` reason); last-known kept, no liveness update |
| `session.generation` behind the tracked generation for that vehicle | whole block rejected as `retired_session_generation` — regardless of whether the id has been seen before (§4); no liveness update |
| `session.generation` equal to tracked, but `session.id` different from the tracked id | whole block rejected as `inconsistent_session_generation` — two publishers cannot share one generation number; no liveness update |
| `session.seq` behind the tracked seq for the same `session.id`/`generation` | whole block's membership rejected (`stale_snapshot_seq`); liveness DOES still update |
| bad `companion_id`, duplicate id in one list | that item skipped |
| `parent_vehicle_id` missing or not the reporting vehicle | item refused — never re-homed onto the vehicle it names |
| bad `vehicle_type` | item skipped; a previously known companion kept unrefreshed |
| bad coordinates / nonfinite / future or > 24 h old `observed_at` | that measurement refused; previous kept |
| an observation older than the stored one | ignored (out of order); equal = duplicate, no-op, ORIGINAL receipt clock kept |
| > 32 hazards | that hazard list rejected; previous list kept |
| bad hazard (id, revision, source, time, geometry, uncertainty, exclusion) | that hazard refused; a known hazard with that id kept |

Every rejection is counted per reason on the fleet row (`companions.rejections`).

The whole envelope is also subject to the existing per-vehicle monotonic guard in `main.py`:
a packet whose envelope `timestamp` is older than the newest accepted one for that vehicle
changes no companion state (and never even reaches `companion_telemetry.py`).

## 9. Examples

Envelope (unchanged) — only `payload.companions` is new:

```json
{"message_type":"status","schema_version":"1.0","source":"usv-2","target":"operator",
 "timestamp":1786175327.0,
 "payload":{"usv_id":2,"name":"Scout","comm_state":"CONNECTED","telemetry":{"...":"..."},
            "companions":{"schema":"companion-v2",
                          "session":{"id":"boot-3f9a2c1e","seq":12,"generation":3},
                          "items":[ ... ]}}}
```

**a) Assigned, no contact yet**
```json
{"companion_id":"uav-1","vehicle_type":"UAV","parent_vehicle_id":"usv-2","display_name":"UAV-1",
 "assignment":"ASSIGNED","link":{"state":"NEVER_CONNECTED","last_peer_contact_at":null},
 "activity":{"state":"IDLE"},"hazards":[]}
```

**b) Active inspection, fresh position**
```json
{"companion_id":"uav-1","vehicle_type":"UAV","parent_vehicle_id":"usv-2","display_name":"UAV-1",
 "assignment":"ASSIGNED","link":{"state":"CONNECTED","last_peer_contact_at":1786175326.6},
 "activity":{"state":"INSPECTING","task_id":"insp-0001","route_revision":3},
 "position":{"lat":56.7003,"lng":13.0035,"heading_deg":45.0,"altitude_m":25.0,
             "altitude_ref":"AGL","observed_at":1786175326.5},
 "battery":{"remaining_pct":71.0,"observed_at":1786175326.5},
 "inspection":{"progress_pct":40.0,"observed_at":1786175326.5},"hazards":[]}
```

**c) Proposed buoy exclusion** (inside the companion's `hazards`)
```json
{"hazard_id":"hz-buoy-1","revision":1,"source":"uav-1:eo_camera","kind":"BUOY",
 "observed_at":1786175320.0,"geometry":{"type":"point","lat":56.7006,"lng":13.0042},
 "uncertainty_m":8.0,
 "exclusion":{"type":"polygon","ring":[{"lat":56.70038,"lng":13.00379},{"lat":56.70038,"lng":13.00461},
                                       {"lat":56.70082,"lng":13.00461},{"lat":56.70082,"lng":13.00379}]},
 "disposition":{"state":"REPORTED"}}
```
(the enclosing block's `session.seq` advances vs. the previous packet, since content changed)

**d) Scout accepts it and replans** — same hazard, `revision` 2:
```json
{"hazard_id":"hz-buoy-1","revision":2, "...":"same observation fields",
 "disposition":{"state":"ACCEPTED","reason":"Exclusion accepted","decided_at":1786175322.0},
 "replan":{"state":"IN_PROGRESS","reason":"Computing a detour","updated_at":1786175322.0}}
```

**e) Revised mission verified** — `revision` 3:
```json
"replan":{"state":"VERIFIED","mission_revision":4,"route_hash":"sha256:…","updated_at":1786175335.0}
```

**f) UAV link lost** — Scout keeps reporting (fresh envelope), the UAV data is old:
```json
{"companion_id":"uav-1", "...":"...",
 "link":{"state":"LOST","last_peer_contact_at":1786175300.0},
 "activity":{"state":"UNKNOWN"},
 "position":{"lat":56.7003,"lng":13.0035,"altitude_m":25.0,"altitude_ref":"AGL","observed_at":1786175300.0}}
```
The operator's `min_age_s` reflects this correctly regardless of how fresh the envelope is.

**g) Delayed delivery** — an envelope composed at `t=1000.0` (observation nearly instant,
`observed_at≈999.8`) but not actually delivered to the operator until wall-clock `t=1030.0`
(30 s of buffering, e.g. during a comms drop-out): `min_age_s` still reads ≈0.2 s (it assumes
zero delivery delay by design), but `reporting_delay_s`≈30 and `estimated_age_s`≈30.2 —
the operator's display uses the latter, so this is NOT shown as fresh.

**h) Explicit clear, resent every packet** — every subsequent packet, not just the first:
```json
"companions":{"schema":"companion-v2","session":{"id":"boot-3f9a2c1e","seq":13,"generation":3},"items":[]}
```

**i) Genuine Scout restart** — a NEW `session.id` with a strictly greater `generation`
(persisted by Scout across its own process restart, e.g. `3 → 4`); `seq` restarts at this
block's value. Adopted unconditionally, replacing prior membership:
```json
"companions":{"schema":"companion-v2","session":{"id":"boot-9a11f0c2","seq":0,"generation":4},"items":[ ... ]}
```

**j) A delayed snapshot from a retired generation** — after (i) has been accepted, a
buffered/delayed packet from the OLD `generation: 3` run finally arrives, inside an envelope
that is itself newer than anything seen before. Rejected as `retired_session_generation`
regardless of the envelope's own timestamp; the vehicle's companions stay exactly as (i)
left them:
```json
"companions":{"schema":"companion-v2","session":{"id":"boot-3f9a2c1e","seq":14,"generation":3},"items":[ ... ]}
```

## 10. What Scout must implement next

1. Emit `payload.companions` (this schema, including `session`) on its status packet
   whenever a companion is assigned; keep sending `items: []` on **every** packet while
   genuinely unassigned (§6 — never a one-shot event). Pre-companion packets keep working.
2. Generate a fresh `session.id` on every restart of the companion-reporting subsystem;
   **persist `session.generation` locally across restarts and increase it by at least 1 on
   every restart** (never reuse or reset it); strictly increase `session.seq` within a
   session/generation on any content change (§4). If Scout's own persistence for this
   counter is ever lost or corrupted, treat it as a hard failure and pick a value guaranteed
   higher than any it could plausibly have sent before (e.g. a wall-clock-derived value) —
   never silently restart the counter at 0, which would make a genuine restart look like a
   retired, rejected generation.
3. Translate every UAV-side time to Scout's clock and stamp `observed_at` /
   `last_peer_contact_at` with the time of the **observation**, not of the packet. Always
   send the envelope `timestamp` — both freshness (§3) and ordering (§4) depend on it.
4. Always send the complete companion list and complete hazard list (snapshots).
5. Keep `hazard_id` stable and bump `revision` on every change; keep removed hazards out of
   every subsequent snapshot, not just the first one that dropped them.
6. Send `disposition` and `replan` only as Scout's own decisions; send `VERIFIED` only after
   its own read-back/hash verification of the revised mission.
7. Validate geometry before sending (the operator will refuse what §8 lists).
8. Serial radio transport, UAV flight control, perception and obstacle replanning are all
   Scout/UAV work outside this contract (§1).

Fleet-row output of the operator backend (`companions` on `GET /api/fleet/status`) is
documented in `DATA_DICTIONARY.md` (Companions).
