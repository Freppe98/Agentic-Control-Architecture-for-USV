# Command queue verification (backend — reverse/control Path E)

First slice of the Operator → Scout command path from `BACKEND_ROADMAP.md` ("Command / control (reverse path)"). Operator-backend-owned (see `SYSTEM_INFORMATION_MODEL.md`): the backend is the queue's source of truth, gates commands on the operator-side comm-state, and records the Local Agent's result — it **never fabricates execution**. Backend only; no UI, no waypoints/mission editing yet.

**Added** (all in `main.py`, in-memory like `event_log` / comms history — resets on restart)
- Command store: `commands` (append-only) + `commands_by_id` keyed by a uuid `id` (the dedup key that prevents duplicate execution).
- `COMMAND_TYPES` = SET_MODE_AUTO, SET_MODE_MANUAL, SET_MODE_HOLD, SET_MODE_GUIDED, RTL, MISSION_PAUSE, MISSION_RESUME, **ARM, DISARM**. `COMMAND_TTL_S = 300`.
- **ARM/DISARM (`CONFIRM_REQUIRED_TYPES`)** touch the motors, so both **always** require `confirm:true` regardless of comm-state — `409 needs_confirmation` otherwise (`high_risk:true` for ARM). The record carries a `RISK_WARNING` (ARM: "the vehicle can move under power once armed"; DISARM: lower). Lifecycle is **unchanged** (QUEUED → SENT → EXECUTED/REJECTED/FAILED/EXPIRED) and success is still only ever set by a Local Agent result.
- Lifecycle, by owner: **backend** sets `QUEUED` (create) → `SENT` (on Agent fetch = claim) → `EXPIRED` (monitor loop, past TTL). **Local Agent only** sets `ACCEPTED` / `EXECUTED` / `REJECTED` / `FAILED` via the result endpoint. Terminal = `{EXECUTED, REJECTED, FAILED, EXPIRED}`.
- Command record = the spec object: `id, vehicle_id, vehicle, type, params, status, created_at, expires_at, created_by, requested_comm_state, claimed_at, completed_at, result, reason` (+ `warning`).
- Comm-state gate on create (operator-side, from `comms_state_by_id`): CONNECTED → queue immediately · PARTITIONED → queue + `warning` (caution event) · DISCONNECTED → `409 needs_confirmation` unless `confirm:true` (then queue until contact/TTL). `UNKNOWN` (never-contacted template vehicle) queues immediately.
- Endpoints: `POST /api/commands` · `GET /api/commands/pending/{id}` (claims QUEUED→SENT, redelivers SENT at-least-once) · `POST /api/commands/{command_id}/result` (idempotent) · `GET /api/commands/{id}` (queue + history + active) · `GET /api/commands/history/{id}` (terminal only).
- Every create / claim / result / expiry emits a first-class `type:"command"` event into the existing log (Events page unchanged). Expiry is also swept once per second in `_comms_monitor_loop`.

**Verified** (fresh instance on :8210, live lifecycle via curl + a timed Python helper)
- ✓ create SET_MODE_AUTO for usv-2 → `QUEUED`, uuid id, `expires_at` = +300 s, `requested_comm_state` stamped
- ✓ `pending/2` claims it → `SENT` with `claimed_at`; a second `pending/2` still returns it (SENT redelivered, at-least-once)
- ✓ Agent result `EXECUTED` → status `EXECUTED`, `completed_at` set, `result` stored
- ✓ duplicate `EXECUTED` result → `applied:false`, status unchanged (idempotent — no double execution)
- ✓ after terminal, `pending/2` is empty; `history/2` shows the executed command
- ✓ event log records `command` events: created (info) · sent to Scout (info) · executed (info, source `usv-2`)
- ✓ validation: unknown type → 400 · unknown vehicle → 404 · unknown command id → 404 · bad result status on a valid id → 400
- ✓ CONNECTED create (after a fresh `POST /agent/status`) → `requested_comm_state:CONNECTED`, no warning
- ✓ PARTITIONED create (link aged >15 s) → `QUEUED` + `warning`, caution event
- ✓ DISCONNECTED create (link aged >30 s) → `409 needs_confirmation`; resend with `confirm:true` → `QUEUED`, `requested_comm_state:DISCONNECTED`

**Verified — ARM/DISARM** (same instance)
- ✓ ARM without confirm → `409 needs_confirmation`, `high_risk:true`
- ✓ DISARM without confirm → `409 needs_confirmation`, `high_risk:false`
- ✓ ARM with `confirm:true` → `QUEUED` with high-risk warning in the record; caution `created` event
- ✓ DISARM with `confirm:true` → `QUEUED` with the lower-risk warning; caution `created` event
- ✓ lifecycle unchanged: `pending/2` claims ARM → `SENT`; Agent result `EXECUTED` → `applied:true`, status `EXECUTED` (backend never set it)
- ✓ command events logged: created (caution) · sent to Scout (info) · executed (info, source `usv-2`)
- ✓ regression: a non-arming command (SET_MODE_AUTO) still queues with no confirm (`http 200`, `QUEUED`)

**Notes / honesty**
- The backend never marks a command EXECUTED — only a Local Agent result endpoint call can. Nothing in this slice implies the vehicle acted.
- In-memory store, resets on restart (like the comms/event logs). Durable storage is out of scope for this slice.
- Delivery is at-least-once: `pending` redelivers a SENT command until a result arrives, so the **Local Agent must dedup by `id`**. The backend guarantees no duplicate *record* transition (results on terminal commands are ignored).
- Claim-on-fetch (QUEUED→SENT on `GET pending`) means a SENT command whose delivery response was lost is still redelivered — correct for reliability; the id dedup covers the double-send.

## Control authority (dedicated API — deliberately NOT this queue)

Fixes a live-testing finding: the Local Agent must never assume control of the Pixhawk on its own. `OPERATOR` (default — RC has exclusive authority) vs `LOCAL_AGENT` (operator has explicitly handed off control) is a supervisory flag, independent of `SET_MODE_*`/ARM/DISARM.

**Revision (superseding an earlier version of this section):** control authority was first built as a `SET_CONTROL_AUTHORITY` entry in this same command queue. That was wrong — architecture direction is that control authority is **vehicle state owned by Scout's own Flask service** (`motherpi/services/flask`, a separate on-vehicle codebase, not part of this repo), not an operator-issued mission command. It has been pulled back out: `COMMAND_TYPES`, `command_result`, and everything else on this page above is exactly as it was before control authority touched it — nothing here changed. Mission commands (ARM, AUTO, RTL, HOLD, etc.) continue to use this queue exclusively.

**Architecture**
```
Operator UI → Operator Backend (GET/POST /api/control_authority/{vehicle})
Operator Backend → Scout Flask (GET/POST /agent/control_authority) — new direct REST call, live proxy, no queue
Local Agent (Scripts/) → Scout Flask (GET /agent/control_authority) — reads and obeys directly
```
The operator backend holds **no authority state of its own** — every `/api/control_authority/{vehicle}` call is a synchronous `requests` round-trip to Scout Flask at `SCOUT_API_BASE[vehicle]` (`main.py`, same hardcoded "no Configuration API yet" per-vehicle map already used by `Pilot.js`'s `DASHBOARDS` / `Terminal.js`'s `SSH_TARGETS` — currently just `{2: "http://10.0.2.10:8080"}`). A network failure surfaces as an honest `502`, never a cached or guessed value — nothing here fabricates state the way the command queue above is careful not to fabricate execution.

**Added** (`main.py`)
- `SCOUT_API_BASE` + `scout_api_base(vid)`.
- `POST /api/control_authority/{vehicle}` — body `{"authority": "LOCAL_AGENT"|"OPERATOR"}`. Validates the value, looks up the vehicle's Scout Flask base (`404` if unmapped), forwards to `POST {base}/agent/control_authority`, returns Scout's response verbatim. `502` on any `requests.RequestException`.
- `GET /api/control_authority/{vehicle}` — same lookup, forwards to `GET {base}/agent/control_authority`, returns Scout's response verbatim.

**Added** (`Scripts/control_authority.py`, `Scripts/agent_runner.py`)
- `ControlAuthority` (stdlib `urllib`, no new dependency) now takes Scout Flask's own base URL (`--scout-api-url`, default `http://127.0.0.1:8080` — same host as the Local Agent) and reads `GET /agent/control_authority` directly. No vehicle id (local to that Pi), no result-posting (a plain read, not a queued command with a lifecycle). On any network failure or unrecognized value, `authority` is left unchanged — fails closed to the last known state, never silently grants control.
- `agent_runner.py` gates the one real Pixhawk write path in the repo — mission upload — behind `authority.has_control()`. Everything else (telemetry reads, FSM decisions, logging) is unaffected; RC keeps exclusive authority whenever `OPERATOR` holds it, since the Local Agent sends nothing to the flight controller.
- Out of scope here: MAVLink ARM/DISARM/mode-set execution doesn't exist anywhere in this repo yet (tracked separately in `BACKEND_ROADMAP.md`, "Feature unavailable" on the agent side) — this slice only gates what already exists (mission upload). The actual Scout Flask `/agent/control_authority` endpoints are being built in the Scout-side codebase, not here.

**Added** (Operator UI)
- Map page "Quick actions" panel: **Take Control** / **Release Control** buttons + a **Current authority: Operator/Local Agent/Unknown** line, from a dedicated `GET /api/control_authority/{id}` fetch on selection + a 2 s refresh timer — separate from the fleet poll and separate from the command queue. The four mocked mission-command buttons (Return Home/Pause/Resume/Loiter) are disabled with "Local Agent does not have control — Take Control first" whenever authority isn't `LOCAL_AGENT` (still additionally mocked pending real command execution — see BACKEND_ROADMAP).
- Vehicle page's Health overview card gains a read-only **Control authority** row, same dedicated fetch pattern; renders `unknown` (not a guessed `OPERATOR`) when the fetch fails.
- `services/api.js`'s `getControlAuthority(id)` / `setControlAuthority(id, authority)` are unchanged from the previous revision — the frontend contract with the operator backend didn't change, only what happens behind it.

**Verified** (fresh instance on :8211, live via curl, no fabricated results)
- ✓ command queue regression: `POST /api/commands` with `type:"SET_MODE_AUTO"` still queues normally; `COMMAND_TYPES` no longer contains `SET_CONTROL_AUTHORITY`
- ✓ `GET /api/control_authority/2` with no mock Scout listening → `502 Scout control-authority API unreachable`
- ✓ `GET /api/control_authority/1` (no `SCOUT_API_BASE` entry) → `404 no Scout API configured for this vehicle`
- ✓ `POST /api/control_authority/2` with an invalid `authority` value → `400 invalid authority` (no request made to Scout)
- ✓ full round-trip against a local mock of Scout's `/agent/control_authority` (GET + POST, same contract Scout Flask is expected to implement): `GET /api/control_authority/2` → proxies through and returns the mock's `{"authority":"OPERATOR"}`; `POST {"authority":"LOCAL_AGENT"}` → proxies through, mock state flips, next `GET` reads `LOCAL_AGENT` back
- ✓ `Scripts/control_authority.py`'s `ControlAuthority.poll()` run directly against the same mock: `OPERATOR` → poll → `LOCAL_AGENT`, and a fail-safe check against an unreachable host leaves `authority` unchanged
- Not verified: the real Scout Flask `/agent/control_authority` endpoints don't exist yet (separate codebase, in progress) — this pass verifies the operator-backend proxy and Local Agent client against a stand-in mock with the agreed contract, not the real device.
- Not verified in this pass: the Operator UI (Map/Vehicle pages) was reviewed by reading the rendered markup/handlers, but not click-tested in a real browser (no Node/Playwright available in this environment).

**Next**
- Scout-side: add the real `GET`/`POST /agent/control_authority` to `motherpi/services/flask` (separate codebase/session).
- Wire the Scout Local Agent to poll `GET /api/commands/pending/2` for mission commands, execute via Flask/mavlink2rest, and POST results — replacing the curl simulation (control authority does **not** use this path; see above).
- Minimal UI command panel on an existing page (Vehicle or Pilot/Autonomy): AUTO/MANUAL/HOLD/GUIDED/RTL/PAUSE/RESUME buttons + queue/history, RTL confirm, existing styling. `services/api.js` gains the command methods (currently the backend is exercised directly).
