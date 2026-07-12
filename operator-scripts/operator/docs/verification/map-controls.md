# Map operational controls, environment & diagnostics (2026-07-12)

Fixes the P1 backend/browser errors and adds the primary-view operational controls.

## P1 — errors removed
- **`GET /api/control_authority/1 → 404` (repeated).** Root cause: the Map defaulted its
  selection to `fleet[0]` = the placeholder vehicle **1** (no Scout API), so the 2 s
  authority poll hit an unconfigured id and 404'd in a loop. Fix: default-select the
  first *reporting* vehicle (Scout); and `GET /api/control_authority/{id}` now returns a
  structured **200 `available:false`** for a known vehicle with no Scout API, reserving
  **404** for genuinely unknown ids. Requests always use the currently selected id.
- **`GET /api/environment → 500`.** Root cause: `ZoneInfo("Europe/Stockholm")` was
  called in both the `try` body and the `except` handler — on any host without the tz
  database the handler re-raised → 500. Fix: `safe_local_time()` never raises, and the
  endpoint always returns a **stable partial schema** (`available`, `stale`,
  `source_age_s`, null values on failure, last-known from cache) — it never 500s. The
  Map wind widget hides/dims on `null` without console errors.

## P3 — Map controls (primary operational view)
The inspector now carries, all through the existing command pipeline
(`api.createCommand` → `POST /api/commands`), state **reported by the vehicle**:
- **Modes** (real ArduRover): AUTO, MANUAL, HOLD, LOITER, GUIDED, RTL.
- **Safety**: ARM, DISARM (extra confirm; sent `confirm:true`).
- **Mission (agent)**: PAUSE MISSION, RESUME MISSION — labelled distinctly; these are
  agent mission commands, **not** Pixhawk modes. No "Continue" button exists.
- A "Last command" line shows the lifecycle phase: requested → sent → acknowledged →
  confirmed / rejected / timed-out. Never optimistic.
Buttons enable only on a confirmed `OPERATOR` authority (see `authority.md`).

`SET_MODE_LOITER` was added to the backend `COMMAND_TYPES` (LOITER is a valid ArduRover
boat mode); ARM/DISARM still require explicit `confirm:true`.

## P4 — diagnostics evidence (Vehicle page)
Pixhawk heartbeat and MAVLink now report **real evidence** from `vehicle.mavlink`
(`mavlink_evidence()` in `main.py`), never inferred from GPS/arrival:
- **Pixhawk heartbeat**: PASS ≤3 s, WARN ≤10 s, FAIL beyond, using the last MAVLink
  HEARTBEAT age; NOT AVAILABLE if Scout forwards no heartbeat field.
- **MAVLink**: connection state + last-message age (+ rate); NOT AVAILABLE if absent.
- NOT AVAILABLE never fails the overall System Check. Battery/RC receiver/camera/mission
  correctly remain NOT AVAILABLE while disabled.
- "RC override" split into three: policy (always-available invariant), receiver detected
  (no telem), override active (derived from effective authority `== RC`).
See BACKEND_ROADMAP → "Pixhawk heartbeat / MAVLink evidence" for the Scout-side schema.

## P5 — stale state
Arm, mode and effective authority render **UNKNOWN** when the link is not CONNECTED
(`opsStale`), rather than a possibly-outdated ARMED/DISARMED/mode/authority. Battery and
last-known position stay vivid by design (comms ≠ health).

## Verified (Playwright, live backend + reachable Scout, poster keeping Scout live)
Default = Scout; Take Control pending→confirmed OPERATOR enables the 10 buttons;
Release→LOCAL_AGENT re-locks; selecting id 1 then id 2 follows the selection; diagnostics
Pixhawk heartbeat=PASS, MAVLink=PASS, RC/Camera/Mission=Not available, overall PASS;
**no 404/500, no console errors.**
