# Bench procedure — Mission-Execution Lifecycle (Start / Pause / Resume / Return)

This is the bench test for the **original mission lifecycle** that surrounds the
already-implemented energy replanning transaction. It is **not** obstacle
avoidance. It exercises the mission-execution controller
(`mission_execution_controller.py`), its Local-Agent API
(`/agent/mission_execution/*` on port 8090), the shared write arbiter, and the
return-to-Home completion monitor.

The controller is **code-complete**. The following still require **hardware /
bench verification** before the first lake test and must not be assumed working
from code alone:

- real Pixhawk `LOITER` / `AUTO` mode changes actually take and hold;
- Set Home to the live launch position and its read-back distance;
- Pause retaining the mission and its sequence (no clear, no reset to wp 0);
- Resume continuing from the retained sequence (Pixhawk resume-from-sequence
  behaviour — see `continuation_verified`);
- the revised safe-return mission replacing the original, and arrival at Home.

## Prerequisites

1. Vehicle Flask service reachable at `LOCAL_FLASK_URL` (default
   `http://127.0.0.1:8080`) with mavlink2rest/Pixhawk connected. Confirm with
   `python3 bench_preflight.py`.
2. Local Agent running (`./run_local_agent.sh`) — its inbound server listens on
   port 8090.
3. A stored **planning package** for the mission under test
   (`GET /agent/replan/planning_package` shows `stored: true`, `usable: true`).
   The package's Home will be **overwritten** with the verified launch Home at
   Start — that is expected.
4. Control authority granted to the Local Agent:
   `curl -s -XPOST $FLASK/agent/control_authority -H 'content-type: application/json' -d '{"authority":"LOCAL_AGENT","reason":"bench"}'`
   Every mission-execution write is gated on `LOCAL_AGENT`; without it, Start
   fails closed with `AUTHORITY_LOST`.

Shell helpers:

```bash
AGENT=http://127.0.0.1:8090
FLASK=http://127.0.0.1:8080
watch_diag() { python3 mission_execution_diag.py --watch; }   # read-only
```

## Procedure

1. **Baseline (read-only).** In a second terminal run `watch_diag`. Confirm
   `state = READY`, `can_start = true`, replanning `fsm = MONITORING`, authority
   `LOCAL_AGENT`.

2. **Start Mission.**
   ```bash
   curl -s -XPOST $AGENT/agent/mission_execution/start \
        -H 'content-type: application/json' -d '{"mission_id":"<mission-id>"}' | jq
   ```
   Watch the diagnostic walk `START_REQUESTED → START_HOLD_REQUESTED →
   START_HOLD_CONFIRMED → SETTING_HOME → VERIFYING_HOME → SYNCHRONIZING_PACKAGE →
   STARTING_AUTO → RUNNING`. Verify on the boat/GCS that it actually reached
   **LOITER**, then **AUTO**. Confirm `verified_home` matches the launch position
   and `home_verification_distance_m` is within tolerance. Confirm the planning
   package Home now equals the launch Home
   (`GET /agent/replan/planning_package`).

3. **Confirm progression.** `state = RUNNING`, observed `mode = AUTO`,
   `sequence.current` advancing.

4. **Pause Mission.**
   ```bash
   curl -s -XPOST $AGENT/agent/mission_execution/pause -d '{}' | jq
   ```
   Verify the boat holds in **LOITER**, `state = PAUSED`,
   `sequence.before_pause` recorded, and the mission is **still loaded**
   (`GET /agent/pixhawk_mission` unchanged — not cleared, not reset to wp 0).
   Re-issue pause once → idempotent success.

5. **Resume Mission.**
   ```bash
   curl -s -XPOST $AGENT/agent/mission_execution/resume -d '{}' | jq
   ```
   Verify **AUTO** re-engaged, `state = RUNNING`. Inspect `sequence.at_resume`,
   `sequence.first_after_resume`, and **`continuation_verified`**. If it is
   `false` with `MISSION_SEQUENCE_RESTART_DETECTED`, the Pixhawk reset to the
   start — capture this; it is exactly the resume-from-sequence behaviour the
   bench test exists to characterise.

6. **Simulate an unsafe energy margin** to hand off to the replanning FSM (uses
   the existing experiment injection):
   ```bash
   curl -s -XPUT $AGENT/agent/replan/experiment \
        -H 'content-type: application/json' \
        -d '{"usv_id":"usv-2","battery_percent":8}' | jq
   ```
   (Enable autonomous replan execution as per the replanning bench doc if you
   want a live transaction rather than dry-run.) Watch mission-execution show
   `effective_state = REPLANNING` and `replanning.active = true`; `can_pause`
   and `can_resume` go `false`. Confirm mission-execution issues **no** competing
   mode command while the replanning controller owns the vehicle.

7. **Return handoff.** When the replanning FSM reaches `MONITORING_REVISED`,
   mission-execution transitions to `RETURNING_HOME`. Confirm the revised
   safe-return mission is running (fresh readback verified on the replan side)
   and `active_route_hash` is the revised hash. The mission is **not** marked
   complete merely because revised AUTO began.

8. **Arrival + final hold.** As the boat closes on Home, watch
   `return_completion.distance_to_home_m` fall inside `arrival_radius_m`
   (default 7.5 m) and `persistence_progress_s` climb toward `persistence_s`
   (default 4 s). On confirmation the controller runs the **final LOITER**:
   `FINAL_HOLD_REQUESTED → COMPLETED_HOLD`. Verify the boat holds in LOITER and
   `return_completion.final_loiter_verified = true`. If the final LOITER cannot
   be verified, the mission is **not** completed — `state` stays
   `RETURNING_HOME` with an explicit `FINAL_LOITER_NOT_VERIFIED` error.

9. **Clear the injection** and **rearm** for another run:
   ```bash
   curl -s -XDELETE $AGENT/agent/replan/experiment | jq
   curl -s -XPOST   $AGENT/agent/mission_execution/rearm -d '{}' | jq
   ```

## Safety notes

- Everything the diagnostic prints is read-only; it can never move the boat.
- Any failure after a confirmed LOITER leaves the boat in **verified LOITER**.
- Loss of `LOCAL_AGENT` authority mid-operation stops further writes and moves
  mission-execution to `SUSPENDED` — it never silently resumes.
- A Local Agent restart mid-operation fails the operation closed
  (`UNKNOWN_AFTER_RESTART`); it is never resumed blind.
- RTL fallback is **not** part of this task and stays disabled.
