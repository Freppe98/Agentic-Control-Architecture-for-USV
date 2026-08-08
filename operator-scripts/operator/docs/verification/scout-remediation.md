# Operator-side pass after the Scout live-bench remediation

Scout is the authoritative source for mission-execution and replanning lifecycle semantics. Every
rule below is CONSUMED from Scout's status, never re-derived here.

Backend `python -m unittest discover -s tests` → **839 pass, OK**.
Frontend `npm test` → **693 pass / 0 fail**.
`runtime_data/mission_store.json` sha256 before and after both suites: **unchanged**.

---

## 1. Mission-store contamination — root cause and fix

**Symptom.** `runtime_data/mission_store.json` held exactly one mission, `msn-restart`, with
`active_original_by_vehicle["2"] = "msn-restart"`, while Scout and the Pixhawk were running a
different mission. The station answered *"Agent package does not match approved mission"* and
package synchronization refused. That refusal was **correct** — it was the correct answer to a
corrupted question.

**Root cause.** `tests/test_mission_publish.py::test_package_sync_required_survives_a_backend_restart`
isolated the store by monkeypatching `main._save_mission_store` rather than the store **path**. It
restored the real writer and then ran a publish, so the real atomic write ran against the test's
own cleared in-memory store and replaced the production snapshot with one seeded fixture record.

**Why it survived review.** A full `unittest discover` run does *not* reproduce it: discovery
imports every module before running any test, and `tests/test_planning.py` redirects
`main.MISSION_STORE_PATH` at import time. The production file was protected only by module import
**order**. Running one module alone — exactly what the per-feature verification docs instruct —
removed that protection. Reproduced on 2026-08-07: `python -m unittest tests.test_mission_publish`
rewrote the file; the full suite did not.

**Fix.** Isolation that depends on import order is not isolation, so the runtime directory is now
resolved **once, in the module that owns the store** (`main._resolve_runtime_dir`):

1. `OPERATOR_RUNTIME_DIR` — explicit override (deployment, or a test choosing its own directory);
2. a test runner in the process (`unittest`/`pytest` in `sys.modules`) → a per-process temp
   directory, logged loudly. No test can opt back in; a **new** test file inherits the guarantee;
3. otherwise the real `runtime_data/`.

`tests/test_mission_publish.py` was also fixed at the source: that test now points the path at a
directory it owns and reads back what actually landed **on disk**.

The production file was left as found — it is stale test residue and clearing it is a fresh
Plan → Upload, not a hand edit.

**Regression test** — `tests/test_mission_store_isolation.py`:
no test process resolves the production store; the real writer, the real upload-status projection
and a real publish transaction all leave it byte-for-byte unchanged; and — in subprocesses — the
**complete suite** and the **single module that caused the incident** both leave it unchanged.

## 2. Normal upload → Pixhawk verify → package sync

The full transaction already existed (`mission_publish.py`, driven by `Plan.js`); the manual
`curl .../planning-package/sync` was needed because the *store* was wrong, not because the chain
was missing. One real gap was found and closed: `doPublish()` fired **once**, so a transaction
that answered "not finished" (queued upload, incomplete read, Scout unreachable, another publish
holding the vehicle) stranded the operator on a progress line, and the only way out was the
manual sync.

`nextPublishAttempt()` (`lib/mission-publish.js`) now carries the same transaction to a verdict:
bounded re-invocation while the transaction itself says it is unfinished, stopping the moment it
is decided. Re-invoking is safe — the publish route issues no vehicle command and writes Scout's
idempotent package slot. The manual sync endpoint is unchanged and remains a recovery tool.

## 3. Start eligibility

`can_start` alone is no longer the input. Scout's explicit contract is consumed:
`start_eligible` / `execution_ready` / `authority_blocks_start` / `start_block_reason`.

| Scout says | Operator does |
| --- | --- |
| `execution_ready = true` | Start offered; already under LOCAL_AGENT |
| `start_eligible = true`, `authority_blocks_start = true` | **Start offered**, labelled *"Start will take Local Agent control"* |
| `start_eligible = false` | Start withheld, with **Scout's own** `start_block_reason` |
| contract absent (older Scout) | previous `can_start` reading, unchanged |

The Operator still owns the transaction: OPERATOR → acquire and verify LOCAL_AGENT → Scout Start.
`AUTHORITY_NOT_LOCAL_AGENT` is no longer presented as a broken/unprepared mission. Every evidence
gate is unchanged (record VERIFIED, read-back hash, package stored/usable/consistent, replanning
readiness), and three hard guards still fail closed on a self-contradictory status.

## 4. `home_corridor`

Derived **only** from geometry the operator already approved — the transit/connector segments the
generator produced and validated against the navigable area and the no-go zones — buffered
±6 m (`planning.HOME_CORRIDOR_HALF_WIDTH_M`), simplified, then checked. Checks run on the
**simplified** ring, so what ships is what was proven.

Emitted only when the ring contains the planning Home, overlaps the navigable area, clears every
no-go zone, is a single contiguous polygon and has ≥3 distinct vertices; wire order `[lon, lat]`,
implicitly closed. Otherwise the key is **omitted** — not `null`, not `[]` — so Scout fails closed
in LOITER. It is never widened, re-anchored or invented to reach a runtime Home.
`shoreline_clearance_m` remains scalar metadata and is stated as such.

## 5–8. Replacement lifecycle, completion, map refresh, trigger latch

- **binding / package_conflict**: `STALE_MISMATCH`, `STALE_PACKAGE_DURING_ACTIVE_EXECUTION` and
  `OPERATION_IN_PROGRESS` block a new mission and surface *"New mission uploaded while another
  mission is active…"* — outranking the generic "already running". No Stop is invented.
- **COMPLETED_HOLD**: chip `COMPLETED`, headline *Mission finished*, second line *Final LOITER
  verified*, next action *Rearm / prepare next mission*. Reaching the last waypoint is still not
  completion; `COMPLETED_HOLD` without the verified LOITER states the gap.
- **Map refresh**: `missionRevisionSignal()` is no longer dormant — it is fed by the
  mission-execution status the Map already polls (`active_route_hash`, replan FSM) plus the replan
  status' revised hash / revision / VERIFIED readback. Unchanged evidence ⇒ no download; no
  evidence ⇒ dormant. Manual Refresh unaffected; no re-centering.
- **Trigger latch**: `triggerLatch()` / `cooldownView()` separate "trigger active" from "another
  attempt is coming". A consumed generation renders *Attempt N consumed · Outcome: X · Re-arm
  required*, and the cooldown is explicitly labelled **not** a pending retry.

## 9–11. Agent diagnostics

- `[object Object]` root cause: `clean()` used `String(v)` on structured Scout values. It now uses
  `asText`; the raw interpolations (`${t.lastError}`, policy flags, decision reasons, mission id,
  decision headline) all go through `asText`/`esc`. Guarded by `tests/agent-diagnostics.test.mjs`.
- **State separation**: the page now labels **Supervisory decision engine** / **Mission execution
  lifecycle** / **Replanning lifecycle**. Current Situation carries both *Mission execution
  (Scout)* and *Vehicle mission state (telemetry)*. When the supervisory engine claims no mission
  while mission execution is live, a contradiction note names which subsystem answers the question.
- **Battery**: `battery_valid:false` / raw `-1` renders *"Battery telemetry temporarily
  unavailable"*, never `0%`. Scout's energy policy is untouched.

## 12. Store durability

Added: a crash before the atomic replace leaves the previous snapshot intact; a new mission never
overwrites a historical immutable record; every loaded active pointer resolves to a loaded record
for that vehicle; an owed package sync survives a restart; a hand-edited route is refused, not
repaired.

## 14. Safety semantics preserved

Operator owns supervisory authority initially · Take Control returns it · LOITER stays exempt · no
automatic disarm · no unsupported Stop · failed safe-return stays LOITER/SUSPENDED · no implicit
resumption after a failed replan · no hash/mission bypass.

---

## Live retest checklist

1. **Upload** — Plan → Finish & upload. Expect, with **no manual sync**:
   `Mission uploaded and Agent package synchronized`. Check the backend log shows
   `[MISSION STORE] saved … active: usv-2=msn-… (VERIFIED, package SYNCED)`.
2. **Confirm the store** — `GET /api/diagnostics` → `active_missions` names the mission you just
   uploaded (not `msn-restart`, not a fixture id).
3. **Start** — Map → Agent Mission. With authority OPERATOR the card should read *Start will take
   Local Agent control* and Start must be **enabled**. Press it; watch the phases
   Checking → Taking agent control → Holding position → Setting and verifying Home → Starting AUTO.
4. **Mid-mission `force_safe_return`** — Agent page → apply injection. Expect the replan FSM to
   leave MONITORING and the mission card to show *Agent is replanning*.
5. **LOITER** — confirm Scout holds; the Operator issues no competing mode command.
6. **Home-corridor return plan** — the package sent at upload must carry `home_corridor`
   (check the Plan page's publish evidence / `package_sent`). If it was absent, expect Scout to
   fail closed in LOITER — that is correct, and the plan is what needs revisiting.
7. **Revised mission on the Map** — the return route must appear **without** pressing Refresh,
   once `active_route_hash` changes / readback is VERIFIED. Confirm the view is not re-centered.
8. **AUTO return** — mission card shows `AUTO · WP n / m`.
9. **Home arrival** — arrival persistence bar fills; *not* completion yet.
10. **Final LOITER** — `final_loiter_verified` true.
11. **COMPLETED_HOLD** — chip `COMPLETED`, *Mission finished*, *Final LOITER verified*, and
    *Rearm / prepare next mission* offered. Pause/Resume must be gone.
12. **Trigger latch** — with the trigger still active, the Agent page must read
    *Attempt N consumed · Re-arm required for another attempt*, and the cooldown must say it is
    **not** a pending retry. Verify no second automatic attempt occurs.
13. **Replacement conflict** — upload a new mission while the previous run is active; expect
    *"New mission uploaded while another mission is active…"* and Start withheld.
14. **Diagnostics sweep** — no `[object Object]` anywhere on the Agent page; battery with a `-1`
    reads *temporarily unavailable*.
