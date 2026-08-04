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
| Map / Vehicle pages | ✅ no `PAUSE MISSION` / `RESUME MISSION` buttons; both point at the Agent page's Mission lifecycle card; manual mode commands (AUTO/MANUAL/LOITER/RTL/ARM/DISARM) retained |

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
