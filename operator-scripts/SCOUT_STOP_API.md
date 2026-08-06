# Required Scout API: `POST /agent/mission_execution/stop`

**Status: NOT IMPLEMENTED ON SCOUT.** The Operator Station's model, proxy, transaction and UI
are already written against the contract below. Until Scout ships it, every Stop attempt
answers `unsupported` (Scout 404 → operator `supported:false`), the Map card shows **Stop** as
disabled with that exact reason, and **nothing is fabricated**: no synthesized terminal state,
no low-level LOITER standing in for a stop, no Rearm pretending to be one.

This document is the Operator side's requirement statement for the Scout repository. Nothing in
the Scout repository was changed by the task that produced it.

---

## Why the Operator cannot emulate Stop

Pause is not Stop. Pause holds position with the mission still loaded and the sequence
recorded, so a Resume continues from the paused waypoint; the run is still live and the Local
Agent still owns the vehicle. Stop **ends the run**: after it, there is no mission in progress
to resume, and control may legitimately return to the operator.

The Operator Station could technically issue `SET_MODE_LOITER` and then mark the mission
"stopped" in its own memory. It deliberately does not, for two reasons:

1. That is a **second mission-execution lifecycle** competing with Scout's. Scout would still
   believe the mission is RUNNING or PAUSED and could resume commanding the vehicle; the
   operator would be looking at a station that says otherwise. Every other lifecycle fact in
   this station is Scout's own word, and Stop must not be the exception.
2. A locally-invented "stopped" gives no basis for the authority decision. Returning OPERATOR
   authority is only safe once the vehicle is verifiably holding, and only Scout can verify
   that as part of its own transaction.

---

## Contract

### Request

```
POST /agent/mission_execution/stop
Content-Type: application/json

{ "mission_id": "msn-..." }        // optional; when present Scout fails closed on a mismatch
```

The Operator always sends the vehicle's **active persisted mission id** when it has one, so
Scout can refuse with `MISSION_ID_MISMATCH` rather than stopping a run the operator did not
mean. An empty body `{}` is valid.

### State sequence

Stop is one Scout-side transaction with the same shape as Start, and it must publish each step
through the existing `state` field of `GET /agent/mission_execution/status`:

```
RUNNING | PAUSED | RETURNING_HOME
      -> STOP_REQUESTED           accepted; the transaction has begun
      -> STOP_HOLD_REQUESTED      a hold has been commanded
      -> STOP_HOLD_CONFIRMED      the hold was READ BACK and verified
      -> STOPPED                  terminal; the run is over and the vehicle is holding
```

`STOPPED` is a **resting** state. `CANCELLED` is accepted by the Operator as a synonym.

### Response

Same envelope as `start` / `pause` / `resume`:

```json
{
  "accepted": true,
  "operation": "stop",
  "operation_id": "op-...",
  "previous_state": "RUNNING",
  "current_state": "STOPPED",
  "execution_state": "STOPPED",
  "verified_mode": "LOITER",
  "mission_id": "msn-...",
  "sequence": { "current": 7, "count": 22 },
  "final": true,
  "idempotent": false,
  "error": null
}
```

HTTP 200 with a body-level `error` (or `accepted:false`) is a **vehicle-level failure**, exactly
as for the other operations — the Operator reads the body, not the status line. HTTP 409 is a
definite refusal (precondition, lifecycle state, replanning ownership, write arbitration).

### Status field

`GET /agent/mission_execution/status` must gain:

```json
{ "can_stop": true }
```

**Presence of the key is the support signal**, not its value. A Scout that has shipped Stop
reports `can_stop` (true or false); one that has not omits it entirely. The Operator uses
exactly this distinction to tell "Stop is not available on this Scout version" apart from
"Stop is not available right now", and shows different, honest copy for each.

### Idempotency

A Stop issued while already `STOPPED` must succeed with `idempotent: true` and change nothing.
The Operator relies on this: a Stop whose HTTP verdict was lost is **never resent blindly**, but
an operator who presses Stop again on an already-stopped run must not be punished for it.

---

## What Stop must NOT do

These are requirements, not preferences. Each corresponds to a behaviour the Operator will
refuse to present as a stop:

| Must not | Why |
|---|---|
| Disarm the vehicle | Stop ends the mission, not the vehicle's ability to hold station. Disarming a USV on open water removes the only thing keeping it off the rocks. |
| Clear the Pixhawk mission | The mission must remain inspectable and re-startable. Clearing it destroys the read-back evidence the whole approval chain rests on. |
| Delete the planning package | Same: the package is the approved artefact, not run state. |
| Invoke RTL | RTL is a distinct, operator-initiated navigation command with its own Home interlock. A stop that silently drives the vehicle home is not a stop. |
| Report the run as `FAILED` | A deliberate stop is a normal outcome. Reporting it as a failure makes real failures unreadable. |
| Be substituted by `rearm` | Rearm prepares the controller for another run; it issues no vehicle command and verifies no hold. It is not a stop and the Operator will not offer it as one. |

---

## Authority handover (Operator side, for reference)

The Operator returns control authority to `OPERATOR` **only** after reading canonical status and
finding **both**:

* `state` ∈ { `STOPPED`, `CANCELLED` }, and
* a verified `LOITER` (`mode` / `verified_mode`).

Anything less — a stop still moving through `STOP_HOLD_REQUESTED`, an unknown outcome, a run
still going — leaves authority with the Local Agent and says so on the card. Take Control
remains the operator's explicit manual override at all times.

Implementation: `mission_lifecycle.run_stop` / `_return_operator_after_stop`
(`operator-scripts/mission_lifecycle.py`), proxied by `scout_mission_execution.post_stop` and
exposed as `POST /api/vehicles/{id}/mission-execution/stop`.
