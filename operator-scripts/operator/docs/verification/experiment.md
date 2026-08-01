# Verification — Experiment (communication impairment controls)

A new primary page (`operator/pages/Experiment.js`, nav route `experiment`) for injecting
**controlled** communication impairment between the Operator Station and Scout during thesis
experiments: latency, jitter, packet loss, bandwidth limit, packet duplication, packet
reordering, and full disconnect — applied in a chosen **direction** (asymmetric links are
first-class) for a bounded **duration**.

## Scope boundary (why this page has no authority gate)

The impairment manipulates the **Operator↔Scout communications link**, not Pixhawk command
authority. It is an experiment control, not a vehicle command, so it is deliberately
**independent of OPERATOR/LOCAL_AGENT control authority** — `validateExperiment(form)` takes
only the form, and the page never imports `lib/authority.js` / `lib/home.js` or reads
`hasControl`. (Pinned by `tests/experiment.test.mjs`.)

## What is real vs. a backend gap

- **Real (frontend, done):** the whole page — validated form, safe defaults, Full Disconnect
  confirmation, request-payload normalization, the confirmed-state rendering, and the
  session-local action log. All logic lives in `operator/lib/experiment.js` and is unit-tested
  without a DOM.
- **Real (backend, done — Stage 1):** `GET/POST/DELETE /api/experiment/network` are now
  implemented in `main.py` as a thin proxy to Scout's experiment controller
  (`{VEHICLE_API_BASE[vid]}/agent/experiment/network`), the same configurable per-vehicle map
  `control_authority` / `pixhawk_mission` use. The browser never runs `tc`/firewall/shell — it
  posts a structured profile and the backend forwards a normalized request to Scout, returning
  ONLY Scout-confirmed state. See "Backend orchestration (Stage 1)" below. Pinned by
  `tests/test_experiment_network.py`.
- **Still a gap (Stage 2+):** `operator_to_scout`, `both`, `bandwidth_kbit_s`,
  `duplication_pct`, `reordering_pct` and `full_disconnect` are not yet implemented on Scout.
  The Operator backend validates capabilities and rejects them with a clear **400**
  (`{ ok:false, error:"unsupported experiment profile", unsupported:[…], supported_stage:1 }`) —
  never a generic 500.

## Backend orchestration (Stage 1)

Implemented in `main.py` (search `Network-impairment experiment (Stage 1)`).

- **Vehicle → vehicle-local URL resolution.** `vehicle_api_base(vid)` → `VEHICLE_API_BASE[vid]`,
  looked up by **canonical id**, so `3` / `"3"` / `"usv-3"` / `"USV-3"` / `"SAR-001"` all resolve
  to one entry. For Scout (vehicle **2**) that is `http://10.0.2.10:8080` and for SAR-001
  (vehicle **3**) `http://10.0.3.10:8080`, so requests go to `{base}/agent/experiment/network`.
  No address is hard-coded in the frontend.
  `GET`/`DELETE` accept an explicit `?vehicle_id=` in any of those spellings; the **no-id**
  `GET`/`DELETE` the browser issues resolve to the **last-targeted** vehicle (a `POST` or
  `DELETE` sets it; before any of those, the first configured route — Scout). A `vehicle_id`
  that IS supplied but names no vehicle is answered `available:false` and **never** falls back
  to the default: with two routable USVs, silently redirecting would impair the wrong vehicle.
- **Known limitation.** `Experiment.js` sends `vehicle_id` on `POST` (from its fleet dropdown)
  but not on `GET`/`DELETE`, so polling and **Stop** follow the last-targeted vehicle rather
  than the dropdown. Harmless while only one experiment runs at a time, but pressing Stop
  before ever applying targets the default vehicle — pass `?vehicle_id=` explicitly if that
  matters. Not changed here (this pass adds routing, and impairment commands were not exercised).
- **experiment_id is backend-owned.** Every accepted `POST` generates a fresh UUID and injects
  it into the forwarded payload; the browser neither sends nor owns one.
- **Never optimistic.** `active` in every response comes from Scout's own confirmed flag. A
  `POST` that Scout has not confirmed active returns `status:"inactive"` (still carrying the
  generated `experiment_id`). GET polling is the source of truth.
- **Forwarded shape** (vehicle_id dropped, direction normalized, experiment_id added):
  `{ experiment_id, latency_ms, jitter_ms, packet_loss_pct, bandwidth_kbit_s, duplication_pct,
  reordering_pct, full_disconnect, direction:"scout_to_operator", duration_s }`.

### Public response schema (stable, every endpoint)

```
{ status, active, experiment_id, vehicle_id, started_at, ends_at,
  remaining_s, direction, profile, error, available }
```

- `GET` success → `status:"inactive"|"active"` (200).
- `GET`/`DELETE` when Scout is unreachable → `status:"unavailable", active:false,
  available:false, error:"Scout experiment controller unreachable"` with a **503** (a non-500,
  handled status). 503 is deliberate: the existing frontend treats a failed experiment GET as
  its honest **Unavailable** signal, so this needs no frontend change and never fabricates an
  inactive/active state it cannot confirm.
- `POST` whose apply ack is lost/delayed (a `scout_to_operator` impairment can do exactly this)
  → **502** with `available:false` and a "poll for confirmed state" message — recorded as
  `apply_failed`, **not** declared a failed experiment. GET polling then reveals the truth.
- Vehicle with no Scout base configured → `POST`/`DELETE` **409**, `GET` **200** with
  `available:false` (all stable, never 500).

### Timeout policy (bounded, latency-aware)

`timeout=(connect, read)` on every Scout call. `connect = 3.0 s` (fixed). `read` accounts for
the requested/active Scout→Operator delay so a legitimately-delayed latency experiment is not
misclassified as a failure: `read = clamp(5.0 + 2·(latency_ms+jitter_ms)/1000, 5.0 … 20.0 s)`.
The `20.0 s` cap is a firm upper bound — a pathological latency can never hang the endpoint.
`GET`/`DELETE` size `read` from the **known active profile** they are tracking, because that
same impairment delays their responses too.

### Experiment history

In-memory, append-only (resets on restart) — the **same persistence pattern** as `event_log` /
`commands` / comms history, not a new database. `_record_experiment_history(...)` writes one
record per action and mirrors it into the operator event log (`etype:"experiment"`), so the
Events page shows experiment activity. Actions recorded: `requested`, `confirmed_active`,
`rejected` (unsupported / invalid_range), `apply_failed` (Scout unreachable), `stopped_manually`,
`expired_automatically`. Queryable at `GET /api/experiment/network/history`.

### Authority boundary

The experiment endpoints are **independent of control authority and of the comm-state command
gate** — there is no `OPERATOR`/`LOCAL_AGENT` check anywhere in the block and no `confirm:true`
requirement. It is infrastructure-level experiment control on the Operator↔Scout link, not a
Pixhawk vehicle command. Pinned by `TestAuthorityIndependence`.

## Honesty rules pinned here

1. **Never optimistic.** The ACTIVE badge is driven **only** by the backend's confirmed
   `active === true`. A `status:"active"` without the flag is not rendered active
   (`experimentStatus`).
2. **Three distinct value sets, never conflated:** *Configured* (what you edit), *Requested*
   (the last payload sent, shown while applying), *Confirmed active* (what the backend reports
   is in effect).
3. **Full Disconnect is an experiment control, not an emergency vehicle command** — a neutral
   panel that tints amber only when armed, requires an explicit confirmation, and dims (keeps
   visible) the netem fields it supersedes because it uses a firewall block, not `tc netem`.
4. **Never applied on load**; **Reset** restores safe defaults without applying.

## Proposed API contract (frontend service functions in `services/api.js`)

- `GET /api/experiment/network` → `{ status, active, experiment_id, started_at, ends_at,
  remaining_s, direction, profile, error }` (stable schema, never 500).
- `POST /api/experiment/network` (apply) — body = `normalizePayload(form, {vehicleId})`:
  `{ vehicle_id, latency_ms, jitter_ms, packet_loss_pct, bandwidth_kbit_s|null,
  duplication_pct, reordering_pct, full_disconnect, direction, duration_s }`.
- `DELETE /api/experiment/network` (stop) — remove the active impairment immediately.
- Direction: `operator_to_scout` · `scout_to_operator` · `both`.
- Backend mechanism (future controller): `tc netem` for delay/jitter/loss/rate/duplication/
  reordering; firewall DROP for `full_disconnect`.

## Frontend safety limits (validation)

| Field | Range |
|---|---|
| Latency | 0–10,000 ms |
| Jitter | 0–5,000 ms |
| Packet loss | 0–100 % |
| Bandwidth | positive kbit/s, or blank = unlimited |
| Duration | 1–3,600 s |
| Duplication | 0–100 % |
| Reordering | 0–100 % |

Negatives and out-of-range values are rejected inline; Apply is disabled while the form is
invalid; Full Disconnect requires confirmation.

## Sidebar change (bundled with this page)

The left navigation pillar and its icons were widened ~50% (`--rail-w` 52→78 px, `--nav-size`
40→60 px, new `--nav-icon` 30 px), all from centralized tokens in `variables.css`. The grid
content offset, the ribbon brand cell, hover/active/tooltip states and the active-page marker
all track the tokens automatically. Pinned by `tests/experiment.test.mjs`.

## Manual checklist

- [ ] Experiment appears in the left rail (flask icon) between Agent-cluster and Configuration; route `#/experiment` loads.
- [ ] Left pillar is visibly wider with larger icons; hover tooltips clear the buttons and don't overlap content; active marker + selected state intact.
- [ ] Enter a negative/over-range value → inline error, Apply disabled.
- [ ] Toggle Full Disconnect → latency/jitter/loss/bandwidth dim + disable; Duration/Direction stay; Apply asks for confirmation.
- [ ] Direction dropdown offers Operator → Scout / Scout → Operator / Both.
- [ ] Apply with the backend absent → honest "Unavailable"/failure; no fake ACTIVE; action recorded in the session log.
- [ ] Reset restores defaults without applying.
- [ ] Tests: `npm test` (see `tests/experiment.test.mjs`).
