# Control Authority verification

Adds a **control-authority** axis to the command path, *independent* of communication
state. Comm-state answers "can we reach the vehicle?"; authority answers "may operator
commands execute?". A vehicle can be CONNECTED+OPERATOR, CONNECTED+LOCAL_AGENT,
PARTITIONED+LOCAL_AGENT, DISCONNECTED+OPERATOR, etc. Operator-backend-owned (see
`SYSTEM_INFORMATION_MODEL.md`); authority is never derived from, or changed by, comms.

## Model
- `authority ∈ {OPERATOR, LOCAL_AGENT}`, per-vehicle, in-memory. **Default OPERATOR.**
  - **OPERATOR** — safe default, observe-only. Telemetry / maps / events / Pilot / Terminal
    all keep working; command buttons are disabled and the backend refuses to create or
    deliver commands.
  - **LOCAL_AGENT** — operator pressed **Engage Control**; commands may now execute. Engaging
    by itself arms nothing and changes no mode — it only opens the command channel.
- Safe resolution: unknown/never-set/undeterminable → **OPERATOR**. In-memory, so a backend
  restart (or a never-seen vehicle) defaults every vehicle to OPERATOR, never LOCAL_AGENT.

## Backend (`main.py`)
- `authority_by_id` + `get_authority(vid)` (safe default). `AUTHORITY_STATES`.
- `GET /api/authority/{id}` → `{ vehicle_id, authority, since, by }`.
- `POST /api/authority/{id}` `{ authority, by? }` — Engage (`LOCAL_AGENT`) / Release (`OPERATOR`).
  On an actual transition it **cancels (EXPIRES) every non-terminal command** for that
  vehicle via `expire_commands_for_vehicle`, logs an `authority` event, and returns
  `{ changed, previous, expired_commands }`.
- **Gates** (both independent of comm-state):
  - `POST /api/commands` → `409 control not engaged` unless authority == LOCAL_AGENT.
  - `GET /api/commands/pending/{id}` → delivers nothing (and claims nothing) unless
    authority == LOCAL_AGENT, so a command can never execute while observe-only.
- `authority` (+ `authority_since`) ride on `GET /api/fleet/status`, so every page sees it
  live on the 2 s poll.
- **Queued-command safety:** because any transition expires the vehicle's in-flight
  commands, granting or releasing authority can never make a stale queued command suddenly
  execute (the requirement). High-risk ARM/AUTO/RTL are covered by this blanket expiry.
- Authority events: Engage → caution "Control engaged…"; Release → info "Control released…".

## Frontend (Vehicle page)
- `services/api.js`: `getAuthority`, `setAuthority`, `createCommand`, `getCommands`, plus a
  `postJSON` helper that returns `{ ok, status, data }` (does not throw on 409, so the panel
  can act on `needs_confirmation` / `control not engaged`).
- **Command & Control** panel at the top of the Vehicle detail: authority badge (OBSERVE
  ONLY / CONTROL ENGAGED), Engage/Release button, the nine command buttons
  (AUTO/MANUAL/HOLD/GUIDED/RTL/PAUSE/RESUME/ARM/DISARM) **disabled unless engaged**, and a
  live command queue/history. High-risk (ARM/DISARM/RTL/AUTO) are amber and prompt an extra
  confirmation (sent `confirm:true`); a DISCONNECTED create prompts "queue anyway?". The
  queue shows only backend-reported status — the UI never asserts a command executed.

## Verified — backend (curl, fresh instance)
- ✓ startup default authority == OPERATOR (endpoint + fleet status, all vehicles)
- ✓ create command while OPERATOR → `409 control not engaged`
- ✓ Engage (OPERATOR→LOCAL_AGENT) → `changed:true`; create now succeeds (QUEUED)
- ✓ Release transition → the QUEUED command becomes **EXPIRED** (`expired_commands` lists it);
  no active commands remain
- ✓ re-Engage does **not** resurrect/deliver the old command (pending empty)
- ✓ delivery gate: while OPERATOR, `pending` returns `authority:OPERATOR` and 0 commands
- ✓ authority + command events in the log (engage caution, release info, cancels warning)
- ✓ independence: posting agent status flips comm-state to CONNECTED while authority stays
  LOCAL_AGENT; authority is untouched by comm transitions
- ✓ setting the same authority → `changed:false`, no expiry, no event
- ✓ validation: invalid authority → 400 · unknown vehicle → 404 · never-set → OPERATOR

## Verified — frontend (Playwright against the live backend, Scout selected)
- ✓ default: badge **OBSERVE ONLY**, "Engage Control", all 9 command buttons disabled, empty queue
- ✓ Engage: badge **CONTROL ENGAGED**, "Release Control", all 9 buttons enabled
- ✓ issue PAUSE → appears in queue as backend-reported **QUEUED** (never a fabricated "executed")
- ✓ Release: back to OBSERVE ONLY, buttons disabled, and the queued PAUSE shows **EXPIRED**
  with reason "Control authority changed to OPERATOR"
- ✓ live independence visible: Scout rendered comms **DISC** while authority was **CONTROL
  ENGAGED** at the same time
- ✓ no functional console errors (one 409 is the expected DISCONNECTED-confirm path, handled
  by `postJSON` + the auto-confirmed dialog, which then queued the command)

## Notes / honesty
- Engaging control performs **no** vehicle action (no arm/disarm/mode change) — it only
  grants command permission; releasing likewise changes no vehicle mode.
- In-memory, resets to OPERATOR on restart — which is also the safe reconnect behaviour: if
  authority cannot be determined it resolves to OPERATOR (observe-only), never LOCAL_AGENT.
- Command execution is still only ever confirmed by a Local Agent result (see `commands.md`).
