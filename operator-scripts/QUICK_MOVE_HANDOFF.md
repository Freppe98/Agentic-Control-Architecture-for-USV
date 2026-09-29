# QUICK_MOVE_HANDOFF.md — `quick-move-v1`

**Status: PROPOSED. Implemented on the operator station; NOT implemented on Scout.**

Nothing in this document means the Scout can perform a guided movement today. It fixes the
*command and report formats* so that when Scout's Local Mission Agent gains the ability to
drive to a commanded point, the operator station already knows how to issue, order, age,
supersede and cancel it — and, just as importantly, knows what it is **not** allowed to
claim while doing so.

**Written for: the developer implementing the Scout side.**

Operator implementation: `quick_move.py` (backend — read its module docstring first, it is
the authoritative rationale for everything below), `main.py` (`QUICK_MOVE` command type,
`quick_move_dispatch_gate`, `supersede_quick_moves`), `operator/lib/quick-move.js` (view and
gating policy), `operator/pages/Map.js` (context menu, target marker, status card).
Tests: `tests/test_quick_move.py`, `tests/quick-move.test.mjs`.
Fixture: `scripts/quick_move_fixture.py` (opt-in, loopback only).

---

## 0. Read this first — the one thing that is not built anywhere

`QUICK_MOVE` needs ArduRover **GUIDED** (`custom_mode = 15`).
`local_mission_agent/command_executor.py` says, in its own words:

```python
# SET_MODE_GUIDED intentionally absent -- no Flask endpoint exists yet
# for ArduRover GUIDED (custom_mode=15). Falls through to "unsupported".
```

So there is currently **no** Scout path to GUIDED at all, and this document does not claim
otherwise. **Firmware support for GUIDED on this airframe has not been verified** — it could
not be, from operator code. Establishing that (GUIDED reachable, a guided-target message
accepted, the vehicle actually driving to it, and LOITER reachable afterwards) is Scout-side
work and is the real prerequisite for everything below. See §11.

Until a vehicle advertises `capability.supported: true`, the operator's menu item is
**disabled for every real vehicle**, with the reason shown in the menu. Nothing here makes a
live movement command reachable by accident.

---

## 1. Model

- **One operator-authority action.** The operator right-clicks the Map and picks
  **“Move &lt;vehicle name&gt; here”**. Scout drives the vehicle to that latitude/longitude
  and, **after confirming arrival, explicitly enters and verifies LOITER**.
- **Transport is the existing command queue.** No new endpoint, no new transport: it is a
  `POST /api/commands` of type `QUICK_MOVE`, delivered on the same `GET /agent/commands`
  poll, reported on the same `POST /agent/command_result`. It inherits at-least-once
  delivery, the stable command id across retries, and the one result path.
- **Ongoing movement is reported on the existing status envelope**, in a new
  `payload.quick_move` block — the same place and shape convention `payload.companions`
  uses. Again: no new endpoint.
- **It is NOT a mission operation.** The operator station does not validate the target
  against a survey polygon, approved geometry, a shoreline offset or a mission no-go zone;
  it does not require mission readiness, an approved planning package or the mission-start
  workflow; and it never uploads, replaces, clears or restores a mission. A quick move works
  with no mission and no planning geometry at all — that is the point of it.
- **It never arms, never takes authority, never disables a fence or failsafe, and never
  resumes a mission.** Neither side may add any of those as a side effect.
- **Three facts, kept apart everywhere in this document and in the code:**

  | Fact | Answered by | Never implies |
  |---|---|---|
  | **Delivery** — did the command reach Scout? | the command queue lifecycle, and (more strongly) Scout echoing the `command_id` in its status block | movement, arrival, or completion |
  | **Movement** — what is the vehicle doing? | `quick_move.active.state` + `distance_remaining_m` | arrival or a verified hold |
  | **Completion** — did it arrive and is it confirmed holding? | `state: COMPLETED` **with** `arrival_confirmed: true` **and** `loiter_verified: true` | — |

  **Command delivery or acknowledgement must never be presented as arrival.** The operator
  station enforces this structurally: a queue record reaching `EXECUTED` produces no
  completion anywhere (`tests/test_quick_move.py::test_an_executed_command_result_alone_never_produces_a_completion`).

---

## 2. Conventions

| Item | Rule |
|---|---|
| Command type | `QUICK_MOVE`, on the existing queue. |
| Status location | `payload.quick_move` inside the existing status envelope. |
| Schema | `"schema": "quick-move-v1"` — any other value rejects the whole block (`unsupported_schema`). |
| Coordinates | WGS-84 decimal degrees as **objects** `{"lat": 56.7005, "lng": 13.0038}`. Never bare arrays: that removes the `[lat,lng]` vs GeoJSON `[lng,lat]` ambiguity. `lat ∈ [-90,90]`, `lng ∈ [-180,180]`; `(0,0)` is refused as a no-fix sentinel. |
| Timestamps | Unix epoch **seconds** (float), on **Scout's clock** — the same clock as the envelope `timestamp` (`time.time()`). |
| Envelope timestamp | Required. Without it every freshness reading is `UNKNOWN` (never guessed) — see §6. |
| Units | metres, degrees, percent 0–100. |
| Tokens | Uppercase `[A-Z][A-Z0-9_]*`. An unrecognised `state` rejects that status (`bad_state`); it is never promoted to a known one. |
| Numbers | Finite only. NaN / Infinity are refused (and stripped at ingest, station-wide). Booleans are not numbers. |
| Tri-state booleans | `arrival_confirmed`, `loiter_verified`, `nav_ready`: **absent means "Scout did not say"**, which is never read as `false`. Send the field when you know; omit it when you do not. |

---

## 3. The command Scout receives

Exactly what `GET /agent/commands?usv_id=usv-2` returns for a quick move:

```json
{"commands": [{
  "command_id": "6c2f0f42-5a0e-4a0a-9f43-1b1b7a3a9c55",
  "command_type": "QUICK_MOVE",
  "source": "OPERATOR",
  "params": {
    "schema": "quick-move-v1",
    "target": {"lat": 56.70050, "lng": 13.00380},
    "issued_at": 1790000000.0,
    "requested_from": {"lat": 56.69950, "lng": 13.00215, "distance_m": 148.3}
  },
  "expires_at": "2026-09-28T12:00:45+00:00"
}]}
```

- **`target` is the only authoritative field.** It is the operator's destination.
- **`requested_from` is NON-AUTHORITATIVE audit metadata** — the vehicle position the
  operator's own screen was showing at click time. It is a snapshot, not a fact about where
  the vehicle is when Scout reads the command. **Never navigate relative to it.** (Same
  treatment, for the same reason, as `SET_HOME`'s `requested_position`.)
- **The operator deliberately sends no mode, no speed, no arrival radius and no path.**
  Those are Scout's: a station that sent them would be asserting things about a vehicle it
  cannot verify. If Scout needs an arrival radius, Scout chooses it and may report it.
- **`expires_at`** is short — see §7.

### Result (immediate acceptance / rejection)

Report through the normal path (`POST /agent/command_result`, id in the body, or
`POST /api/commands/{id}/result`). The result answers **delivery and acceptance only**:

```json
{"command_id": "6c2f...", "status": "ACCEPTED"}
```
then, once GUIDED is set and the target has been commanded:
```json
{"command_id": "6c2f...", "status": "EXECUTED",
 "result": {"verified": true, "expected_mode": "GUIDED", "observed_mode": "GUIDED"}}
```

- `EXECUTED` here means **“the guided move was accepted and commanded”**. It must **not** be
  withheld until arrival — arrival travels on the status block (§5), and withholding it
  would collide with the short TTL (§7).
- `REJECTED` / `FAILED` with a readable `reason` when Scout cannot take the command. A bare
  `FAILED` with no reason leaves the operator with nothing to act on.
- The operator applies its normal verification rules: an `EXECUTED` carrying
  `result.verified: false` renders as FAILED, never as success
  (`operator/lib/command.js`).

---

## 4. Capability advertisement — absence means unsupported

Sent on the status envelope. **This is what enables the menu item, and nothing else does.**
A vehicle merely being present in the fleet is never treated as evidence it can perform a
guided move (`tests/quick-move.test.mjs`: *“being connected, armed and under OPERATOR control
is not a capability”*).

```jsonc
"quick_move": {
  "schema": "quick-move-v1",
  "capability": {
    "supported": true,              // required; anything but exactly true is unsupported
    "reason": null,                 // required WHEN supported is false: why not (shown verbatim)
    "nav_ready": true,              // optional tri-state; see below
    "max_distance_m": 2000,         // optional; 0 < x <= 100000, else ignored
    "vehicle_class": "SURFACE"      // optional token
  },
  "active": null                    // see §5
}
```

- **Omitting the whole `quick_move` block, or omitting `capability`, means unsupported.**
  A Scout build that predates this contract needs no change: it simply never advertises,
  and the action is never offered for its vehicles.
- **`supported: false` must carry a `reason`.** It is shown to the operator verbatim, in the
  menu, at the moment of the attempt. “Disabled with no explanation” is not an acceptable
  operator-facing state.
- **`nav_ready` is tri-state and blocks ONLY when explicitly `false`.** Omitting it means
  “Scout said nothing”, which the operator does not turn into a readiness failure. Send
  `false` when Scout knows navigation is unavailable (no fix, EKF not ready, …) and can say
  so; the accompanying `reason` should explain it.
- **`max_distance_m`**, when sent, is enforced by the operator *before* dispatch — the
  operator refuses a farther target with both numbers in the message. Scout remains free to
  refuse anyway.
- **`vehicle_class`** is advisory. The operator's structural guarantee is stronger: the menu
  resolves only the selected **fleet** vehicle, and a companion (UAV) has no fleet id and no
  command route, so this command can never be pointed at one. **Do not advertise this
  capability for a UAV**, and do not apply a `QUICK_MOVE` to one if you ever route commands
  by something other than fleet identity.

---

## 5. Movement status

```jsonc
"quick_move": {
  "schema": "quick-move-v1",
  "capability": { ... },              // keep sending it; it is a STATE, not a one-shot event
  "active": {
    "command_id": "6c2f0f42-...",     // required: the operator command this reports on
    "state": "MOVING",                // required token (see the table below)
    "target": {"lat": 56.70050, "lng": 13.00380},   // required: where Scout is actually going
    "updated_at": 1790000012.0,       // required: Scout-clock time of THIS observation
    "distance_remaining_m": 42.5,     // optional; omit when not known
    "arrival_confirmed": false,       // optional tri-state
    "loiter_verified": false,         // optional tri-state
    "mode": "GUIDED",                 // optional: the mode Scout observes
    "reason": "…"                     // required for FAILED/CANCELLED; optional otherwise
  }
}
```

| `state` | Meaning | Terminal |
|---|---|---|
| `STARTING` | Scout has the command and is preparing (e.g. entering GUIDED). | no |
| `MOVING` | Under way toward the target. | no |
| `ARRIVED` | Scout reports it is at the target; **LOITER not yet verified**. | no |
| `COMPLETED` | Arrived **and** Scout verified LOITER. See the rule below. | yes |
| `CANCELLED` | Superseded, cancelled via LOITER, or stood down by Scout. `reason` required. | yes |
| `FAILED` | Could not perform or could not verify. `reason` required. | yes |

### Completion requires arrival AND confirmed LOITER

`COMPLETED` is published by the operator as a **confirmed** completion only when the same
status also carries `arrival_confirmed: true` **and** `loiter_verified: true`.

A bare `COMPLETED` is published as `completion: "CLAIMED_UNVERIFIED"` and rendered as
*“Scout reported the move complete but did not confirm arrival or verified LOITER. Treat the
hold as unconfirmed.”* Scout's claim stays visible — it is evidence — but the station will
not render it as a finished hold. This is the same rule a mission upload read-back follows.

`loiter_verified: true` means Scout performed its **own read-back verification** that the
vehicle is in LOITER (the same standard `MODE_COMMAND_EXPECTED` / `command_normalization.py`
already applies to a mode command: observed `HEARTBEAT.custom_mode == 5`), not that a LOITER
request returned 200.

### `target` is echoed back, and it is checked

The operator compares Scout's `target` against the one it issued and publishes
`target_offset_m` plus `target_mismatch` (threshold 5 m). A mismatch is surfaced
prominently: *“Scout is reporting a target 37 m from the point you clicked.”* It is the one
failure mode that would otherwise look exactly like a healthy move.

### Omission and clearing

| Scout sends | Operator does |
|---|---|
| no `quick_move` key (every pre-quick-move Scout) | nothing — no capability, so the action is never offered. |
| `"quick_move": null` | same as omission. |
| block with `capability` and **no** `active` key | capability-only heartbeat: capability updates, no movement correlation attempted. |
| `"active": null` | **Scout reports nothing running.** This is **not** completion and **not** cancellation: the last applied status stands, with its age, and nothing is inferred to fill the gap. |
| `"active": {...}` repeated with the same `updated_at` | idempotent resend — normal, expected, and the original receipt clock is kept so a repeat cannot manufacture freshness. |

**Status is a STATE, not an event: keep sending the current truth on every packet**, exactly
as you do for `telemetry` or `mission_state`. That includes the terminal one for a short
while, and it includes the capability block.

---

## 6. Freshness — the station's one model

Scout sends **only `updated_at`**. The operator derives three separately-published numbers,
using the **same model and the same code** as `companion-v2` (`COMPANION_CONTRACT.md` §3,
implemented in `companion_telemetry.py` and imported by `quick_move.py` — there is
deliberately no second copy):

```
min_age_s         = (envelope.timestamp − updated_at) + (now_monotonic − receipt_monotonic)
                    a GUARANTEED LOWER BOUND; both terms are single-clock deltas
reporting_delay_s = max(0, receipt_wall − envelope.timestamp)
                    an ESTIMATE that CONFLATES transit delay with Scout↔operator clock offset
estimated_age_s   = min_age_s + reporting_delay_s      (display only)
freshness_quality = "LOWER_BOUND" | "UNKNOWN"          (UNKNOWN = no envelope timestamp)
```

There is **no FRESH verdict** for any individual measurement, here or anywhere in this
station, and Scout should not expect the UI to display one. Only `min_age_s` is ever used to
decide staleness. Local elapsed time runs on `time.monotonic()`, so an operator-side clock
step cannot corrupt it.

Two Scout-side requirements follow:

1. **Always send the envelope `timestamp`.** Both freshness and the outer ordering guard
   depend on it.
2. **Stamp `updated_at` with the time of the OBSERVATION, not of the packet.** A status
   composed now about something observed 8 s ago must say so.

Separately, the operator tracks how long since *any* status for this move arrived
(`status_age_s`, monotonic). Past ~10 s, or on a link that is not CONNECTED, the movement is
rendered **“(last known)”** rather than as current — the station will not assert a vehicle is
still moving on evidence it can no longer refresh.

---

## 7. Expiry — short-lived, never an offline queue

**`QUICK_MOVE_TTL_S = 45`** (vs. `COMMAND_TTL_S = 300` for every other type), and:

- **The operator refuses to create a quick move at all unless the link is CONNECTED.** A
  `PARTITIONED` or `DISCONNECTED` vehicle gets `409 {"code": "link_not_current"}`, and
  `confirm: true` — which buys every *other* command type into the queue-until-contact path
  — **cannot override it**. No command record is created at all.
- The TTL covers **delivery and acceptance**, not the movement. Once Scout reports
  `EXECUTED` the queue record is done and the status block carries everything after.
- An expired quick move is **never delivered** (`expire_commands` runs before every fetch).

This is a safety property, not a tuning choice: *“go to that spot now”* stops being the
operator's intent within seconds of them losing the link, so a quick move must never survive
a disconnection and execute on reconnection.

**Scout side:** if a `QUICK_MOVE` is dequeued after its `expires_at`, **do not execute it.**
Report `REJECTED` with a reason, or simply drop it — the operator has already expired its own
record either way.

---

## 8. Retry identity, supersession and cancellation

### Retries reuse the command id
Delivery is at-least-once: a non-terminal command is redelivered on every poll until a
terminal result arrives. **The `command_id` is stable across every redelivery.** Scout dedups
by `command_id` exactly as it already does for every other command type — a redelivered quick
move must not be executed twice.

### A newer move supersedes an older one
When the operator issues a second quick move for the same vehicle:

1. The station registers the new `command_id` as the **one** move it tracks for that vehicle,
   and **retires** the old id.
2. Any still-non-terminal `QUICK_MOVE` queue record for that vehicle is terminated
   (`EXPIRED`, reason *“replaced by a newer Quick move”*), so an undelivered older move is
   **never handed to Scout afterwards**.
3. A status naming a retired id is refused as `superseded_status`; one naming an id this
   station never issued is refused as `unknown_command_id`. **Neither can put the old target
   back on the map** — pinned by
   `test_a_delayed_status_for_the_superseded_move_never_restores_the_old_target`.

**Scout side:** on receiving a new `QUICK_MOVE` while one is running, abandon the old target
immediately, and report the old one as `CANCELLED` (reason: superseded) if you report it at
all. Never interleave the two.

### Ordering within one command id
- `updated_at` must **strictly increase** for a status to apply. An earlier observation
  arriving in a later envelope is refused (`stale_status`) — the operator orders on the
  observation, not on delivery order.
- A **terminal state is never walked back**: a buffered `MOVING` arriving after a `CANCELLED`
  is refused (`terminal_state_regression`) and cannot resurrect the movement.
- The pre-existing per-vehicle envelope-timestamp guard in `main.py` still applies on top: a
  replayed older *envelope* changes no movement state, because it never reaches this module.

### LOITER cancels a quick move
The Stop action on the operator's card is **the ordinary `SET_MODE_LOITER` command** — the
same one the mode row has always had, through the same pipeline. There is deliberately **no
bespoke cancel endpoint**. When an operator LOITER is created for a vehicle with a tracked
quick move, the station:

1. records the operator's **cancel request** (the card reads *“Stop requested — waiting for
   Scout to confirm the vehicle has stood down”*, never *“stopped”*); and
2. terminates any still-undelivered `QUICK_MOVE` so it cannot fire **after** the hold.

**Only Scout ends the movement.** The station never renders the move as cancelled until Scout
reports a terminal state.

**Scout side:** a `SET_MODE_LOITER` (or `LOITER`) command arriving while a quick move is
running **cancels that quick move** — stop navigating to the target, enter LOITER, and report
`state: "CANCELLED"` with a reason. Do not treat the LOITER as an unrelated mode change that
leaves a guided target still active.

---

## 9. Required Scout behaviour in the awkward cases

| Situation | Scout must |
|---|---|
| **Authority moves to `LOCAL_AGENT`** (operator releases control, or autonomy takes it) | Abandon the quick move and report `CANCELLED` with a reason. The operator's queue is cancelled on a confirmed release (`cancel_pending_commands`) and the card shows the cancel request, but the **vehicle's** behaviour is Scout's to stop. A guided target must not outlive the operator's authority. |
| **RC override** | Same: report `CANCELLED` (reason: RC override). RC has the highest priority and is never overridden by software. |
| **A mission is running** | Coordinate with the mission supervisor. Decide and document whether a quick move suspends the mission or is refused while one is executing — **the operator does neither and asserts neither** (see §11, open question 2). Whatever you choose, the operator never resumes a mission after a quick move, and Scout must not silently resume one either without saying so. |
| **Link loss to the operator** | Keep executing or stand down — **Scout's decision, and it must be documented.** The operator will render the last status as *“(last known)”* and will never infer either outcome. If you stand down, report `CANCELLED` when the link returns so the record is complete. |
| **Arrival** | Confirm it by Scout's own criterion (an arrival radius Scout owns), then set `arrival_confirmed: true`. Do not set it from a commanded-target acknowledgement. |
| **LOITER after arrival** | **Explicitly enter LOITER and verify it by read-back** before setting `loiter_verified: true`. Arriving and staying in GUIDED is not a completed quick move. |
| **GUIDED refused by the flight controller** | `FAILED` with a readable reason. Do not silently fall back to another mode. |
| **The vehicle is disarmed, or navigation is not ready** | `REJECTED` on the command, and advertise `nav_ready: false` with a reason so the operator's menu is disabled *before* the attempt. |

---

## 10. Operator-side integration points (reference)

| What | Where |
|---|---|
| Command type, TTL, dispatch gate, supersession | `main.py`: `COMMAND_TYPES`, `QUICK_MOVE_TTL_S`, `COMMAND_TTL_OVERRIDES`, `canonical_quick_move_params`, `quick_move_dispatch_gate`, `supersede_quick_moves` |
| Status ingest, correlation, publication | `quick_move.py`: `ingest`, `register_command`, `request_cancel`, `fleet_block`, `eligibility` |
| Freshness primitives (shared, not duplicated) | `companion_telemetry.py`: `mark_now`, `receipt_bundle`, `evidence_of`, `evidence_freshness`, `time_since` |
| Issue a move | `POST /api/commands` `{vehicle_id, type: "QUICK_MOVE", params: {target: {lat, lng}}}` → `200 {command}` or `409 {code, message}` |
| Delivery to Scout | `GET /agent/commands?usv_id=usv-2` (claim on first fetch; at-least-once) |
| Result from Scout | `POST /agent/command_result` (id in body) or `POST /api/commands/{id}/result` |
| Ongoing status from Scout | `POST /agent/status` → `payload.quick_move` |
| Published to the UI | `GET /api/fleet/status` → per-vehicle `quick_move` |
| Station-level capability | `GET /api/commands/capabilities` → `commands.QUICK_MOVE` (says explicitly that the real answer is per-vehicle) |
| UI policy and view model | `operator/lib/quick-move.js` |
| Map surface | `operator/pages/Map.js`: `openQuickMenu`, `sendQuickMove`, `updateQuickMoveMarker`, `renderQuickMove` |

### Refusal codes
`POST /api/commands` returns `409` (or `400` for a malformed target) with a machine `code`
from this ordered list, mirrored verbatim in `operator/lib/quick-move.js` and pinned by a
parity test:

`not_reported` · `not_supported` · `link_not_current` · `authority_unreadable` ·
`not_operator` · `disarmed` · `armed_unknown` · `nav_not_ready` · `bad_target` · `too_far` ·
`busy`

The backend **re-runs the whole gate at dispatch over a LIVE authority read**, so a menu item
the UI enabled can still be refused. That is the correct order of authority, not a defect: a
UI check is a courtesy to the operator, never an authorization.

### Rejection counters
Every refused status is counted per reason on the fleet row
(`quick_move.rejections.by_reason`): `unsupported_schema`, `bad_capability`,
`status_not_object`, `missing_command_id`, `bad_state`, `bad_updated_at:*`, `bad_target`,
`superseded_status`, `unknown_command_id`, `stale_status`, `terminal_state_regression`,
`cleared_before_status`.

---

## 11. What was tested, and what still depends on Scout

### Verified on the operator station
`tests/test_quick_move.py` (45 tests) and `tests/quick-move.test.mjs` (42 tests), both green,
plus the full existing suites (1124 frontend, backend suite) with no regressions. Covered:

- a quick move issued and completed with **no mission and no planning geometry**, and a
  stored mission **unchanged** across the whole lifecycle;
- capability absent → refused; `supported:false` → refused with Scout's own reason; a
  connected, armed, OPERATOR-controlled vehicle that has not advertised → still refused;
- authority not OPERATOR → refused; authority **unreadable** → refused with its own distinct
  reason; disarmed vs. unknown-armed → two different refusals; `nav_ready:false` → refused,
  `nav_ready` absent → **not** refused;
- invalid coordinates (out of range, `(0,0)`, non-finite, missing) and over-range targets;
- expiry: own short TTL, expired moves never delivered, `confirm:true` cannot queue one while
  disconnected;
- repeated clicks, **retry identity** (same `command_id` across redeliveries), supersession
  (old record terminated, old target never restored), stale and unknown `command_id`;
- accepted vs. moving vs. confirmed completion; `EXECUTED` alone produces **no** completion;
  bare `COMPLETED` renders as an unverified claim;
- out-of-order `updated_at`, terminal-state regression, replayed older envelope;
- cancellation through LOITER (request recorded, undelivered move terminated, only Scout ends
  the movement) and authority-release cancellation;
- vehicle switching leaks neither target nor status; two vehicles never share state;
- stale/disconnected status renders as **“last known”**, never as current.

Browser verification (context menu and status card at desktop and laptop sizes) is recorded
in `operator/docs/verification/quick-move.md`. Scout responses in every test and screenshot
are produced by `scripts/quick_move_fixture.py` and are **clearly labelled fixtures** — every
vehicle it speaks for is named “Scout (FIXTURE)”. **No live movement command was sent to any
vehicle, and no production service was restarted.**

### Depends entirely on Scout
1. **GUIDED itself** — no Flask endpoint, no Local Agent mapping, and **firmware support
   unverified** (§0). This is the gating item.
2. Driving to a commanded target and reporting honest `distance_remaining_m`.
3. Arrival determination (Scout owns the criterion and the radius).
4. **Entering and read-back-verifying LOITER after arrival**, and only then setting
   `loiter_verified: true`.
5. Advertising `capability`, and keeping the status block flowing on every packet.
6. Honouring supersession, LOITER cancellation, authority loss and RC override as §8–§9
   describe.

### Unresolved assumptions — stated, not decided
1. **`QUICK_MOVE_TTL_S = 45` is an operator-side guess** at “long enough to deliver and
   confirm a mode change, short enough that the intent is still current”. If Scout's
   GUIDED-entry path is routinely slower, say so and the number moves — it is one constant.
2. **Mission interaction is deliberately undecided here.** Whether a quick move suspends a
   running mission, is refused while one executes, or is simply layered over it is Scout's
   call with its mission supervisor. The operator asserts nothing either way and never
   resumes a mission afterward. Document the choice and mirror it in `capability.reason`
   when it means the action is unavailable.
3. **Link-loss behaviour is Scout's call** (§9) and is not specified here.
4. **No `arrival_radius_m` is sent or read today.** If Scout reports one it is currently
   ignored by the operator; adding it would be an additive, optional field.
5. **Nothing here has run against real hardware.** Every operator-side guarantee above is a
   guarantee about the *operator station's* behaviour given a conforming Scout.
