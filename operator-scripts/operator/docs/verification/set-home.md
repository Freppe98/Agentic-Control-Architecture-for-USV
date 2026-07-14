# Verification — Set Home Here (deployment workflow)

Adds the lake-deployment workflow to the Map page inspector: set + read-back-verify the
Pixhawk `HOME_POSITION` (RTL recovery point) at the deployment site, and gate autonomous
commands on that verification. LOITER is deliberately never gated (anti-drift safety).

## What is real vs. a backend gap

- **Live**: the fleet `home` block (live `HOME_POSITION` Scout forwards in status), the
  operator-backend verification record, command gating, readiness, the map marker.
- **Backend gap**: Scout's `POST /agent/set_home` (and forwarding `home_position` in
  status) is not shipped in this repo yet — see `BACKEND_ROADMAP.md`. Until Scout ships
  it, `SET HOME HERE` returns an honest `scout_unavailable` and Home reads `UNKNOWN`. The
  screenshots below were produced against a local Scout stub (verification harness).

## API contract

`POST /api/vehicles/{id}/commands/set-home`  body `{ lat, lng, confirm:true }`
→ proxies to Scout `POST /agent/set_home {lat,lng}` and returns
`{ ok, verified, phase, code?, message?, home:{lat,lng}, distance_m, verified_at }`.
`verified` is true ONLY when Scout read `HOME_POSITION` back and it landed within
`HOME_VERIFY_TOLERANCE_M` (5 m). Structured failure `code` ∈ `gps_unavailable`,
`position_stale`, `scout_unavailable`, `command_rejected`, `ack_timeout`,
`readback_timeout`, `verification_out_of_tolerance`. Threads a `SET_HOME` command record
(uuid/timestamps) — EXECUTED only on verification, else REJECTED/FAILED.

## UI states

![HOME NOT VERIFIED](img/set-home-01-not-verified.png)
HOME NOT VERIFIED — Pixhawk Home 1.9 km from Scout; coords, distance, freshness,
verification rows; SET HOME HERE enabled (operator holds control); readiness checklist.

![Confirmation dialog](img/set-home-02-dialog.png)
Confirmation dialog — current Scout position, existing Pixhawk Home, distance, safety
caution, CANCEL / SET AND VERIFY HOME. Not a one-click action.

![Command gating](img/set-home-03-gated-commands.png)
Interlock while unverified — AUTO / RTL / RESUME MISSION disabled with the reason;
LOITER, MANUAL, ARM/DISARM, PAUSE stay enabled; readiness banner notes LOITER remains a
safety hold.

![HOME VERIFIED](img/set-home-04-verified.png)
After SET AND VERIFY HOME — HOME VERIFIED (~1.4 m from Scout), marker turns green,
AUTO/RTL/RESUME unlocked, Home verified ✓ in the readiness checklist.

## Manual steps (against a live Scout)

1. Load the mission; place the Scout in the water at a safe recovery point; wait for a
   valid GPS fix (Scout CONNECTED, position shown).
2. Take Control (OPERATOR). Confirm `SET HOME HERE` becomes enabled and AUTO/RTL/RESUME
   are disabled with "set and verify Pixhawk Home…".
3. Press `SET HOME HERE` → dialog. Press CANCEL — confirm no request is sent and Home
   stays NOT VERIFIED.
4. Press `SET HOME HERE` → SET AND VERIFY HOME. Observe SETTING HOME (pending), then
   HOME VERIFIED with the read-back distance and time.
5. Confirm AUTO/RTL/RESUME are now enabled, the marker is green, and readiness shows
   Home verified ✓. Confirm LOITER was enabled throughout.

## LOITER as the primary safety hold

LOITER (active station-keeping / anti-drift) is the Scout's primary safety hold and is
presented as such on both command surfaces; the passive `SET_MODE_HOLD` (kept for backend
compatibility) is demoted, never shown as LOITER's equal. Taxonomy lives in `lib/home.js`
(`SAFETY_HOLD_TYPE`, `PRIMARY_MODES`, `ADVANCED_MODES`).

- **Map** — primary modes `AUTO · MANUAL · LOITER · RTL` (no HOLD); LOITER carries a green
  "safety" style and stays enabled while AUTO/RTL/RESUME are Home-gated. Readiness banner:
  "Autonomous mission not ready. LOITER remains available as an immediate anti-drift safety hold."
- **Vehicle** — primary row `AUTO · MANUAL · LOITER · RTL · PAUSE · RESUME · ARM · DISARM`;
  HOLD + GUIDED moved into a collapsed **Advanced modes** group with a note that HOLD is a
  passive hold (may drift) and LOITER is the active anti-drift hold.

## Automated coverage

- Backend: `python -m unittest tests.test_set_home tests.test_mode_commands` (20 tests —
  set-home success/guards/failure codes/tolerance/`home` block; LOITER routes as
  `SET_MODE_LOITER` with no forced confirmation; HOLD still accepted for compatibility).
- Frontend: `npm test` (25 tests — home status + gating policy; LOITER is the safety hold
  and in `PRIMARY_MODES`, HOLD is advanced-only, LOITER enabled when Home unverified /
  readiness false while AUTO/RTL/RESUME stay disabled).
