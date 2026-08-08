# Mission execution lifecycle — verification

Scout's Local Agent (port 8090) owns the mission-execution lifecycle as complete, verified
transactions. The Operator Station is a thin per-vehicle proxy plus one authoritative control on
the Agent page. It runs no second FSM, issues no separate LOITER / Set Home / AUTO for Start, and
routes none of this through the command queue or the Flask (8080) Pixhawk surface.

Routes consumed: `GET|POST /agent/mission_execution/{status,start,pause,resume,rearm}`.

## Automated

```powershell
python -m unittest tests.test_mission_execution     # 49 tests
node --test tests/mission-execution.test.mjs        # 62 tests
python scripts/_baseline_checks.py                  # route table + api.js endpoint resolution
```

## Bench-verified against a mock Local Agent implementing the contract

Driven headless (Playwright, `npm install --no-save playwright`), Agent page, vehicle Scout:

| Check | Result |
| --- | --- |
| Card renders from Scout status only | ✅ state / effective state / mode / authority / mission id / route hashes / active operation id |
| `can_start` → **Start Mission** enabled | ✅ |
| **The button does not flip on click** — 2 s Start transaction | ✅ label stays `Start Mission`, *disabled*, for the whole 2 s; becomes `Pause Mission` only after Scout reports `RUNNING` |
| `can_pause` → `Pause Mission` (never "Stop Mission") | ✅ |
| `can_resume` → `Resume Mission` | ✅ |
| Home card | ✅ requested launch Home, verified Home, 0.62 m verification distance, VERIFIED BY SCOUT, package SYNCHRONIZED, distance to Home |
| "Start Mission resets Home to the launch position" warning | ✅ shown on the Home card and under the primary control |
| Sequence evidence | ✅ current/count, before pause 4, at resume 4, first after resume 0, continuation FALSE |
| Continuation warning on `continuation_verified: false` | ✅ red-bordered "AUTO resumed, but continuation from the paused waypoint was not verified… waypoint 0" — shown despite verified AUTO |
| Return completion | ✅ distance, arrival radius, persistence 1/4 s + progress bar, ARRIVAL NOT CONFIRMED, FINAL LOITER NOT VERIFIED, MISSION NOT COMPLETE |
| Operation trace | ✅ `START ACCEPTED → RUNNING · mode AUTO · CONTINUATION NOT VERIFIED · op-a17`, then `PAUSE ACCEPTED → PAUSED · mode LOITER · op-a18` |
| No console errors | ✅ |
| Vehicle page | ✅ no `PAUSE MISSION` / `RESUME MISSION` buttons; points at the lifecycle card; manual mode commands (AUTO/MANUAL/LOITER/RTL/ARM/DISARM) retained |

> **Superseded for the Map.** This table predates the product decision that made the **Map** the
> operational surface: normal Start / Pause / Resume / Stop now live in the Map's Agent Mission
> card and the Agent page keeps the diagnostic depth. The row above applies to the Vehicle page
> only. See `tests/mission-control.test.mjs` for the pinned state → control mapping.

## The hold controls are STATE-driven; `can_pause` / `can_resume` only gate them

A live bench Start (DISARMED → ARMED/AUTO/LOCAL_AGENT, Agent Mission RUNNING, Home VERIFIED)
reached RUNNING with no usable hold control on the Map. Cause: `can_pause` / `can_resume` were
read with a strict `=== true`, so a Scout status that simply does not carry the optional
capability flag was treated as an explicit refusal — the card rendered `Pause` disabled with the
**fabricated** reason "Scout reports can_pause=false", beside a `Stop Mission` that was disabled
for the same kind of reason. Two dead buttons.

They are now tri-state, and so is `can_stop`:

| Scout's status | Control | Why |
| --- | --- | --- |
| `can_pause: true` | **Pause Mission**, enabled | Scout will accept it |
| `can_pause: false` | Pause Mission, **disabled**, "Scout reports can_pause=false in RUNNING" | Scout has refused; the station does not talk it into a 409 |
| *(key absent)* | **Pause Mission**, enabled | Scout said nothing. The STATE is the authority, and the backend transaction is fail-closed |

The same three rows hold for `can_resume` in `PAUSED`. Which control EXISTS still comes from
Scout's state alone (`RUNNING`/`RETURNING_HOME` → Pause, `PAUSED` → Resume), and an active
operation id, a mid-transaction state or the replanning overlay still withholds it.

## Stop Mission — Scout's safe abort, beside the hold control

`POST /agent/mission_execution/stop` is a first-class Scout lifecycle operation, proxied by
`POST /api/vehicles/{id}/mission-execution/stop`. It is **not** the legacy raw Pixhawk stop (which
this station does not expose at all) and **not** a mission deletion.

| Scout state | Controls |
| --- | --- |
| `RUNNING` / `RETURNING_HOME` / `HOME_ARRIVAL_PENDING` | **Pause Mission** · **Stop Mission** |
| `PAUSED` | **Resume Mission** · **Stop Mission** |
| `SUSPENDED` (e.g. after a failed replan) | **Rearm Mission Controller** · **Stop Mission** · Take Control |

Pause and Stop are deliberately different actions: Pause is a temporary LOITER that retains the
execution position so Resume continues the same run; Stop holds the vehicle, restores the original
mission if a revised route is installed, rewinds it to the beginning, clears the execution/replan
test state and returns control authority to the operator, ready for a clean new Start.

While a Stop runs the card shows Scout's own phases — *Stopping mission… → Holding position… →
Restoring original mission… → Rewinding mission… → Verifying reset…* — every control is disabled,
and nothing is claimed until Scout's transaction completes.

A successful Stop leaves Scout at `state=NOT_READY`, `start_eligible=true`,
`authority_blocks_start=true`, `authority=OPERATOR`. **This is expected, not a failure**: authority
is deliberately back with the operator, and the Start transaction hands it to the Local Agent
again — so `Start Mission` remains available and the card reads *Mission stopped* with the proven
claims beneath it.

A Stop that fails after the safe hold leaves `SUSPENDED` with Scout's own code
(`STOP_ACTIVE_MISSION_UNKNOWN`, `STOP_RESTORE_UPLOAD_FAILED`, `STOP_RESTORE_HASH_MISMATCH`,
`STOP_REWIND_NOT_VERIFIED`). The card shows the exact code and states that the vehicle is being
held in LOITER with the reset incomplete. The station runs **no** automatic recovery.

After a successful Stop the station re-reads mission-execution status, re-runs the one-shot
preflight, refreshes control authority and forces a fresh Pixhawk mission download (`"stop"` is a
force reason in `lib/mission-refresh.js`) so the overlay, the active waypoint and the progress
readout return to the original mission at its beginning. The map is **not** recentred.

`ACCEPTED` is also no longer shown as a clean result when the backend's post-operation read-back
(`mission_lifecycle._verify_state`) answered `withheld`: the Map's result line reads
**"accepted — resulting state NOT verified"** with Scout's observed state/mode in the tooltip.
That verdict was already computed and parsed; it was simply rendered nowhere.

## Two things the console called by the same name

`[STATUS]` printed `comm=` and `mission=` straight out of the packet, which read as the operator's
own verdict and produced two contradictions that were not bugs:

* `comm=PARTITIONED` beside a UI showing CONNECTED — the operator's comm state is **arrival-age
  derived** (`build_vehicle_view`), so a packet reaching us proves the link is up now even when
  the payload self-reports a partition the vehicle saw earlier.
* `mission=IDLE` beside an Agent Mission card showing RUNNING — this is the **supervisory agent's
  decision state** (`payload.mission.mission_state`), not Scout's mission-execution lifecycle
  (port 8090). A supervisory loop that is not deciding anything while the mission controller flies
  the route is correct.

The line now reads `agent_comm=… link=… agent_mission=…` (the vehicle's word, the operator's
verdict, the vehicle's word), and the Map inspector section formerly titled "Agent status" is
**"Supervisory agent · decision state"**, tagged *not the mission lifecycle*.

## Verified against the REAL deployed Scout (10.0.2.10:8090)

This Scout does **not** implement the lifecycle yet, and it fails in a way worth recording:
its Local Agent routes with `self.path.startswith("/agent/mission")`, so
`GET /agent/mission_execution/status` is swallowed by the legacy `/agent/mission` handler and
answers **HTTP 200 with a Pixhawk mission readback** (`mission_count`, `waypoints`,
`mission_hash`) — not a 404.

Accepting that body would have rendered a lifecycle card claiming the lifecycle is supported
with every field blank. Both the backend (`scout_mission_execution.is_status_body`) and the
frontend (`isStatusBody`) therefore require at least one identifying lifecycle field
(`state` / `effective_state` / `execution_state` / `mission_execution_enabled` / `can_*`) before
accepting a 200 as a status. Observed result against the live vehicle:

```
supported: False
error: This Scout answered /agent/mission_execution/status with a body that is not a
       mission-execution status (an older Local Agent prefix-matches /agent/mission and
       returns its Pixhawk mission readback) — the lifecycle is not supported
summary: state=null  can_start=null  final_loiter_verified=null
```

The Agent page shows **"Mission lifecycle not supported by this Scout version"** and offers no
action. `POST .../start` against the same Scout returns 404 → `supported:false`. Nothing is
fabricated.

## NOT verified — requires hardware bench time

None of the following has been exercised against a real Pixhawk, and the first lake run is **not**
validated:

- real launch LOITER verification and the `LOITER_NOT_VERIFIED` path;
- real `SET_HOME` + Home read-back and the verification distance under GPS noise;
- planning-package Home synchronization and `PACKAGE_INCONSISTENT_AFTER_SYNC`;
- real AUTO verification, progression confirmation, and `PROGRESSION_UNCONFIRMED`;
- **pause/resume continuation** — whether the Pixhawk resumes at the paused waypoint or restarts
  at 0 (the `continuation_verified: false` warning exists precisely because this is unproven);
- the replanning handoff (`effective_state: REPLANNING` → `MONITORING_REVISED` → `RETURNING_HOME`);
- revised-mission return, arrival persistence against a real fix, final LOITER verification and
  `COMPLETED_HOLD`;
- a genuine write timeout on a degraded link and its reconciliation (only simulated in tests).
