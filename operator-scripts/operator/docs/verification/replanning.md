# Replanning integration (Scout Local Agent `/agent/replan/*`, port 8090)

The Operator Station integrated with Scout's first-phase safe-return replanning lifecycle.
The Operator side is a **thin, honest proxy**: Scout owns every decision, the FSM, mission
revisions, safety checks and vehicle actions. The Operator constructs the approved planning
package, issues explicit supervisory operations, and presents Scout's status verbatim.

## Architecture

| Concern | Where | Notes |
|---|---|---|
| Scout replan HTTP client | `scout_replan.py` | per-op functions, bounded timeouts, three-state outcome model (accepted / rejected / **unknown**), 404→unsupported |
| Planning-package construction | `replan_package.py` | builds Scout's package from the immutable revision-0 mission record + Home; route bytes = Pixhawk route (hash-invariant); segment labels from real generation stages |
| Proxy routes + readiness + op log | `main.py` | `/api/vehicles/{id}/replan/*` + `/api/replan/operations`; `LOCAL_AGENT_API_BASE` (8090) separate from Flask 8080 |
| Frontend client | `operator/services/api.js` | per-vehicle `getReplan*/putReplan*/…` |
| Supervisory logic (pure) | `operator/lib/replan.js` | FSM classification, staged-exec mapping, real-exec safety interlock, status normalization, map model |
| Supervisory UI | `operator/pages/Agent.js` | readiness, decision, transaction, mission revision, config, experiment, transitions |

## Scout routes consumed (all on 8090)

`GET/PUT/DELETE /agent/replan/planning_package` · `GET/PUT/DELETE /agent/replan/experiment` ·
`GET/PATCH /agent/replan/config` · `GET /agent/replan/status` · `POST /agent/replan/reset`.

## The outcome model (load-bearing)

A supervisory **write** resolves to exactly one of:
- **accepted** (2xx) — Scout stored/applied it.
- **rejected** (definite 4xx, incl. 409 `TRANSACTION_ACTIVE`) — Scout refused; error code preserved.
- **unknown** (timeout / dropped connection / ambiguous 5xx) — no verdict reached us. The write
  MAY have landed (Scout's stores are idempotent), so it is **never** called a failure; a later
  GET reconciles (compare mission id / hash). Surfaced as HTTP 202.

Reads that fail are `unavailable` (503); routes an older Scout 404s are `supported:false` (200).

## Readiness rules (`GET /api/vehicles/{id}/replan/readiness`)

`MISSION READY` = Pixhawk mission verified **and** valid Home.
`REPLANNING READY` = MISSION READY **and** package stored+usable **and** `PLANNING_PACKAGE_CONSISTENT`
**and** mission id match **and** navigable boundary supplied **and** no hash mismatch.
Limitations (absent boundary, connector not proven safe, shoreline scalar-only, hash comparison
unavailable) are reported **separately** — a package failure is never hidden behind a Pixhawk upload.

## Safety sequencing (real execution)

`autonomous_execution_enabled` and `dry_run` are **independent** Scout flags; the UI presents them
as a ladder DISABLED → DRY-RUN → REAL via `stagePatch()`. `realExecutionBlockers()` forbids the
dangerous sequence *active forced-return injection → dry-run → real execution*: real execution is
blocked while an injection is active, a transaction is active, the package is inconsistent, Home is
invalid, or authority is not understood. Enabling real execution requires explicit `window.confirm`.
RTL fallback is never auto-enabled.

## Isolation & reconnect

- Every route resolves the SELECTED vehicle's 8090 base by canonical id; a write to `usv-2` never
  reaches `usv-3` (test: `test_target_isolation_write_hits_only_selected_base`).
- The Agent page clears replan panels on vehicle switch and discards fetches whose vehicle changed.
- Polls are **reads only**; writes fire only from explicit operator clicks — reconnect/poll can
  never resend a package/config/injection. An `unknown` write is reconciled by the next GET.
- Experiment injection forces `target_vehicle` to the path vehicle server-side.

## Tests

- Backend: `tests/test_replan_integration.py` — **31 tests** (package construction, labels,
  hash-invariance, proxy routes, timeout→unknown + GET reconciliation, 409, error-code
  preservation, isolation, older-Scout compat, readiness). Full backend suite: **513 pass**.
- Frontend: `tests/replan.test.mjs` — **18 tests** (FSM classification, staged-exec mapping,
  real-exec interlock, status normalization, map model, injection payload, api.js wiring).
  Full frontend suite: **391 pass**.

## Remaining frontend increment (documented, not yet wired)

- `operator/pages/Map.js` Leaflet rendering of `replanMapModel()` layers (original/active/revised/
  connector + geometry badges). The **model is built and tested** (`replanMapModel`); only the
  Leaflet draw calls in the 1410-line Map page remain.
- Standalone Config / Experiment *pages* — the controls are live on the Agent page today; promoting
  them to dedicated pages is cosmetic (extend-over-create).
