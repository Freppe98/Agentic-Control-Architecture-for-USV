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

## Local Agent delivery API (2026-07-17) — what the DEPLOYED Scout actually polls

**Fixes a live integration bug**: the Scout's `local_mission_agent` is configured
`USV_ID = usv-2` and polls **`GET /agent/commands?usv_id=usv-2`** — an endpoint that did
not exist, so the backend answered `{"detail":"Not Found"}` and **no command was ever
claimed**; every `SET_HOME` sat `QUEUED` until its TTL. `GET /api/commands/2` worked, but
that is the operator/UI view, not the Agent delivery path. The originally *planned* agent
path (`GET /api/commands/pending/2`, "Next" below) was never adopted by the Scout.

| | Agent-facing | Operator/UI-facing |
|---|---|---|
| Delivery | `GET /agent/commands?usv_id=usv-2` | `GET /api/commands/pending/{id}` |
| Result | `POST /agent/command_result` (id in body) | `POST /api/commands/{id}/result` (id in path) |
| Fields | `command_id`, `command_type`, `params`, `expires_at` | the full internal record |
| Semantics | **at-least-once** (identical to the operator path) | at-least-once |

Both claim from the **one** queue (`commands`) with the **same** delivery semantics,
differing only in field names — neither is a second source of truth.

### Delivery: at-least-once, with Scout-side deduplication

The **first** fetch is the claim: a `QUEUED` command moves to `SENT` with `claimed_at`
stamped, atomically (the handler is `async def` with no awaits, so the scan/mutate cannot
interleave with a concurrent poll). Thereafter:

| Command state | Delivered? |
|---|---|
| `QUEUED` | yes — and claimed (`SENT`, `claimed_at` set) |
| `SENT` / `ACCEPTED`, no terminal result | **yes, redelivered on every poll** |
| `EXECUTED` / `REJECTED` / `FAILED` / `EXPIRED` | never |

Redelivery continues until a terminal result arrives **or the command expires** — the TTL
(`COMMAND_TTL_S = 300`) is what bounds it, so nothing is redelivered forever. This is what
stops a command being permanently lost when the backend marks it `SENT` but the HTTP
response is dropped by an intermittent link — the condition this system exists for. The
**Scout Local Agent deduplicates by `command_id`**: it records processed ids and rejects a
redelivery without re-executing, so a repeat is inert.

A redelivery is also inert **backend-side**: `claimed_at` keeps the ORIGINAL claim time (it
records when the Agent first took the command, not when it last saw it), and the "sent to
Scout" event fires **once**, on the first claim — a polling Agent cannot flood the log.

`agent_command_view()` exposes only the Agent's four fields — operator-side bookkeeping
(`created_by`, `requested_comm_state`, `warning`, …) never leaves the backend. Terminal and
expired commands are never delivered; `expire_commands()` runs first, so a command past its
TTL expires rather than reaching the vehicle late.

`usv-2` / `2` / `USV-2` all map to internal id 2 (`parse_vehicle_id`). An **unknown**
`usv_id` is a loud `404 unknown vehicle` (listing known ids), never an empty list — a
misconfigured `USV_ID` must not look like "no work to do" forever, which is the failure
mode this endpoint was added for. A missing `usv_id` is a `400` naming the expected form.

Covered by `tests/test_agent_commands.py` (26 tests: claim, shape, redelivery of `SENT`,
`claimed_at` preserved across redeliveries, no duplicate claim event, `ACCEPTED` still
redelivered, expiry of both `QUEUED` and `SENT`, `usv_id` mapping/unknown/missing,
per-vehicle scoping, lowercase result statuses, Set Home annotation, idempotent replay, and
the full queue → claim → redeliver → result → stop round trip).
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

## Command-result contract hardening + SET_HOME canonicalization (2026-07-17)

**Fixes a live incident**: a queued `SET_HOME` sat `SENT` indefinitely — redelivered by
`GET /agent/commands` on every Scout poll — even after the Local Agent caught its own
internal error and attempted to report a terminal `failed` result. Traced to two bugs,
both fixed here:

1. **`POST /agent/command_result` always answered 2xx, even on a single request with an
   unknown `command_id` or an unrecognized `status`.** A caller that checks the HTTP
   status (not just the body) to decide whether to stop retrying would see "200 OK" and
   believe its result was accepted, while the command record was left untouched (still
   `SENT`, `completed_at: null`) — indistinguishable from Scout never having reported
   anything. Fixed: a **single** result now gets an honest status — `404` unknown
   `command_id`, `400` missing id / invalid status — exactly like
   `/api/commands/{id}/result` already did. A **batch** (2+ items, a flushed backlog) is
   unchanged: always 2xx with per-item `found`/`applied`/`error` detail, so one bad id in
   a backlog never fails the whole flush.
2. **Diagnostic logging** (`[COMMAND-RESULT] ...`) now prints the raw incoming body and
   the per-item `found`/`applied`/`status`/`error` outcome for both result endpoints, so a
   future field-name/status-value mismatch between what Scout sends and what this backend
   recognizes is visible in the server log instead of only manifesting as "the command
   never resolves."

Redelivery itself was re-audited and is unchanged: `SENT` is redelivered on every poll,
but that is an **explicit, documented, bounded** lease — bounded by `COMMAND_TTL_S` (300 s)
— not silent or indefinite (see `agent_commands()`'s docstring and
`TestRedeliveryIsExplicitlyBoundedByTTL` in `tests/test_command_result_contract.py`).

**SET_HOME's canonical params changed**: the command now always carries
`params.mode == "current_position"` — Scout picks and verifies its own current position;
a browser-supplied lat/lng is never authoritative (it can be stale by the time the Local
Agent executes). `main.create_command()` canonicalizes this server-side for every
`SET_HOME`, regardless of caller (UI, curl, tests), via `_canonical_set_home_params()` —
one enforcement point, not duplicated in the frontend. Any lat/lng supplied survives only
as non-authoritative audit metadata under `params.requested_position`:
```json
{ "mode": "current_position", "requested_position": { "lat": 56.66, "lng": 12.88 } }
```
`operator/services/api.js`'s `setHome(id, {lat, lng})` is unchanged in signature — it still
forwards whatever fix the UI has for the audit trail — but no longer needs to construct
the canonical contract itself, since the backend now enforces it unconditionally.

Covered by `tests/test_command_result_contract.py` (15 tests): SET_HOME canonicalization
(mode present, audit-only lat/lng, no-lat/lng-supplied case, Scout sees the canonical
params on delivery); single-result honest status (failed/rejected/executed all 200 +
terminal, terminal removes from pending, duplicate is idempotent, unknown id → 404,
invalid status → 400, missing id → 400, garbage body → 400); batch still always-2xx; TTL-
bounded redelivery. `tests/test_agent_commands.py`'s delivered-params assertion updated to
the new canonical shape; the rest of the existing suite (`test_agent_commands`,
`test_set_home`, `test_mode_commands`) is unaffected and green.

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
- Vehicle page's **Control** card (part of the Health-page redesign) shows the same dedicated fetch, plus the full Take Control / Release Control + 9 command buttons + queue/history panel — see `verification/authority.md`. Renders `unknown` (not a guessed `OPERATOR`) when the fetch fails; command buttons stay disabled until authority reads `LOCAL_AGENT`.
- `services/api.js`'s `getControlAuthority(id)` / `setControlAuthority(id, authority)` are unchanged from the previous revision — the frontend contract with the operator backend didn't change, only what happens behind it. `postJSON` now returns `{ ok, status, data }` instead of throwing on 4xx, so the command panel can act on `needs_confirmation` (see `authority.md`).

**Verified** (fresh instance on :8211, live via curl, no fabricated results)
- ✓ command queue regression: `POST /api/commands` with `type:"SET_MODE_AUTO"` still queues normally; `COMMAND_TYPES` no longer contains `SET_CONTROL_AUTHORITY`
- ✓ `GET /api/control_authority/2` with no mock Scout listening → `502 Scout control-authority API unreachable`
- ✓ `GET /api/control_authority/1` (no `SCOUT_API_BASE` entry) → `404 no Scout API configured for this vehicle`
- ✓ `POST /api/control_authority/2` with an invalid `authority` value → `400 invalid authority` (no request made to Scout)
- ✓ full round-trip against a local mock of Scout's `/agent/control_authority` (GET + POST, same contract Scout Flask is expected to implement): `GET /api/control_authority/2` → proxies through and returns the mock's `{"authority":"OPERATOR"}`; `POST {"authority":"LOCAL_AGENT"}` → proxies through, mock state flips, next `GET` reads `LOCAL_AGENT` back
- ✓ `Scripts/control_authority.py`'s `ControlAuthority.poll()` run directly against the same mock: `OPERATOR` → poll → `LOCAL_AGENT`, and a fail-safe check against an unreachable host leaves `authority` unchanged
- Not verified: the real Scout Flask `/agent/control_authority` endpoints don't exist yet (separate codebase, in progress) — this pass verifies the operator-backend proxy and Local Agent client against a stand-in mock with the agreed contract, not the real device.
- Not verified in this pass: the Operator UI (Map/Vehicle pages) was reviewed by reading the rendered markup/handlers, but not click-tested in a real browser (no Node/Playwright available in this environment).

## RTL & LOITER result classification (verification-aware, not optimistic)

**The bug fixed here:** the command panels rendered any `EXECUTED` status as a green "confirmed", and the backend only ever classified `SET_HOME` (`home_result`). So an RTL that Scout completed as a transport but that never actually put the Pixhawk into RTL — `EXECUTED` with `result.verified:false`, or the legacy `{"status":"Returning home"}` HTTP-200 shape — read as a **success the vehicle never performed**. LOITER had no duplicate-press guard.

**Command types (unchanged, canonical — do not rename without coordinating with Scout):** `SET_MODE_AUTO`, `SET_MODE_MANUAL`, `SET_MODE_LOITER`, `RTL`. The UI sends `SET_MODE_LOITER` (never `HOLD` in its place; `SET_MODE_HOLD` remains a demoted, backend-only compatibility type).

**RTL classifier** (`main._annotate_rtl_result`, the twin of `_annotate_set_home_result`) — runs on `process_command_result`, sets `cmd["rtl_result"]`:
- `"confirmed"` **only** when `result.accepted is True` AND `result.verified is True` AND `result.observed_mode` names RTL.
- `"failed"` otherwise — `cmd["reason"]` is replaced with Scout's real error, or a synthesized one from the observed mode:
  - not accepted / legacy `{"status":"Returning home"}` → `"MAVLink rejected the RTL mode change."` (or `error.message`)
  - `verified:false`, observed ≠ RTL → `"Pixhawk remained in <MODE>"`, or `"Mode reverted from RTL to <MODE>"` when `previous_mode` was RTL
  - `verified:false`, no observed mode → `"RTL verification timed out."` (or `error.message`)
- The nested `result` is **preserved verbatim** (`cmd["result"]`), and `cmd["status"]` stays `EXECUTED` — only `rtl_result`/`reason` carry the verdict. Idempotent (a replayed result on a terminal command is a no-op).

**LOITER** is a plain mode command: `EXECUTED` **is** the confirmation (Scout reporting the mode change), so it gets **no** `rtl_result`/`home_result` annotation and is never Home-gated. A `REJECTED` surfaces Scout's reason verbatim (e.g. `"unsupported command_type: SET_MODE_LOITER"`).

**Frontend** — one shared, tested rule (`operator/lib/command.js` `commandVerification`, used by both Map `cmdStatus` and Vehicle's queue row): an `EXECUTED` `RTL`/`SET_HOME` renders green only when its `rtl_result`/`home_result` confirms; otherwise it renders **FAILED** (red) with the real reason on hover. Map additionally suppresses a duplicate press while a same-type command is nonterminal (`hasPendingOfType` + a synchronous `sending` guard) — the LOITER button stays visible (dashed "awaiting" state) but disabled until the outstanding command is terminal.

**Success/failure vocabulary shown to the operator**
- Successful RTL → pill `confirmed` (green); the structured `result` (previous_mode → RTL, observed_mode) is preserved in history.
- Failed RTL → pill `failed` (red) + reason: `"Pixhawk remained in MANUAL"` / `"Mode reverted from RTL to MANUAL"` / `"MAVLink rejected the RTL mode change."` / `"RTL verification timed out."`
- LOITER → `confirmed` only after Scout's `EXECUTED`; otherwise the real rejection/failure reason.

**Live test procedure** (backend on `:8000` — adjust port; `usv-2` is Scout)
1. **Queue + inspect:** `POST /api/commands {"vehicle_id":2,"type":"RTL"}` → note the returned `command.id`. Poll `GET /api/commands/2` (UI view) and `GET /api/commands/history/2` (terminal only) to watch the lifecycle.
2. **Simulate each Scout result** against the claimed command (claim first with `GET /agent/commands?usv_id=usv-2`), via `POST /agent/command_result`:
   - success: `{"command_id":"…","status":"executed","result":{"accepted":true,"verified":true,"observed_mode":"RTL","previous_mode":"MANUAL"}}` → `rtl_result:"confirmed"`.
   - not entered: `…"result":{"accepted":true,"verified":false,"observed_mode":"MANUAL"}` → `rtl_result:"failed"`, `reason:"Pixhawk remained in MANUAL"`.
   - legacy: `…"result":{"status":"Returning home"}` → `rtl_result:"failed"` (never confirmed).
3. **Compare with Scout ground truth:** `GET {SCOUT_API_BASE[2]}/agent/state` (Scout's own reported flight mode) — the operator's `observed_mode`/verdict must agree with Scout's live mode.
4. **Compare with Mission Planner:** the Pixhawk mode indicator (bottom-left flight-mode box) must read `RTL` for a `confirmed` RTL, and the pre-RTL mode for any `failed` one. A green `confirmed` with Mission Planner **not** in RTL is the exact regression this change forbids — report it.
5. **LOITER duplicate guard:** press LOITER on the Map inspector; while it is `QUEUED`/`SENT` the button is disabled (dashed, "already sent — awaiting the vehicle's result"). On `REJECTED` it re-enables and shows the reason.

**Verified** (automated, no live vehicle)
- ✓ backend: `tests/test_mode_commands.py` — RTL confirmed/verified-false/observed-MANUAL/reverted/rejected/failed/error-surfaced/legacy-string, LOITER executed-is-plain-success / rejected-reason-verbatim / never-home-gated.
- ✓ frontend: `tests/command.test.mjs` — `commandVerification` (RTL/SET_HOME/LOITER/AUTO/MANUAL, bare-EXECUTED-is-failure) + `hasPendingOfType` duplicate guard.
- ✓ existing SET_HOME / AUTO / MANUAL / ARM / DISARM tests remain green (`test_agent_commands.py`, `test_set_home.py`, 69 backend + 81 frontend tests pass).
- Not verified in this pass: click-test in a real browser and against a live Scout/Pixhawk (procedure above) — the Scout-side RTL endpoint returning the structured result is a separate codebase change.

**Next**
- Scout-side: add the real `GET`/`POST /agent/control_authority` to `motherpi/services/flask` (separate codebase/session).
- Wire the Scout Local Agent to poll `GET /api/commands/pending/2` for mission commands, execute via Flask/mavlink2rest, and POST results — replacing the curl simulation (control authority does **not** use this path; see above).
- ~~Minimal UI command panel~~ **done** — Vehicle page's Control card: AUTO/MANUAL/HOLD/GUIDED/RTL/PAUSE/RESUME/ARM/DISARM buttons + queue/history, high-risk confirm, existing styling; `services/api.js` gained `createCommand`/`getCommands`. See `verification/authority.md`.
