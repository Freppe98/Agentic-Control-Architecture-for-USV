# Verification — Agent page + command-result receiver + event recording

Prepared for the practical communication-degradation mission test. Supersedes
`autonomy.md` (the page was renamed **Agent**; the `#/autonomy` route key is kept for
back-compat). Screenshots in `img/agent-0*.png`.

## What changed (files)

Backend (`main.py`)
- **`POST /agent/command_result`** — new. The command-result receiver the Local Agent
  (Scout) posts to; the id is in the **body** (the previous route only accepted the id
  in the path → 405). Accepts a single result, a bare list, or `{results:[…]}`; tolerant
  of field spellings and status aliases; idempotent; always 2xx.
- Shared `process_command_result()` / `normalize_result_status()` — one apply path for
  both result endpoints (`/agent/command_result` and `/api/commands/{id}/result`).
- `normalize_agent_message()` now forwards **`agent_status`** = `payload.agent.*`
  (previously dropped), with last-known carry-forward (`last_known_agent`).
- `record_agent_changes()` — records **agent-decision** and **mission-state** changes as
  first-class events, deduped (only on change, never per poll).
- `set_control_authority()` — records a **control-authority** change event (deduped).

Frontend
- `operator/pages/Agent.js` (renamed from `Autonomy.js`) — a **digested decision view**:
  Current Situation · Current Decision (+ Reason, Confidence) · Watch Conditions ·
  Current Policy · Recent Transitions, with the detailed Observed State below.
- `operator/app.js` — imports `Agent`; route key `autonomy` → `Agent`.
- `operator/services/api.js` — `getEventLog()` (flat event feed), `getAgentReasoning()`;
  removed the dead `getAutonomy()` stub.

Scout (agent-owned reasoning — the emitter side)
- `Scripts/collectors.py` `get_agent_status()` — **extended** to emit the digested
  decision block the Agent page renders: `current_decision`, `decision_reasons` (list),
  `decision_confidence` (HIGH/MEDIUM/LOW), `policy_flags` (list), `watch_conditions`
  (`[{name,state}]`). All derived from the agent's real comm-state / health / mission —
  the Local Agent reporting its own cognition (it owns this per `SYSTEM_INFORMATION_MODEL`).
  Additive: the previous fields (`current_behaviour`, `decision_reason`, `current_policy`,
  `autonomy_level`) are unchanged. The decision/confidence are template heuristics meant
  to be replaced by the FSM output (`fsm_agent.OutputData`) once the runner is wired in.

### `payload.agent` schema the Agent page consumes (rendered verbatim)
```
"agent": {
  "current_decision":     "Continue Search",     # big decision label
  "decision_reasons":     ["Communication degraded.", "Reduced reporting policy active.",
                           "Vehicle health nominal.", "Mission safety unaffected."],
  "decision_confidence":  "HIGH",                  # HIGH | MEDIUM | LOW | <number>%
  "policy_flags":         ["Autonomous continuation", "Reduced reporting", "Buffered messages"],
  "watch_conditions":     [{"name":"Battery","state":"OK"}, {"name":"Heartbeat","state":"OK"},
                           {"name":"GPS","state":"OK"}, {"name":"Operator","state":"LOST"}],
  "current_behaviour":    "search",  "autonomy_level": "ASSISTED",
  "current_policy":       "REDUCED_REPORTING_LOCAL_AUTONOMY", ...
}
```
Any field Scout omits renders as honest "Unavailable"; Watch Conditions the agent omits
are backfilled from the operator's own observed signals (battery, heartbeat, GPS, link).

## 1. Root cause of `POST /agent/command_result` → 405

The backend exposed the result endpoint only as `POST /api/commands/{command_id}/result`
(command id in the URL path). Scout posts results to `POST /agent/command_result` with
the id **in the body**. That path had no route, so FastAPI matched the method table for
`/agent/command_result` (only `GET`-like siblings under `/agent/*` existed) and returned
**405 Method Not Allowed** rather than 404 — the request reached a known path with no
POST handler. Fix: add the `POST /agent/command_result` route (body-carried id), sharing
the same idempotent apply logic.

## 2. Command-result endpoint / schema

```
POST /agent/command_result
Body (single):  { "command_id": "<uuid>", "status": "ACCEPTED|EXECUTED|REJECTED|FAILED",
                  "result"?: any, "reason"?: str, "vehicle_id"?: int }
Body (backlog): [ {…}, {…} ]   or   { "results": [ {…}, … ] }
```
- **id** accepted as `command_id | id | cmd_id | commandId | uuid`.
- **status** accepted as `status | result_status | outcome | state`; aliases normalized:
  `TIMEOUT/TIMED_OUT/ERROR/FAIL → FAILED`, `ACK → ACCEPTED`, `DONE/COMPLETE/SUCCESS/OK →
  EXECUTED`, `DENIED → REJECTED`.
- **reason** accepted as `reason | error | message | detail` (rejected/failed/timeout
  reasons preserved on the record).
- **Idempotent**: keyed by the uuid command id. A result on an already-terminal command
  is a no-op (`applied:false`) — no duplicate history row, no re-execution.
- **Batch always 2xx** (2+ items, a flushed backlog): per-item `found`/`applied` flags
  carry detail, so a buffered Agent can drain its backlog (including unknown/already-
  terminal ids) and stop retrying without one bad id failing the whole flush.
- **Single result gets an honest status** (2026-07-17, fixing a live incident where a
  silent 200 on an unrecognized id/status hid a result that never applied, leaving the
  command `SENT` forever): `404` unknown `command_id`, `400` missing id or an invalid/
  unrecognized `status`, `200` applied — same as `/api/commands/{id}/result`.
- Response (single): `{ ok, applied, found, error, command, received, applied_count }`.
  Response (batch): `{ ok, received, applied_count, results:[{command_id,found,applied,…}] }`.

Backwards compatible: `POST /api/commands/{id}/result` still works (same apply path).

## 3. Test results

### Command-result receiver (P2)
| Case | Result |
|---|---|
| `POST /agent/command_result` single EXECUTED | 200, `applied:true`, command EXECUTED |
| Duplicate result (same id) | 200, `applied:false` (idempotent) |
| History rows for the command | exactly **1** (no duplicate) |
| Batch `{results:[…]}` with alias `TIMEOUT` | 200, mapped → FAILED, reason preserved |
| Batch containing an unknown id | 200, item `found:false`, **flush not failed** |
| Bare-list form `[ {…} ]` | 200 |
| Old id-in-path route | 200 (still works) |
| Empty body (no results) | 400 `no command results in body` |
| Single result, unknown `command_id` | **404** (was silently 200 — see `commands.md`) |
| Single result, invalid/unrecognized `status` | **400** (was silently 200) |
| Single result, missing `command_id` | **400** |

### Agent reasoning forwarding (P1)
`agent_status` present on the fleet vehicle and equal to Scout's `payload.agent.*`
(`current_behaviour`, `decision_reason`, `current_policy`, `autonomy_level`,
`current_communication_state`, `current_mission_state`, `buffer_usage`). Rendered
verbatim on the Agent page; fields Scout does not emit (`current_decision`,
`decision_confidence`, `mission_policy`) render honest "Unavailable / Not emitted".

### Event recording (P3)
| Event type | Trigger | Deduped |
|---|---|---|
| `comms` | operator-side arrival-age transition | yes (on change) |
| `agent` | `current_behaviour`/`decision_reason`/`current_policy` change | yes |
| `mission` | `mission.mission_state` change | yes |
| `authority` | operator Take/Release Control (Scout-confirmed) | yes |
| `command` | queue lifecycle (created/sent/executed/…) | inherent |
Verified: repeating an **identical** status adds **no** new agent/mission events.

### Four communication states (P6)
Driven by arrival age (`main.py` thresholds: PARTITIONED > 15 s, DISCONNECTED > 30 s).
Verified against a SEARCH-mission payload (`img/agent-0*.png`):

| State | Current Situation | Current Decision | Watch conditions |
|---|---|---|---|
| **CONNECTED** (`01`) | CONNECTED · reachable Yes · Healthy · SEARCH · OPERATOR (all LIVE) | **Continue Search** · Confidence **HIGH** | Battery OK · Heartbeat OK · GPS OK · Operator **OK** |
| **PARTITIONED** (`02`, ~17 s) | PARTITIONED · reachable No (LAST KNOWN·17s) · Healthy · SEARCH (LAST KNOWN) · Authority Unknown (stale) | Continue Search (LAST KNOWN·17s) · HIGH | Battery OK · Heartbeat OK · GPS OK · Operator **LOST** |
| **DISCONNECTED** (`03`, ~33 s) | DISCONNECTED · LAST KNOWN·33s · Authority Unknown (stale); banner shown | Continue Search (LAST KNOWN·33s) | Operator **LOST**; vehicle-side flagged LAST KNOWN |
| **CONNECTED again** (`04`) | all LIVE again | Continue Search · HIGH | all OK again |

Reason bullets ("Communication degraded / Reduced reporting policy active / Vehicle
health nominal / Mission safety unaffected") and policy flags ("Autonomous continuation /
Reduced reporting / Buffered messages") shown **verbatim from Scout** and labelled "from
the agent". Recent Transitions render the `DISCONNECTED ↓ CONNECTED ↓ PARTITIONED ↓
Continue Search` chain. No console/page errors in any state.

Map page smoke test after the same changes: inspector, agent-status block (current
SEARCH), Pixhawk card, vehicle marker — all present, no errors (P5, no redesign).

## 4. Stale-data behaviour (P4)

- Real values render **LIVE** while connected; **LAST KNOWN · Xs** (dimmed + age) when
  partitioned/disconnected. Health and comms stay independent — LAST_KNOWN never becomes
  a fault (battery stays vivid).
- Ops-sensitive facts (**vehicle mode, armed, control authority, decision confidence**)
  become **UNKNOWN — stale** rather than showing a last value — we never assert an
  operational fact we can no longer confirm.
- Agent reasoning follows the same rule via `last_known_agent`: on a degraded packet the
  page shows the last reasoning marked LAST KNOWN, plus a banner that reasoning may have
  changed since last contact — never blanked, never presented as live.
- Numeric sentinels are treated as "no data", never shown as values: battery `-1`,
  heartbeat `9999`, negative GPS sats.

## 5. Practical-test checklist

1. Start backend: `python -m uvicorn main:app --host 0.0.0.0 --port 8200`.
2. Point Scout `OPERATOR_URLS` at this PC (see `RUNBOOK.md`); confirm
   `GET /api/fleet/status` from Scout.
3. Open `…/app/#/autonomy` (Agent) and `…/app/#/map`.
4. **CONNECTED** — confirm Current Situation all LIVE; Decision/Reason/Policy show
   Scout's values verbatim; Decision Inputs real; Watch Conditions evaluate.
5. Trigger a command from Map (e.g. MISSION_PAUSE) → confirm Scout receives it and posts
   a result to `/agent/command_result`; confirm no more 405 in the backend log and the
   command reaches EXECUTED once (Events + command history).
6. **Degrade comms**: stop/limit Scout posting. Watch CONNECTED → PARTITIONED (>15 s) →
   DISCONNECTED (>30 s): Agent page flips to LAST KNOWN, Authority/mode/armed →
   UNKNOWN (stale), banner appears, Heartbeat/link-timeout watch condition TRIGGERS.
7. **Restore comms**: Scout resumes → back to CONNECTED, values LIVE.
8. Confirm the **Recent Timeline** + Events page recorded the comms/agent/mission/
   authority/command transitions with timestamps and reasons (no duplicate rows).
9. Confirm the **Map** Pixhawk mission overlay survives the degradation (cached mission
   is not cleared by a failed fetch).
10. If Scout buffered command results while disconnected, confirm the backlog **flushes**
    on reconnect (all 2xx; no duplicate history).

## 6. Still dependent on Scout

The digested decision fields (`current_decision`, `decision_reasons`,
`decision_confidence`, `policy_flags`, `watch_conditions`) are now emitted by the extended
`Scripts/collectors.py get_agent_status()`. Run that build on Scout and the top cards
populate exactly as the screenshots show. Remaining dependencies / caveats:

- The decision/confidence are **template heuristics** derived from comm-state + health.
  For thesis fidelity they should be wired to the real FSM (`fsm_agent.OutputData` —
  `next_state`, `intent.constraints["reason"]`, `explanation`) via `agent_runner.py`,
  which currently runs the FSM but logs to CSV instead of feeding the status reporter.
- `gps_satellites`, `mission_count` — not in Scout's telemetry/mission today (the
  telemetry stub returns an error) → "No data received" in Observed State.
- `control_authority` reflects Scout's Flask `/agent/control_authority`; unreachable Scout
  reads Unknown/Unreachable, and a stale link masks it to "Unknown (stale)". RC override is
  reported-only. (The mockup's "Authority: LOCAL_AGENT" appears once control is released to
  the agent and Scout is reachable.)
- If Scout is NOT updated: the page still works — `current_decision` falls back to
  `current_behaviour`, reason falls back to operator observations (labelled as such),
  confidence reads "Unavailable", and Watch Conditions are evaluated from the operator's
  own observed signals. Nothing is fabricated.
- The `/agent/command_result` result schema is the operator-side contract in §2; Scout's
  reporting code must POST to it (id in body). `command_adapter.py` currently only uploads
  missions and has no result-reporting path yet.
