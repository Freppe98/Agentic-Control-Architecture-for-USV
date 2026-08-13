# Start readiness — four questions, four answers

## The defect

A healthy, fully prepared mission on the slipway — planning package consistent, route hash
matched, Scout reporting `start_eligible: true` — rendered as:

```
Agent  NOT_READY
RTL Home unavailable
[ Start Mission ]   (disabled)

Home will be set during Start
```

Home had simply not been set yet, **because setting it is a phase of Start**. Three of those
four statements described the step the disabled button was about to perform, and the fourth
contradicted them. A manual Set Home made Start available instantly — which taught the
operator to perform, by hand and outside Scout's guarded transaction, exactly the sequence
Scout owns end to end (authority → LOITER → Set Home → verify → ARM → AUTO).

## The four questions Scout answers

| Question | Scout field | Source block |
|---|---|---|
| May the guarded Start **transaction** be entered? | `start_eligible` (+ `start_block_reason`, `authority_blocks_start`, `execution_ready`) | `agent.mission_execution` |
| Is there a **proven runtime Home** right now? | `verified` | `agent.home_status` |
| Would Scout accept an **AUTO** command this instant? | `ready_for_auto` | `agent.home_status` |
| Would Scout accept an **RTL** command this instant? | `ready_for_rtl` | `agent.home_status` |

A healthy pre-Start mission has the first `true` and the other three `false`. The AUTO and
RTL interlocks close **as a result** of pressing Start, not as a precondition for being
allowed to press it.

## What gates Start

`startGate()` (`operator/lib/mission-execution.js`) and nothing else. Its inputs are stable
lifecycle facts plus Scout's own explicit verdict:

- busy / disconnected / unsupported / status unavailable
- replanning controller owns the vehicle · an active operation · a mid-transaction state
- mission/package replacement conflict · already running · a terminal state needing Rearm
- no active mission
- `home.requiredBeforeStart` — **only** when Scout explicitly declares it will not enter the
  transaction without an already-verified Home
- `startEligibility()` — Scout's `start_eligible`, verbatim, with its own `start_block_reason`

`verified`, `ready_for_auto` and `ready_for_rtl` are **not** inputs. Asserted exhaustively
over all eight combinations in `tests/start-readiness-layers.test.mjs`, plus a source guard
that `startGate`'s body contains none of those identifiers.

Package and hash blockers are untouched: `PLANNING_PACKAGE_MISSING` / `_UNUSABLE` / `_STALE`,
`ROUTE_HASH_STALE`, `POSITION_STALE`, `MISSION_UNAVAILABLE`, `BATTERY_INVALID` and Scout's
binding `STALE_MISMATCH` all still disable Start, in Scout's own words. **Only Home
pre-verification is deferred.**

## What is displayed

`readinessLayers()` (`operator/lib/mission-readiness.js`) — one pure derivation, rendered by
both the Map's Agent Mission card and the Agent page's Home card, so the two surfaces cannot
word the same three facts differently. It reaches no gate.

| | pre-Start (Home unverified) | Home verified | Scout silent | Scout's status stale |
|---|---|---|---|---|
| **Home** | `Not verified` (neutral) | `Verified` (ok) | `Not reported` | `Last known — not confirmed` |
| **AUTO readiness** | `Waiting for Start Home setup` (neutral) | `Ready` (ok) | `Not reported` | `Last known — not confirmed` |
| **RTL readiness** | `Waiting for verified Home` (neutral) | `Available` (ok) | `Not reported` | `Last known — not confirmed` |
| **Start Mission** | **enabled** | enabled | enabled | enabled |

A **verified** Home that Scout still refuses AUTO or RTL against is a genuine gap and reads
as one — `Not ready` / `Unavailable`, warning-toned, with Scout's own sentence
(`home_status.reason`, carried through `homeStatus().scoutReason`) as the tooltip. It still
does not gate Start; Scout owns that verdict and has not withdrawn it.

### Messaging

- `HOME_DURING_START_NOTE` — "**Home will be set and verified during Start**", neutral tone.
- `RTL_AFTER_HOME_NOTE` — "RTL becomes available after Home verification", carried in the
  Home note's tooltip. RTL *becomes* available; it is never announced as unavailable.
- Both are withdrawn once the state is no longer a pre-start resting state
  (`READINESS_CHIP_STATES`). A promise about a step still ahead must not be repeated over a
  Start that has already failed to establish Home — there, Scout's actual failure is the
  whole message.

## Badge semantics

- **READY / NOT_READY** on the Agent Mission card means *ready to **start***, and follows the
  same gate the button does. `READY` beside a disabled Start, or `NOT_READY` beside an
  enabled one, is impossible by construction (one derivation, asserted).
- *Ready for AUTO right now* is a different layer and keeps its own name: Scout's
  `execution_ready`, shown on the Agent page as "Execution ready · READY UNDER LOCAL_AGENT".
- **VEHICLE READY / VEHICLE NOT READY** (`deploymentReadiness`) is a third, already
  separately-labelled layer: properties of the vehicle (Pixhawk, GPS, mission loaded, Home
  verified). It has never gated Start and still does not.

## Polling precedence

One authoritative source for the Start control: the **mission-execution status**
(`mission.status`, polled at 3 s). The fleet payload's `home_status` is read into `hs` and
passed to the *card*, never to the *gate* — so a fleet poll landing after a mission-execution
poll cannot turn `start_eligible: true` into NOT_READY due to Home. The one-shot Start
preflight remains informational and never feeds the gate (see
[mission-execution.md](mission-execution.md)). Source-guarded in
`tests/start-readiness-layers.test.mjs`.

## Unchanged

- Start still calls `POST /api/vehicles/{id}/mission-execution/start` — one endpoint per
  intent. The operator station issues no Set Home, no LOITER, no ARM and no AUTO on Start's
  behalf, and the backend transaction still performs its own fresh, fail-closed proof
  (forcing a live Pixhawk read-back) before any vehicle write.
- Manual Set Home remains an explicit operator tool with its own preconditions.
- Planning-package acceptance/sync semantics, survey planning, and risk/energy semantics.
- `mission_lifecycle.start_eligibility()` (backend) already read the same contract and was
  not changed.

## Tests

`tests/start-readiness-layers.test.mjs` — 17 tests covering: `start_eligible` true with Home
unverified / `ready_for_auto` false / `ready_for_rtl` false all leave Start enabled; package
stale, hash mismatch, position stale and every `start_eligible:false` reason disable it; the
Home-unverified messaging; the verified-Home display; a Start in flight (phase line + both
double-submit guards); a Start that failed during Set Home; manual Set Home preserved and
never automatic; an older/partial Scout failing closed; the exhaustive no-gate proof; the
one-derivation badge invariant; the polling-precedence source guard.

`tests/test_set_home.py::test_reported_flag_separates_scout_silence_from_scout_saying_no` —
the backend half.
