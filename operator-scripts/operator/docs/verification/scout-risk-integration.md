# Verification — Scout continuous risk & feasibility integration

Operator-side integration pass against Scout's **frozen** instantaneous risk model. No Scout code
was changed. No hysteresis, no autonomous decision policy, no command behaviour change.

Scout's pipeline, and the one field the Operator may display as *the* level:

```
stabilized evidence
    → hard mission/RTL feasibility  +  continuous component risk
    → weighted continuous score                      risk.weighted_score / weighted_level
    + non-compensatory component severity floors     risk.component_floor_*
    + hard feasibility override                      risk.hard_constraint_violated / hard_override_level
    → GOVERNING risk level                           risk.level          ← authoritative
    → advisory recommendation                        risk.recommendation
```

---

## 1. Data-flow audit (state before this pass)

| Scout source | Port | Reached the Operator? |
|---|---|---|
| `GET /agent/mission_execution/status` | 8090 | yes — proxied verbatim under `scout` |
| `GET /agent/state` (`evidence`) | 8080 | **no — never called** |
| `GET /agent/home_status` | 8080 | partially, via the pushed `payload.agent.home_status` |

Findings:

1. **`risk` — 13 of 18 fields dropped** in `normalizeStatus()`. Kept: `level`, `score`,
   `components`, `reason`. Dropped: `weighted_score`, `weighted_level`, `component_floor_level`,
   `component_floor_reason`, `component_floor_source`, `hard_constraint_violated`,
   `hard_override_level`, `confidence`, `recommendation`, `evaluated_at`, `dominant_component`,
   `dominant_reason`, `weights`, `feasibility_status`.
2. **Governing level was already correct** — `riskView()` read `risk.level` only, never the
   score. Untested and unexplained, but not defective.
3. **`riskView().detail` dumped the whole nested `components` object** into the Map tooltip
   (~2 KB on live Scout).
4. **`energy_feasibility.message`** (Scout's own sentence) was dropped.
5. **`agent.home_status.verification_recovery`** was dropped by `home_block()` — Scout pushes it,
   nothing displayed it.
6. **The stabilized `evidence` block was unreachable.** It is not on the pushed status packet
   (`telemetry / power / failsafe / imu / freshness / mavlink / communication / health / mission /
   agent / service_status / measurements`), only on `GET /agent/state`.
7. `summarize_status()` dropped `risk` and `energy_feasibility` entirely.
8. **No fail-open defects found.** Absent risk → `—`; absent feasibility → `—`/`UNKNOWN`;
   `homeStatus()` read `verified` alone. Readiness came from `start_eligible` / `can_start` /
   `start_block_reason`, never from risk. Polling 3 s (Map) / 2 s (Agent).

---

## 2. Changes

**Backend**
- `main.home_block()` — `verification_recovery` passed through verbatim. Provenance only; it can
  never promote an unverified Home.
- `main.read_agent_evidence()` + `GET /api/vehicles/{id}/agent/evidence` — read-only proxy of
  `GET /agent/state` (port 8080). Passes `evidence` / `freshness` / `state_timestamp` through and
  computes no age and no state.
- `scout_mission_execution.summarize_status()` — carries `risk` and `energy_feasibility`
  verbatim, plus `risk_level` (Scout's `risk.level`, never `weighted_level`),
  `risk_recommendation`, `energy_mission_feasible`, `energy_rtl_return_feasible`.

**Frontend logic**
- `lib/mission-execution.js` — full `risk` normalization; `riskView()` gains the explanation
  fields and `governedBy` (hard / floor / weighted, *reported* by comparison, not recomputed);
  new `recommendationView()` and `riskComponents()`; the tooltip is a line again;
  `energy.message` carried.
- `lib/evidence.js` — **new**. Scout's evidence records, no TTL, no clock, no age comparison.
- `lib/home.js` — `recoveryState` / `recoveredAfterRestart` / `recoveryReason` /
  `recoveryCheckedAt`, read *after* `verified` has already decided the state.

**Pages**
- **Map** — third compact row `Advice` (only when Scout sends a recommendation). Text, not a
  button. `Energy` and `Risk` unchanged in form.
- **Agent** — risk section rewritten for full explainability + component breakdown; energy split
  into `MISSION COMPLETION` / `RTL RETURN` / battery provenance; new *Observation evidence
  (Scout)* card; `renderDetail()` guarded against a fetch landing after unmount.
- **Vehicle (Health)** — `recovered after restart` beside the Home-verification chip.

---

## 3. Live verification (Scout `10.0.2.10`, read-only — no vehicle command issued)

Run against a throwaway backend on `127.0.0.1:8211` so the operator's own station was not
restarted; live packets were relayed into it from the running station's `GET /agent/status`.

```
RISK block verbatim match  : True (17 keys)
ENERGY block verbatim match: True (26 keys)
summary.risk_level         : LOW      (scout risk.level LOW · weighted_level LOW · floor None)
summary.risk_recommendation: CONTINUE
mission_feasible True  margin 16.25      rtl_feasible True  margin 77.86
EVIDENCE supported True   all signals FRESH
```

Rendered:

| Surface | Displayed |
|---|---|
| Map card | `Energy FEASIBLE +16%` · `Risk LOW` · `Advice CONTINUE` |
| Agent · risk | governing `LOW` *from the weighted continuous score*, weighted 0.1374 / `LOW`, floor `none active`, hard constraint `NO`, dominant `energy` / `ENERGY_MARGIN_TIGHTENING`, confidence `HIGH`, evaluated `4.2 s ago` |
| Agent · components | `ENERGY 0.46 · 0.3 · 0.138 ENERGY_MARGIN_TIGHTENING` + Scout's evidence, then communication / navigation / health / mission |
| Agent · energy | `MISSION COMPLETION` +16.1%, `RTL RETURN` +77.8%, `Planned Mission Home PLANNING_PACKAGE`, `Verified RTL Home PIXHAWK_VERIFIED_HOME` |
| Agent · evidence | all `FRESH`, Scout's own ages (0.30 s battery / 0.76 s heartbeat) and sources (`SYS_STATUS`, `GPS_RAW_INT`, `HEARTBEAT`, `EKF_STATUS_REPORT`, `GLOBAL_POSITION_INT`) |
| Health | `Home verification  VERIFIED · recovered after restart` |

Screenshots: `img/` is not updated by this pass; the captures above were taken from the live
throwaway instance during verification.

---

## 4. Tests

| Suite | Result |
|---|---|
| `npm test` (node:test) | **901 pass / 0 fail** |
| `python -m unittest` (28 modules) | **971 pass / 0 fail** |

New: `tests/agent-risk.test.mjs` (18), `tests/agent-feasibility.test.mjs` (13),
`tests/evidence.test.mjs` (12), `tests/test_scout_assessment_passthrough.py` (16).

The load-bearing regression, from Scout's own worked example:

```
score 0.2375 · weighted_level LOW · component_floor_level HIGH
(COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION) · level HIGH   →  displays HIGH
```

Two existing assertions were updated where this pass deliberately changed the surface:
`map-inspector` raised its single-function slice bound (the card gained contract commentary), and
`mission-energy` now expects `Direct-return distance` plus the two distinct Home labels while
still forbidding raw coordinates on that card.

---

## 5. What was NOT done

- No hysteresis (explicitly out of scope).
- No autonomous decision or action policy.
- No command-behaviour change: Start / Stop / Pause / Resume / Take Control / Release Control /
  mode commands / Set Home / mission upload are untouched. The recommendation issues nothing.
- No Scout-side change.
- No commit.
