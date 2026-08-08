# Startup / reconnect mission reconciliation

Backend `python -m unittest discover -s tests` → **940 pass, OK**.
Frontend `npm test` → **780 pass / 0 fail**.
`runtime_data/mission_store.json` sha256 before and after everything below:
`eb3042d30bcac14339a5cb38c56e413a720077dc2f1118da6aad7ea5f97faac4` — **unchanged**.

All live evidence captured against Scout (usv-2, `10.0.2.10`) on 2026-08-08.

---

## 1. The symptom, and the exact code path that produced it

**Shown:** *"Agent package does not match the approved mission"* —
`operator/lib/mission-publish.js` `READINESS_TEXT.REAL_MISMATCH`, rendered by
`readinessLabel()` from the Map's Agent-mission card and the Agent page's readiness card.

The old condition, and the two backend fields it read:

```
idDisagrees  = planning_package.mission_id != null && planning_package.mission_id_match === false
hashDisagrees= planning_package.hash_mismatch === true
idDisagrees || hashDisagrees  ->  REAL_MISMATCH
```

Both come from `main._compute_replan_readiness`:

```python
mission_id      = active_original_by_vehicle.get(vid)      # the RESTORED active pointer
rec             = original_missions.get(mission_id)
record_hash     = rec["route_hash"]                        # the approved route's content hash
package_hash    = _normalize_scout_package(...)["route_hash"]

mission_id_match = bool(package_mission_id and mission_id and package_mission_id == mission_id)
hash_mismatch    = bool(record_hash and package_hash and record_hash != package_hash)
```

So the warning is a comparison of **Scout's stored planning package** against **whatever record
the restored active pointer happens to name**. Nothing anywhere re-examined that pointer.

## 2. Root cause

**The Operator had no startup/reconnect reconciliation at all.** `lifespan` (`main.py:29`)
restores `original_missions` + `active_original_by_vehicle` and starts the comms monitor; that
is the whole of startup. The restored pointer was then treated as ground truth forever, and the
only thing in the station that could move it was `_new_mission_record()` — i.e. Plan → Finish &
upload, which mints a **new mission id** and **rewrites the flight controller's mission** with
content that was in many cases already on it.

Three distinct startup faults follow from that single omission:

| # | Fault | Why re-upload "fixed" it |
| --- | --- | --- |
| 1 | The active pointer names a **superseded** approved record while another approved record in the same store carries the exact route the flight controller reports. | A new record becomes active and its package is pushed. |
| 2 | A record is stranded at `upload_status: QUEUED`. `mission_id_by_command` and the command queue are **in-memory** (`main.py:1281`, `main.py:2657`), so a MISSION_UPLOAD result that arrives after a restart is an *orphaned historical result* and `_sync_mission_record_status` never runs. Publish then answers `PIXHAWK_UPLOAD_PENDING` forever. | A fresh command lives inside one process, so it verifies. |
| 3 | `package_sync_state` is persisted and blindly restored, so a stale `REQUIRED` survived even when Scout demonstrably held the matching package. | The successful publish rewrote it to `SYNCED`. |

Two further conflations were confirmed in the same pass:

- **Record identity was treated as content identity.** `mission_id_match === false` alone raised
  `REAL_MISMATCH` even with `hash_match === true` — i.e. with the package route, the approved
  route and the route on the flight controller proven byte-identical.
- **A mismatch could be declared with no Pixhawk evidence at all.** `hash_mismatch` needs only
  the record and the package, so an unreachable flight controller plus a stale package rendered
  as a proven disagreement.

## 3. Three-way state — before (live reproduction, isolated runtime dir, real vehicle)

The store's active pointer at `msn-restart`, with `msn-7e6538a61dff` sitting in the same store:

| Field | Operator persisted | Pixhawk observed | Agent package / binding |
| --- | --- | --- | --- |
| mission_id | `msn-restart` | — (carries none) | `msn-7e6538a61dff` |
| route hash | `sha256:21e7f7d4…60d990` | `sha256:ef169694…a548d8` | `sha256:ef169694…a548d8` |
| route wp count | 14 | 14 (15 Pixhawk items) | 14 |
| upload_status | `VERIFIED` | `mission_valid: true` | binding `UNBOUND` |
| package_sync_state | `REQUIRED` (`SCOUT_PACKAGE_POST_FAILED`) | `partial: false` | `stored: true` |

`mission_id_match=False  hash_match=False  hash_mismatch=True  readback_hash_match=False`
→ **REAL_MISMATCH** — *"Agent package does not match the approved mission"*.

The flight controller and the Agent package agreed with each other, and with a persisted,
approved, VERIFIED operator record. Only the pointer disagreed.

## 4. Three-way state — after (same store, same vehicle, with reconciliation)

| Field | Operator persisted | Pixhawk observed | Agent package / binding |
| --- | --- | --- | --- |
| mission_id | `msn-7e6538a61dff` | — | `msn-7e6538a61dff` |
| route hash | `sha256:ef169694…a548d8` | `sha256:ef169694…a548d8` | `sha256:ef169694…a548d8` |
| upload_status | `VERIFIED` | `mission_valid: true` | binding `UNBOUND` |
| package_sync_state | `SYNCED` | `partial: false` | `stored: true` |

`mission_id_match=True  hash_match=True  hash_mismatch=False  readback_hash_match=True`
→ **READY** — *"Agent package synchronized"*, reached with **no mission upload, no new mission
id, and no write to Scout**.

## 5. Did each suspect contribute?

| Suspect | Contributed? |
| --- | --- |
| Persisted active-mission selection | **Yes — the primary cause.** Restored and never re-examined. |
| Startup timing / stale evidence | **Yes.** `hash_mismatch` needed no Pixhawk evidence, so a mismatch could be declared before (or without) a read-back. |
| `package_sync_state` persistence | **Yes, secondarily.** A restored `REQUIRED` was never recomputed against live Agent evidence. |
| `mission_id` vs content hash conflation | **Yes.** Reproduced: identical route content, different record label → false `REAL_MISMATCH`. |
| Incompatible hash domains | **No.** See §6 — all three sides compare the same value. |
| Verification lost with the command queue | **Yes, as a second startup fault** — different symptom (`mission_ready:false`, publish stuck at `UPLOAD_IN_PROGRESS`), same "only a re-upload clears it" outcome. |

## 6. Hash audit — the compared values are one domain

| Name | Producer | Content hashed | Consumer | Purpose |
| --- | --- | --- | --- | --- |
| `route_hash` (record) | Operator, `mission_contract.route_content_hash` | route only, Home **excluded**; 1-based seq, fixed `MAV_CMD_NAV_WAYPOINT` / `MAV_FRAME_GLOBAL_RELATIVE_ALT`, lat/lon round 7, loiter round 3, alt/param2-4 `0.0`; compact JSON list; SHA-256 | readiness, publish, reconciliation | mission CONTENT identity |
| `route_content_hash` (read-back) | Scout, independently | same canonicalization | `_scout_mission_read` (pass-through only — the operator never recomputes it) | proves what the FC carries |
| `package.route_hash` / `original_route_hash` | Scout's package store, echoing what the Operator sent | same | `_normalize_scout_package` | proves what the Agent holds |
| `full_mission_hash` / `hash` | Scout | route **plus Home** | passed through, **never compared** | different bytes; explicitly excluded |
| `mission_id` | Operator, `msn-<uuid12>` | — | everywhere | lifecycle RECORD identity — *not* content |

Verified live on 2026-08-08: record `ef169694…a548d8` == Pixhawk `route_content_hash` == package
`route_hash` == package `original_route_hash`. Counts: record 14, Scout `route_waypoint_count`
14, `pixhawk_item_count` 15 (Home at seq 0 — the offset is Scout's own statement, applied once in
`mission_publish.route_count_from_readback`).

## 7. The reconciliation algorithm (`mission_reconcile.py`)

Runs inside `_compute_replan_readiness`, on the evidence that path already gathers — so it fires
on the **first fresh mission evidence after startup or reconnect**, never before there is any.

```
records  = approved records for THIS vehicle (ownership re-checked, never trusted)
readback = live-ish Pixhawk read-back;  package = Scout's planning-package evidence

if read-back is unreachable / partial / hash-less / older than 10 s:
        -> RECONCILING.  Decide nothing.  Change nothing.                     (Case F)

matches = records whose route_hash == read-back route_content_hash            (CONTENT, not id)

if not matches:
        -> MISMATCH (an active record exists) | UNAPPROVED_MISSION (none)     (Cases D, E)
           Nothing adopted, nothing approved, nothing made active.

chosen = the active record if it is among matches, else newest by created_at
                                                       (mission_id as final tie-break)

1. chosen is not the active record   -> move the active pointer               (Case B)
2. chosen.upload_status in {QUEUED, ACCEPTED} and its stored hash still
   describes its own waypoints       -> upload_status = VERIFIED
3. package proves id == hash == count-> package_sync_state = SYNCED
   package contradicts it            -> package_sync_state = REQUIRED (+ code)
   package unreachable/incomplete    -> left alone

verdict: SYNCHRONIZED (Case A) | PACKAGE_SYNC_REQUIRED (Case C)
       | RECONCILING with pixhawk_settled:true when only the Agent half is unread
```

The durable store persists **approved mission identity, its hash and its verified upload
status**. The reconciliation verdict itself is in-memory only (`_reconciliation_by_vehicle`) and
is recomputed from fresh evidence every poll — a restored copy of a live comparison would be a
fabricated observation.

## 8. Proof that reconciliation cannot upload a mission

- `mission_reconcile.py` imports exactly `__future__`, `datetime`, `mission_contract` —
  asserted on the **import graph** by
  `test_mission_reconcile.NoVehicleWriteTests.test_the_module_imports_nothing_that_can_upload_or_command`.
  No `scout_replan`, no `requests`, no `main`, no `mission_publish`.
- Its whole outward surface is a four-callable `Deps` (`vehicle_records`, `active_mission_id`,
  `set_active`, `persist`) — asserted exhaustively; there is no vehicle hook to call.
- Every integration test asserts `len(main.commands)` is unchanged (so no MISSION_UPLOAD was
  created) **and** that no non-GET reached the Local Agent.
- Live: after the four reproductions plus the production-store run, the vehicle's
  `route_content_hash` was unchanged and the Local Agent's package envelope was still
  `generation: 6, received_at: 1786177945.418` — the user's own 08:32 upload, untouched.

## 9. Planning-package-only resynchronization

Already existed and is **reused, not duplicated**:
`POST /api/vehicles/{id}/replan/planning-package/sync` →
`mission_publish.run_publish(package_only=True)`, which issues no vehicle command and writes only
Scout's single idempotent package slot. Reconciliation does not call it — writes to Scout stay on
explicit operator routes, never on a poll — it puts the mission into the state where that button
is the offered action (`PACKAGE_SYNC_REQUIRED`, already wired on both Map and Agent).

The coupling that *was* the architectural bug is now broken in the other direction too: before,
a record stranded at `QUEUED` made the package-only retry refuse with `mission_not_verified`, so
even package-only recovery needed a Pixhawk re-upload. Pinned by
`test_reconcile_integration.LostVerificationTests.test_the_package_sync_route_then_works_instead_of_refusing_as_not_uploaded`.

## 10. Evidence audit table

| Evidence / state | Persisted? | Source | Compared against | Freshness required | Used in reconciliation? | Problem found |
| --- | --- | --- | --- | --- | --- | --- |
| Operator `mission_id` | yes | `mission_store.json` | package mission id | n/a (durable) | as a label only | **was** conflated with content |
| Operator approved `route_hash` | yes | record (self-verified on load) | Pixhawk + package hash | n/a | **yes — the key** | none |
| Operator active pointer | yes | `active_original_by_vehicle` | — | n/a | **repaired** | **restored blindly** |
| Operator `upload_status` | yes | command projection | live read-back | n/a | **re-derived** | stranded at `QUEUED` after restart |
| Operator `package_sync_state` | yes | publish transaction | live package evidence | n/a | **recomputed** | stale `REQUIRED` survived restart |
| Pixhawk `route_content_hash` | no | Scout 8080 read-back | approved hash | ≤ 10 s, complete, non-partial | **yes** | mismatch could be declared without it |
| Pixhawk `route_waypoint_count` | no | Scout (explicit) | record route count | same | yes | none |
| Read-back freshness / partial | no | `evidence_age_s`, `partial` | — | — | **gates every verdict** | not previously gated |
| Agent package `mission_id` | no | 8090 package GET | active record id | live | yes | none |
| Agent package `route_hash` | no | 8090 package GET | approved hash | live | yes | none |
| Agent package `route_count` | no | 8090 summary | record route count | live | yes | none |
| Agent `mission_execution.mission_id` / `original_route_hash` / `active_route_hash` | no | 8090 | — | live | no (reported only) | Scout-side; see §12 |
| Agent `binding` / `package_conflict` | no | 8090 | — | live | no (reported only) | Scout-side |

## 11. Live verification (usv-2, real vehicle, isolated runtime dirs)

Reproduced by pointing a backend at a scratch `OPERATOR_RUNTIME_DIR`, restarting it, and polling
without uploading anything.

| Scenario | Before the fix | After the fix |
| --- | --- | --- |
| **A** active pointer at `msn-restart`; the FC + Agent carry `msn-7e6538a61dff`'s route | `REAL_MISMATCH` (hash) | poll 1 `REBOUND_ACTIVE_MISSION msn-restart → msn-7e6538a61dff`, `SYNCHRONIZED`; poll 2 **READY** |
| **B** active record correct but stranded `QUEUED`, `package REQUIRED` | `mission_ready:false`, publish refuses `PIXHAWK_UPLOAD_PENDING` forever | `UPLOAD_STATUS_VERIFIED` + `PACKAGE_SYNC_STATE REQUIRED→SYNCED`; `mission_ready:true` |
| **C** identical canonical route, different `mission_id` | `REAL_MISMATCH` (mission id) | `PACKAGE_SYNC_REQUIRED` — *"same canonical route under a different mission id"*, Synchronize button offered |
| **D** the FC carries a route no approved record has | `REAL_MISMATCH` | `MISMATCH` **retained** (`NO_APPROVED_MATCH`), nothing adopted |
| **production store**, unchanged mission | READY | `SYNCHRONIZED`, **zero store writes**, READY |

Resulting stores: A active `msn-7e6538a61dff` (`msn-restart` preserved untouched); B `VERIFIED`
+ `verified_by: RECONCILED_READBACK`; C still `msn-differentid01`, now `REQUIRED`; D unchanged.
**No new mission id in any of them.**

## 12. Remaining cases that genuinely need a new mission upload

1. The flight controller carries a route **no approved record for that vehicle** matches
   (`UNAPPROVED_MISSION` / `MISMATCH`). Never adopted automatically — approving unseen content is
   the one thing reconciliation must not do.
2. A record whose `upload_status` is **`FAILED`**. A recorded upload failure is a fact about a
   write that did not complete; observing the route on board later does not explain it, so it is
   reported rather than repaired.
3. A record whose stored hash no longer describes its own waypoints (a hand-edited store). The
   load-time validator already refuses the whole snapshot; reconciliation refuses to promote it
   too.

## 13. Known Scout-side gap (not fixed here; Scout is authoritative)

`mission_execution.binding.binding_state` is `UNBOUND` with
`bound_original_mission_id: null` even when `package_mission_id`, `package_route_hash` and
`verified_route_hash` all agree, and `replan.last_error` reads
`ORIGINAL_MISSION_ID_MISMATCH — "no bound original mission identity from mission execution"`.
The 2026-08-08 07:48 capture also shows Scout's own restart recovery refusing with
`reason: MISSION_ID_MISMATCH` while `prior_original_route_hash` **equalled** the package route
hash — the same record-identity/content-identity conflation, on Scout's side of the line. The
Operator now reports this verbatim and does not re-derive it. Fixing it belongs to Scout.

## 14. Safety semantics — unchanged

Start gating is untouched: `run_start` still requires the record `VERIFIED`, the read-back hash
match, `planning_package.consistent` (which still requires `mission_id_match`), Scout's own
replanning readiness, Set-Home verification and the authority transaction. Reconciliation
promotes nothing into readiness that live evidence does not prove, adopts no unapproved content,
never widens a tri-state null to true, and cannot reach the vehicle. `REAL_MISMATCH` still fires
on every genuine route-content disagreement — verified live in scenario D.

## 15. Files changed

| File | Change |
| --- | --- |
| `mission_reconcile.py` | **new** — the pure reconciliation module |
| `main.py` | import; `_reconciliation_by_vehicle`, `_reconcile_deps`, `_reconcile_vehicle_mission`, `reconciliation_for`; Scout reads hoisted above the comparisons in `_compute_replan_readiness` and reconciliation run there; `reconciliation` added to `GET .../replan/readiness` and `GET .../missions/publish` |
| `operator/lib/mission-publish.js` | `RECONCILING` + `UNAPPROVED_MISSION` states; `reconcileOf()`; new precedence — inconclusive reconciliation outranks a mismatch, and an id-only disagreement over a proven-identical route is an owed sync |
| `operator/pages/Map.js` | new states in the card comment + warn tone |
| `operator/pages/Agent.js` | new states in `verdictTone` |
| `tests/test_mission_reconcile.py` | **new** — 29 unit tests |
| `tests/test_reconcile_integration.py` | **new** — 20 end-to-end tests through the real routes |
| `tests/mission-publish.test.mjs` | 9 new readiness-vocabulary tests; the id-disagreement test split into "content unproven" (mismatch) and "content proven" (rebind) |
| `tests/test_replan_integration.py` | `test_not_ready_when_pixhawk_not_verified` made specific (the read-back must not prove the route), plus its companion re-verification test |
