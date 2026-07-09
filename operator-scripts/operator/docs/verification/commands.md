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

**Next**
- Wire the Scout Local Agent to poll `GET /api/commands/pending/2`, execute via Flask/mavlink2rest, and POST results — replacing the curl simulation.
- Minimal UI command panel on an existing page (Vehicle or Pilot/Autonomy): AUTO/MANUAL/HOLD/GUIDED/RTL/PAUSE/RESUME buttons + queue/history, RTL confirm, existing styling. `services/api.js` gains the command methods (currently the backend is exercised directly).
