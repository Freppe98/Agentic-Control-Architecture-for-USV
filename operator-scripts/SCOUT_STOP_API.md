# `POST /agent/mission_execution/stop` — Scout's Stop Mission lifecycle operation

**Status: SHIPPED ON SCOUT.** The Operator Station's model, proxy, transaction and UI are written
against the contract below and are wired to it. This document records what the Operator expects
of Scout and — just as importantly — what the Operator deliberately does **not** do, so that no
future change quietly reintroduces a second mission-execution lifecycle on this side.

---

## What Stop is

A **safe abort** of the mission run. It is **not** the legacy raw Pixhawk stop (`/nav/stop`, which
this station does not expose anywhere), and it is **not** a destructive mission deletion.

Scout performs the whole transaction itself:

```
active mission
  -> verified LOITER
  -> verify active mission identity
  -> restore the immutable ORIGINAL mission if a verified revised route is installed
  -> rewind the original mission to its start
  -> verify the rewind
  -> reset mission-execution / replan / test state
  -> clear the simulated experiment injection
  -> invalidate the prior runtime Home
  -> return supervisory authority to OPERATOR
  -> re-prove mission evidence
```

## Why the Operator does not emulate any of it

**Pause is not Stop.** Pause holds position with the mission still loaded and the execution
position recorded, so Resume continues the *same* run from where it stopped. Stop **ends** the
run and returns the vehicle to a clean, restartable state.

The Operator Station could technically issue `SET_MODE_LOITER`, re-upload the original mission,
reset the sequence and write `OPERATOR` authority itself. It deliberately does not, for two
reasons:

1. That is a **second mission-execution lifecycle** competing with Scout's. Scout would still
   believe the mission is RUNNING or PAUSED and could resume commanding the vehicle. Every other
   lifecycle fact in this station is Scout's own word, and Stop is not the exception.
2. A locally-assembled "stopped" gives no basis for the authority decision. Returning OPERATOR
   authority is only safe once the vehicle is verifiably holding and the reset is verifiably
   complete, and only Scout can prove that as part of its own transaction.

---

## Contract

### Request

```
POST /agent/mission_execution/stop
Content-Type: application/json

{ "mission_id": "msn-..." }        // optional; when present Scout fails closed on a mismatch
```

The Operator always sends the vehicle's **active persisted mission id** when it has one, so Scout
can refuse rather than aborting a run the operator did not mean. An empty body `{}` is valid.

### Phases

Scout publishes each step through the `state` field of `GET /agent/mission_execution/status`. The
Operator renders each one as its own progress line and predicts nothing:

| state | operator line |
|---|---|
| `STOP_REQUESTED` | Stopping mission… |
| `STOP_HOLD_REQUESTED` | Holding position… |
| `STOP_HOLD_CONFIRMED` | Position hold verified |
| `STOP_VERIFYING_MISSION` | Verifying active mission… |
| `STOP_RESTORING_ORIGINAL` | Restoring original mission… |
| `STOP_REWINDING` | Rewinding mission… |
| `STOP_VERIFYING_REWIND` | Verifying rewind… |
| `STOP_RESETTING` | Clearing execution and replan state… |
| `STOP_VERIFYING_RESET` | Verifying reset… |

A Scout that publishes no intermediate state is handled too: the Operator derives the phase from
the `stop` evidence block below (hold verified → restoring, original restored → rewinding, rewind
verified → verifying reset).

### The resting state after a SUCCESSFUL Stop

```
state                   = NOT_READY
start_eligible          = true
authority_blocks_start  = true
authority               = OPERATOR
```

**This is expected and is never displayed as a mission failure.** Authority is deliberately back
with the operator; the Operator's own Start transaction is what hands it to `LOCAL_AGENT` again,
which is why the Start button stays available in exactly this condition.

`STOPPED` / `CANCELLED` are still accepted as resting states, so a Scout that settles there is not
displayed as an unrecognized state.

### Status evidence

`GET /agent/mission_execution/status` carries:

```json
{
  "stop": {
    "hold_verified": true,
    "original_restored": true,
    "active_hash_before": "sha256:...",
    "original_hash": "sha256:...",
    "revised_hash": "sha256:...",
    "rewind_verified": true,
    "sequence_after": 0,
    "replan_reset": true,
    "experiment_cleared": true,
    "authority_after": "OPERATOR",
    "ready_for_start": true,
    "outcome": "STOPPED"
  }
}
```

Booleans are read **tri-state**: `true` / `false` / absent are three different facts. "Scout could
not verify the rewind" and "Scout said nothing about the rewind" are not the same claim, and the
Operator never rounds the second into the first.

`can_stop` remains meaningful: `true` enables the control, `false` shows it disabled with Scout's
own answer, and an absent key is silence — the lifecycle **state** is then the authority, exactly
as for `can_pause` / `can_resume`.

### Response

Same envelope as `start` / `pause` / `resume`, optionally carrying the same `stop` block:

```json
{
  "accepted": true,
  "operation": "stop",
  "operation_id": "op-...",
  "previous_state": "RUNNING",
  "current_state": "NOT_READY",
  "verified_mode": "LOITER",
  "mission_id": "msn-...",
  "sequence": { "current": 0, "count": 22 },
  "stop": { "...": "..." },
  "final": true,
  "idempotent": false,
  "error": null
}
```

HTTP 200 with a body-level `error` (or `accepted:false`) is a **vehicle-level failure**, exactly as
for the other operations — the Operator reads the body, not the status line. HTTP 409 is a definite
refusal (precondition, lifecycle state, replanning ownership, write arbitration).

### Failure after the safe hold

Scout reaches a verified LOITER **before** it restores, rewinds or resets anything, so a Stop that
fails past that point leaves the vehicle **held** and the reset **incomplete**. Scout reports
`state = SUSPENDED` with one of:

```
STOP_ACTIVE_MISSION_UNKNOWN
STOP_RESTORE_UPLOAD_FAILED
STOP_RESTORE_HASH_MISMATCH
STOP_REWIND_NOT_VERIFIED
```

The Operator shows the exact code, states that the vehicle is being held in LOITER and that the
reset is incomplete, and **performs no automatic recovery** — no Rearm, no Resume, no AUTO, no
second Stop. The next action is the operator's explicit decision.

### Idempotency

A Stop issued against an already-stopped run must succeed with `idempotent: true` and change
nothing. The Operator relies on this: a Stop whose HTTP verdict was lost is **never resent
blindly** (it is reconciled by reading canonical status), but an operator who presses Stop again
must not be punished for it.

---

## What Stop must NOT do

| Must not | Why |
|---|---|
| Disarm the vehicle | Stop ends the mission, not the vehicle's ability to hold station. Disarming a USV on open water removes the only thing keeping it off the rocks. |
| Clear or delete the Pixhawk mission | Stop **restores and rewinds** the mission; it does not remove it. The route must remain inspectable and immediately re-startable. |
| Delete the planning package | The package is the approved artefact, not run state. |
| Invoke RTL | RTL is a distinct, operator-initiated navigation command with its own Home interlock. A stop that silently drives the vehicle home is not a stop. |
| Report the run as `FAILED` | A deliberate stop is a normal outcome. Reporting it as a failure makes real failures unreadable. |
| Be substituted by `rearm` | Rearm prepares the controller for another run; it issues no vehicle command and verifies no hold. |

---

## Operator implementation

| Concern | Where |
|---|---|
| Transport + outcome model | `scout_replan.write` (bounded 3 s connect / 12 s read, shared with Start/Pause/Resume/Rearm) |
| Client + evidence model | `scout_mission_execution.post_stop` / `stop_evidence` / `summarize_status` |
| Transaction | `mission_lifecycle.run_stop` → `_verify_stop` → `_observe_authority_after_stop` |
| Route | `POST /api/vehicles/{id}/mission-execution/stop` (`main.mission_execution_stop`) |
| Availability + presentation | `operator/lib/mission-execution.js` `stopAvailability` / `stopPhase` / `stopOutcomeView` |
| Map control + refresh | `operator/pages/Map.js` (`renderAgentMission`, `onMissionAction`, `missionTransaction`) |
| Diagnostics | `operator/pages/Agent.js` `mxStopCard` |

The authority phase is an **observation**: Scout returns supervisory authority itself, and the
Operator reads it back and reports whether it could confirm it. It writes no authority for a Stop.
Take Control remains the operator's explicit manual override at all times.
