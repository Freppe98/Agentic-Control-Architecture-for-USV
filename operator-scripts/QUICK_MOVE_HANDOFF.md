# QUICK_MOVE_HANDOFF.md — `quick-move-v1` (joint integration contract)

**Status: implemented on BOTH sides. Not verified against real hardware, SITL or a live
mavlink2rest on either side. `QUICK_MOVE_ENABLED = False` on Scout; the operator's menu item
stays disabled for every vehicle that has not advertised support.**

This is the **joint** contract. It supersedes the operator-only version and incorporates
Scout's own `QUICK_MOVE_HANDOFF.md` at checkpoint `a102f21a`, which implements the Scout side
(GUIDED entry, `MAV_CMD_DO_REPOSITION` over `COMMAND_INT`, arrival/LOITER monitoring, capability
and status reporting). Where the two repositories' requirements differ, **both are preserved
here and the difference is named** — see §13.

Nothing in this document means either side has moved a vehicle. Scout's own verification is a
source read plus fixture runs against synthetic telemetry; the operator's is unit tests, capture
replay and browser checks. The bench steps that would close that gap are in §14.

**Written for: developers on both sides of this integration.**

**Operator implementation**: `quick_move.py` (backend — read its module docstring first, it is
the authoritative rationale for everything below), `main.py` (`QUICK_MOVE` command type,
`quick_move_dispatch_gate`, `quick_move_delivery_block`, `supersede_quick_moves`,
`invalidate_quick_moves_on_link_loss`), `operator/lib/quick-move.js` (view and gating policy),
`operator/pages/Map.js` (context menu, target marker, status card).
Tests: `tests/test_quick_move.py` (46), `tests/test_quick_move_delivery.py` (21),
`tests/test_scout_quick_move_compat.py` (20), `tests/quick-move.test.mjs` (50).
Fixture: `scripts/quick_move_fixture.py` (opt-in, loopback only).
Capture replay: `scripts/scout_payload_replay.py`, payloads in
`tests/fixtures/scout_quick_move_payloads/`.

**Scout implementation** (checkpoint `a102f21a`): `flask/services/guided_navigation.py`,
`flask/routes/agent_routes.py` (`POST /agent/guided_target`), `command_executor.py`,
`command_normalization.py`, `command_handler.py`, `quick_move_controller.py`,
`quick_move_state.py` / `quick_move_runtime.py`, `local_agent.py`.
Fixture: `quick_move_fixture.py` (8 scenarios, synthetic telemetry, opt-in).

---

## 0. Read this first — what is and is not established

**Built on both sides.** Scout now has a GUIDED path: `guided_navigation.py` enters GUIDED via
the existing `set_mode_and_verify`, then sends `MAV_CMD_DO_REPOSITION` as a `COMMAND_INT`. The
earlier operator-side statement that "no Scout path to GUIDED exists" is **out of date** and is
withdrawn.

**Not established on either side:** that any of it moves a vehicle.

| Claim | Evidence | Strength |
|---|---|---|
| `DO_REPOSITION` needs `COMMAND_INT`, not `COMMAND_LONG` | Scout read `GCS_MAVLINK_Rover::handle_command_int_do_reposition` in ArduPilot master | source read — strong |
| `z=NaN` → `MAV_RESULT_DENIED`; `z=0` resolves it | ArduPilot developer reply, Rover 4.6.3 forum thread | third-party report — good |
| The `COMMAND_INT` JSON shape works on **this** mavlink2rest | `mission_upload_service.py` round-trips a sibling `MISSION_ITEM_INT` in the same shape | analogy — **not** proof for `COMMAND_INT` |
| This boat runs ArduRover **4.5.0**, not master | `BENCH_TEST_MISSION.md` | behavioural difference unlikely, **unconfirmed** |
| Arrival radius / stability window are right for this boat | `QUICK_MOVE_ARRIVAL_RADIUS_M = 3.0`, `_STABLE_WINDOW_S = 3.0` | **a starting guess**, not bench-tuned |
| The operator parses what Scout actually emits | a **reconstruction** of Scout's scenarios, not a capture (§12) | **weaker than it looks** |

Until a vehicle advertises `capability.supported: true`, the operator's menu item is
**disabled for every real vehicle**, with the reason shown in the menu. Scout ships with
`QUICK_MOVE_ENABLED = False`. Nothing here makes a live movement command reachable by accident.

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

### `mode` must agree with `loiter_verified` — and the operator checks that it does

Scout hit and fixed a real bug here: `_handle_arrival()` / `_emergency_loiter()` checked the
LOITER call's `verified` but **discarded that same response's `observed_mode`**, so `mode` kept
reporting the last value sampled during the arrival poll loop — `GUIDED` — alongside
`loiter_verified: true`. The fix sets `record.observed_mode` from the LOITER call's own fresh
read-back, and a coherent `COMPLETED` now reports `mode: "LOITER"`.

**The operator does not take that on trust.** `quick_move.py` publishes `mode_incoherent: true`
when `loiter_verified: true` arrives alongside any other *reported* mode, and
`_completion()` refuses to call such a report CONFIRMED — it becomes `CLAIMED_UNVERIFIED` with
the contradiction named in the operator-facing text. An **absent** `mode` is not a
contradiction and is not treated as one.

This is not distrust of the fix; it is that a silent regression here produces the single most
dangerous screen this feature can show — a confident "Arrived and holding" over a vehicle that
is still in GUIDED and may not be holding at all.

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

## 7. Delivery — short-lived, never an offline queue, never across a link gap

### The defect this section was rewritten for

The earlier rule — *refuse creation unless CONNECTED, plus a 45 s TTL* — **was not sufficient,
and the gap was reproduced, not theorised.** The creation check proves the link was current at
t=0 and says nothing about t=20. A quick move created on a healthy link survived a complete
`CONNECTED → PARTITIONED → DISCONNECTED → CONNECTED` cycle and was then handed to Scout and
executed, up to 45 s after the operator pressed — pointing a vehicle at a spot chosen from a
picture that had since gone away and come back, with the operator given no sign it had fired.

**Delivery is now re-decided at the moment of delivery**, by three independent bounds. Each one
alone has a hole, which is why there are three:

| Bound | Constant | What it closes |
|---|---|---|
| **Link continuity** | `params.link_epoch` | ANY comm-state transition since creation refuses delivery. The only one that catches the sub-second race where Scout's poll lands between the outage and the 1 Hz comms monitor noticing it. |
| **Invalidation on link loss** | — | The move is terminated the moment the link leaves CONNECTED, rather than left for the TTL (which would still allow delivery once the link returned). |
| **Claim window** | `QUICK_MOVE_CLAIM_WINDOW_S = 15` | An unclaimed move older than this is stale intent even on a link that never wobbled. Applies only before the first claim — a REdelivery of an already-claimed command is the at-least-once mechanism working. |
| **TTL** | `QUICK_MOVE_TTL_S = 45` | Once claimed, how long Scout has to report a result. |

Also unchanged and still enforced:

- **The operator refuses to create a quick move at all unless the link is CONNECTED.** A
  `PARTITIONED` or `DISCONNECTED` vehicle gets `409 {"code": "link_not_current"}`, and
  `confirm: true` — which buys every *other* command type into the queue-until-contact path —
  **cannot override it**. No command record is created at all.
- The TTL covers **delivery and acceptance**, not the movement. Once Scout reports `EXECUTED`
  the queue record is done and the status block carries everything after.
- Both delivery paths (`GET /agent/commands` and `GET /api/commands/pending/{id}`) apply the
  same rule, so the debug view is not a second way in.
- **`SET_MODE_LOITER` is deliberately NOT subject to any of this.** The safety hold stays
  queue-until-contact: it must reach the vehicle even across an outage, which is exactly the
  opposite of what a quick move needs. Pinned by
  `test_a_loiter_is_still_delivered_after_a_link_blip`.

A refused delivery is EXPIRED with the real reason on the record, and the operator's card shows
a cancel **request** — never "cancelled", because if the command had already reached Scout only
Scout can say whether the vehicle stood down.

### Required Scout behaviour — the part the operator CANNOT close alone

Everything above is operator-side and needs no Scout change. But there is a residual the
operator provably cannot cover, and it needs two precise things from Scout:

1. **Honour `expires_at` at execution time, not just at receipt.** If a `QUICK_MOVE` is
   dequeued, or reaches the point of execution, after its `expires_at`, **do not execute it** —
   report `REJECTED` with a reason, or drop it. The operator has already expired its own record
   either way.
2. **Do not execute a quick move you claimed before a link outage.** The operator can
   invalidate a move it has not yet handed over; once Scout has claimed it, **only Scout can
   decide not to act on it.** If the Scout→operator link was interrupted between claiming a
   `QUICK_MOVE` and acting on it, treat the command as stale: do not start the move, and report
   `CANCELLED` or `REJECTED` with that reason. Scout already knows its own link state
   (`communication.py`), so this is a local check, not new protocol.

Without (2) there remains a window — claimed, link drops, link returns, Scout executes — that
no operator-side guard can reach, because by then the command is in Scout's hands. The operator
will show it as "Delivery not confirmed" rather than claiming either outcome, which is honest
but is not the same as the move being prevented.

**Nothing else about §7 requires a Scout change.**

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
| **A mission is running** | **Decided — see §11.** Scout REJECTS the quick move when its mission controller is actively driving a bound mission; otherwise entering GUIDED is itself the suspension, and there is no automatic resume on either side. |
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

## 11. Mission interaction — what Scout actually does

**Decided on the Scout side, and it is a RESTRICTION, not a suspension feature.** The earlier
operator-only draft left this open; it is now settled, and the operator must not overstate it.

`mission_execution_controller.pause()` / `resume()` require **LOCAL_AGENT** authority
(`autonomy_gate`), while a quick move only ever executes under **OPERATOR** authority — so
Scout's controller can never legally call them for a quick move. That produces two cases:

| Scout's state | What happens |
|---|---|
| **No mission bound to the controller** — no mission at all, or one running via a raw `SET_MODE_AUTO` the controller never bound | The quick move proceeds. Entering GUIDED **is** the suspension: the vehicle leaves AUTO, and mission progress (`mission_current`) lives on the Pixhawk rather than in Python state, so nothing is lost. |
| **A mission is actively bound and being driven** (`RUNNING`, `RETURNING_HOME`, any in-flight transitional state) | Scout's `prepare()` **REJECTS** the quick move with a precise reason, rather than writing GUIDED underneath a controller that would go on believing it is supervising a mission that is no longer running. |
| **`PAUSED`** | Not rejected — the vehicle is already safely held in LOITER. |

### What this explicitly does NOT include

- **No automatic mission suspension.** Nothing on either side pauses a mission to make room
  for a quick move. In the "no mission bound" case the mission simply stops being executed
  because the vehicle left AUTO.
- **No automatic resume.** Neither side resumes a mission after a quick move, ever. Continuing
  is an ordinary explicit operator action (`SET_MODE_AUTO` / the Agent Mission card), exactly as
  it has always been.
- **No operator-side knowledge of the restriction.** This station cannot tell whether Scout's
  controller currently has a mission bound, so it **cannot** pre-emptively disable the menu item
  for this reason. The refusal arrives as a command `REJECTED` with Scout's sentence, which the
  operator shows verbatim (§13, finding 2). If Scout can surface the restriction earlier, the
  natural place is `capability.supported: false` with a reason while a bound mission is running
  — that would disable the action *before* the operator attempts it. **Not implemented; optional.**

Planned-geometry / survey-polygon / no-go-zone validation is performed **nowhere on either
side**, by design and unchanged.

---

## 12. Capture replay — what it proves, and what is actually in the repo

### The distinction that matters

| | What it establishes |
|---|---|
| **Reporting compatibility** (this section) | The operator parses, orders, correlates and renders what Scout's publisher emits. |
| **Command delivery** (§7, §14) | An operator-generated command actually reaches a vehicle and moves it. |

**The replays below establish the first and say nothing about the second.** No capture replay
is evidence that any vehicle moved, or that a command was ever delivered to one.

### Evidence quality — read this before quoting the results

Scout's handoff says its exported captures live in `scout_quick_move_captures/` in the Scout
repository, **gitignored**, and asks for them to be handed over. **They have not been.** What is
in `tests/fixtures/scout_quick_move_payloads/` today is
`RECONSTRUCTED-from-scout-handoff.json` — Scout's eight scenarios **transcribed from the prose
of its handoff**, not captured from its code.

A reconstruction proves the operator handles the contract **as documented**. It cannot prove
the operator handles what Scout's publisher **actually emits**: exact field spellings, which
optional fields are really omitted, how `updated_at` is really stamped, what a capability-only
heartbeat really looks like on the wire. Those are exactly the things prose smooths over, and
exactly what has bitten this integration before — `battery_remaining=-1`, and Scout's own
mode/completion coherence bug, neither of which any written description would have revealed.

`tests/test_scout_quick_move_compat.py::test_the_evidence_quality_is_stated_honestly` fails
loudly if the reconstruction is ever allowed to pass itself off as a capture. **Dropping the
real captures into that directory requires no other change** — the replay tool and the test both
pick up whatever `*.json` is there, and the reconstruction can then be deleted.

### Correlated command ids

A quick move is correlated: `active.command_id` names an operator command, and the operator
refuses a status for an id it never issued (`unknown_command_id`) so a delayed or foreign report
can never put a target on the map. Scout's captures carry ids from **Scout's** run, so a bare
replay proves only that the block parses — every status is correctly refused (pinned by
`test_a_status_for_an_uncorrelated_command_is_refused_outright`).

`--correlate-commands` mints a **real** operator command per captured id and rewrites that id in
memory. **The capture files on disk are never modified** — a capture is evidence, and evidence
the harness edits in place is not evidence any more. Every rewrite is printed.

```
python scripts/scout_payload_replay.py tests/fixtures/scout_quick_move_payloads/ --correlate-commands
python -m unittest tests.test_scout_quick_move_compat -v
```

### Scenario isolation — a harness requirement the first replay run exposed

Scout exports its scenarios **independently**, each starting at its own `t0`. Replayed into
shared vehicle state, every scenario after the first was silently refused by `main.py`'s
per-vehicle **monotonic envelope-timestamp guard** (`stale_timestamp`): the fleet row kept
showing the previous scenario's result and the run *looked* like a pass.

That is the guard doing its job — a real Scout never emits two scenarios on one vehicle with
overlapping clocks — so the fix belongs in the harness. `scout_payload_replay.py` now resets the
per-vehicle high-water mark and blocks between scenario groups, prints a loud
`!! REFUSED AS STALE` line for any envelope that changed nothing, and reports the resulting
state **per scenario** rather than only once at the end. Command records are deliberately *not*
reset between scenarios, so a cross-scenario correlation leak would still be caught.

**Scout side:** when you hand over the captures, one file per scenario is ideal (it is also what
your `--export-dir` already produces).

### Results — all eight scenarios, replayed through the real ingestion endpoint

| # | Scenario | Resulting fleet state | Verifies |
|---|---|---|---|
| 1 | `capability_only_heartbeat` | `supported=true`, `current=none`, 0 rejections | capability without a movement |
| 2 | `disabled_carries_reason` | `supported=false` + Scout's sentence; a `QUICK_MOVE` then refused `409 not_supported` with that sentence | capability absent/disabled keeps the action unavailable |
| 3 | `full_success` | `STARTING → MOVING → MOVING → COMPLETED`, `completion=CONFIRMED`, `arrival_confirmed`+`loiter_verified` true, `mode=LOITER`, `mode_incoherent=false` | starting/moving distinct from completion; completion shows arrival **and** verified LOITER consistently |
| 4 | `arrival_loiter_failure` | `FAILED`, `completion=None`, `arrival_confirmed=true`, `loiter_verified=false`, `mode=GUIDED`, reason preserved | arrival + failed LOITER is never shown as success |
| 5 | `cancelled_by_loiter_command` | `CANCELLED`; the delayed older `MOVING` refused as `terminal_state_regression` | cancellation cannot be overwritten by delayed older status |
| 6 | `authority_transfer_mid_move` | `CANCELLED` + authority reason; delayed `MOVING` refused the same way | authority transfer cannot be overwritten |
| 7 | `rejected_not_armed` | capability-only, `current=none`, 0 rejections — and the operator refuses `409 disarmed` itself (§13, finding 1) | rejection shows the actual reason |
| 8 | `failed_dispatch_target_not_accepted` | `FAILED`, `MAV_RESULT_DENIED` reason preserved, `mode=LOITER` (the safety hold that did take) | failed dispatch shows the actual reason |

Plus, from the same suite: two vehicles never share a target or an outcome
(`test_two_vehicles_never_share_a_target_or_an_outcome`), the `-fixture` vehicle is structurally
isolated from the real Scout (§13, finding 5), every published movement carries its own age, and
a replayed capture is aged honestly rather than presented as live.

---

## 13. Joint findings — where the two implementations met

Each of these came out of comparing Scout's handoff against the operator implementation, or out
of replaying the scenarios. Findings 1–5 are **fixed or resolved**; 6–7 are **open**.

### 1. Scout's `rejected_not_armed` is unreachable from this UI — and both checks must stay
The operator's own gate reads the same `telemetry.armed` and refuses `409 disarmed` **before a
command is created**, so Scout's `prepare()` never sees that case from the Map. That is defence
in depth working, not dead code on either side: armed state can change between the operator's
gate and Scout's `prepare()`, and a caller other than this UI can reach the queue. The
consequence worth knowing is that **the operator-facing sentence for a disarmed vehicle is the
operator's, not Scout's.**
*Pinned by `test_scouts_not_armed_rejection_is_unreachable_because_the_operator_refuses_first`.*

### 2. A Scout-only rejection used to render as "Sent — awaiting Scout" — **fixed**
Scout rejects at `prepare()` **without registering a movement**, so the status block stays
capability-only and the reason travels on the command result. The operator's state line was
driven solely by the status block, so a rejected move read "Sent — awaiting Scout" beside its own
rejection reason. `commandOnlyState()` now derives the state line from the command's terminal
outcome when Scout reported no movement: **Rejected by Scout** / **Failed** / **Not delivered** /
**Accepted — awaiting movement**. This is the path the **mission-supervisor restriction** (§11)
arrives on.
*Pinned by `tests/quick-move.test.mjs` "a Scout rejection is never rendered as 'awaiting Scout'".*

### 3. "Arrived but not holding" was collapsed into "Failed" — **fixed**
Scout's `arrival_loiter_failure` reports `FAILED` with `arrival_confirmed: true`. The operator
showed "Failed" and the reason, which reads as *nothing happened* — the opposite of the truth.
The vehicle **is** at the operator's chosen point and is **not** confirmed holding, so it may be
drifting there. The card now carries that as its own warning.
*Pinned by "arrival with a failed hold is surfaced as its own fact, not just 'Failed'".*

### 4. The operator trusted `mode` after a verified LOITER — **fixed**
Scout fixed its own mode/completion coherence bug this round. The operator now **checks** it:
`mode_incoherent` is published, and a `COMPLETED` whose `loiter_verified: true` arrives alongside
another reported mode is refused as CONFIRMED and shown with the contradiction named (§5). An
absent `mode` is not a contradiction.
*Pinned by `test_a_verified_hold_reported_in_another_mode_is_refused_as_confirmed`.*

### 5. The `-fixture` vehicle id is structurally safe — **no change needed**
Scout suffixes its fixture's `usv_id` with `-fixture`. The operator's registry resolves that to
**its own discovered vehicle** (`usv-2-fixture`), not to Scout (id 2). Simulated quick-move state
therefore cannot land on the real Scout's row, and the simulated display name travels with it, so
a screen showing fixture data cannot be mistaken for a live vehicle. No registry entry and no
test-only identity hack is required.
*Pinned by `test_the_fixture_vehicle_is_isolated_from_the_real_scout`.*

### 6. Quick move has no session/generation — **open, low priority**
Companions carry `session {id, seq, generation}` so the operator can tell a publisher restart
from a quiet link. Quick move has no equivalent, and Scout's state is explicitly **in-memory only
— a restart never resumes a move**. So after a Scout restart mid-move, `active` simply
disappears, and the operator cannot distinguish "Scout restarted and dropped the move" from
"Scout went quiet". It currently renders the last state as **(last known)** with a growing age,
which is honest but less specific than it could be. A terminal `CANCELLED` ("publisher restarted")
on Scout's first packet after a restart would close this; the in-memory design makes that hard,
which is why it is low priority rather than required.

### 7. Capability fields Scout does not send — **open, benign**
`nav_ready`, `max_distance_m` and `vehicle_class` are optional and Scout's handoff does not say
it emits any of them. The operator treats all three as "not stated" and blocks on none of them —
the reconstruction omits them for that reason. If Scout can supply `nav_ready: false` when it
knows navigation is unavailable (no fix, EKF not ready, bound mission running — see §11), the
operator will disable the menu item **before** the attempt with that reason, instead of the
operator finding out via a rejection.

---

## 14. Remaining bench steps before `QUICK_MOVE_ENABLED = True`

Scout's list, in Scout's order (cheapest/lowest-risk first), with the operator-side additions.
**No step below has been performed.** SITL is unavailable in both development environments.

| # | Step | Owner |
|---|---|---|
| 1 | **mavlink2rest `COMMAND_INT` acceptance** — POST a captured `guided_navigation.py` payload to a live mavlink2rest and confirm 200 plus correct MAVLink on the wire (`mavproxy` / Wireshark). No `COMMAND_INT` has ever been sent by this codebase; the JSON shape is proven only for a *sibling* message type. | Scout |
| 2 | **GUIDED entry** on the real boat — `set_mode_and_verify` for `custom_mode=15` specifically. | Scout |
| 3 | **`DO_REPOSITION` acceptance** — in GUIDED on the bench (props/motors disconnected), confirm `MAV_RESULT_ACCEPTED` and that `set_desired_location()` actually took. | Scout |
| 4 | **Arrival radius / stability tuning** — `QUICK_MOVE_ARRIVAL_RADIUS_M` / `_STABLE_WINDOW_S` against this boat's real GPS noise. Currently a guess. | Scout |
| 5 | **LOITER re-entry after arrival** — a LOITER immediately following a GUIDED reposition (not from AUTO/MANUAL) verifies the same way. | Scout |
| 6 | **ArduRover 4.5.0 vs master** — confirm this boat's firmware behaves as the master-branch source read and the 4.6.3 forum report describe. | Scout |
| 7 | **Hand over the real captures** — `scout_quick_move_captures/` → `tests/fixtures/scout_quick_move_payloads/`, so §12 runs on evidence instead of a reconstruction. | Scout |
| 8 | **Delivery across a real link gap** — with a real Scout polling, create a quick move, drop the link, restore it, and confirm nothing is delivered and the operator shows the invalidation. Reproduced and fixed in-process (§7); never exercised over a real radio. | Both |
| 8b | **Scout-side staleness checks** (§7, "the part the operator cannot close alone") — honour `expires_at` at execution time, and refuse a quick move claimed before a link outage. **Not implemented on Scout today**; without it a claimed-then-interrupted move can still execute. | Scout |
| 9 | **Full end-to-end** — one real (or SITL) quick move from click to `COMPLETED`, with `payload.quick_move` observed live on the operator station. | Both |

Steps 2–6 and 8–9 can all be run against SITL first, if one becomes available.

---

## 15. What was tested on the operator side

`tests/test_quick_move.py` (46), `tests/test_quick_move_delivery.py` (21),
`tests/test_scout_quick_move_compat.py` (20) and `tests/quick-move.test.mjs` (50) — all green,
alongside the full existing suites with no regressions. Covered, beyond §12's replay results:

- **Delivery (§7)**: the reported defect reproduced and fixed; invalidation at the moment the
  link degrades rather than at the TTL; a `PARTITIONED` blip counted as an interruption; the
  epoch catching a poll the 1 Hz monitor has not seen; an uninterrupted link still delivering
  normally; the claim window; redelivery keeping a stable id and its original claim time;
  redelivery stopping after a result and after a link blip; supersession across polls with a
  distinct reason; Stop before and after delivery; LOITER itself always delivered, including
  across a blip; the debug pending view applying the same rule.
- **Independence from missions**: a quick move issued and completed with no mission and no
  planning geometry, and the mission store byte-identical across the whole lifecycle.
- **Capability**: absent → refused; `supported:false` → refused with Scout's reason; a live,
  connected, armed, OPERATOR-controlled vehicle that never advertised → still refused; a
  never-seen vehicle → a plain 404, which is a different thing and is pinned as such.
- **Authority**: not OPERATOR → refused; unreadable → its own distinct reason; the gate re-run
  server-side over a live read.
- **Honesty**: delivery/movement/completion never collapsed; `EXECUTED` alone produces no
  completion; a bare or self-contradictory `COMPLETED` renders as an unverified claim; stale or
  disconnected status renders as "last known"; an ambiguous send says so.

Browser verification (desktop 1920×1080 and laptop 1366×768) is recorded in
`operator/docs/verification/quick-move.md`.

**No live movement command was sent to any vehicle, no production service was started, stopped
or restarted, and every test and replay ran against an isolated `OPERATOR_RUNTIME_DIR`.**
