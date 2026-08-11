# E2 experiment evidence — Map reference geometry + Agent preflight

The E2 water experiment has to be **visually indisputable**: the USV flies an approved mission
around one no-go polygon, its battery is driven critically low on the far side, Scout replans a
constrained safe return with its `RETRACE_APPROVED` strategy, and the vehicle returns **around the
same polygon** rather than straight through it.

The evidence is fragile in one specific way. Once Scout uploads the revision, the flight controller
carries **only** the return route — the outbound route and the obstacle are gone from the vehicle.
A map that shows only the live Pixhawk mission at that moment cannot answer the question the
experiment exists to answer.

## What changed

**Map** now draws an **approved-plan reference layer** from the operator's own immutable original
mission record (`GET /api/vehicles/{id}/missions/active-original`), which no replan can overwrite:

| Layer | Source | Appearance |
|---|---|---|
| No-go zones | `planning_inputs.no_go_zones` | red fill + outline (same red as the Plan page) |
| Original route | `route_waypoints` | subdued grey, finely dashed |
| Navigable area | `planning_inputs.navigable_boundary` | faint dashed blue outline |
| Active / revised route | Pixhawk read-back (`getPixhawkMission`) | the existing prominent blue/green mission line |
| Vehicle Home | `v.home` (Scout's `home_status`) | unchanged |
| Vehicle position | fleet feed | unchanged |

The reference layer lives in its own Leaflet layer group and its own panes (z 392 / 394 / 396, all
**below** the default overlay pane at 400), so the live route always draws over the approved plan
and `clearMissionOverlay()` cannot take the obstacle with it when the active mission is replaced.

**Legend** gained four entries: Active mission on the vehicle · Original approved mission
(reference) · No-go zone (original plan) · Navigable area (original plan).

**Map replan line** — one short phrase from Scout's own replanning FSM
(`Safe return — validating`, `Replanned safe return active`, …). It says **safe return**, never
"RTL". `MONITORING` shows nothing.

**Agent** gained two cards at the top of the replanning section:

* **E2 experiment preflight** — 12 tri-state checks (mission execution, mission id, route identity,
  planning package, Home, feasibility, risk, advice, action request, replan FSM, no-go count on the
  approved record, no-go count in Scout's package).
* **Risk · Advice · Action request · FSM** — the four independent layers, one row each.

The **Mission revision** card is now **Safe return mission revision** and carries the full read-only
evidence set: strategy, revision number, original/revised hash, original/preserved/removed/inserted/
revised counts, validation / upload / readback outcomes, fallback state, last error.

## Manual verification (2026-08-10)

Backend on `127.0.0.1:8231` against the real mission store; Scout's replan/mission-execution
responses stubbed at the network layer to a mid-safe-return state (`fsm_state: VALIDATING`,
`strategy: RETRACE_APPROVED`, risk `CRITICAL`, advice `RETURN_HOME`, action request
`REQUEST_RETURN_HOME`). Headless Chromium; **zero console errors** on both pages.

**Map, usv-2 selected**

```
PXM ROWS: Loaded 34 waypoints + Home (seq 0) · Current WP 1 / 34 · Last download 3s ago
          Mission id d5d60a87 · Home Not verified · Approved plan 34 wp · 0 no-go zones
E2 PANES: leaflet-e2-navigable-pane 392 · leaflet-e2-route-pane 394 · leaflet-e2-nogo-pane 396
REF LAYERS DRAWN: { nogo: 0, route: 1, nav: 1 }
REPLAN BANNER: "Safe return — validating"  (class "ov replan caution")
LEGEND: … Active mission on the vehicle · Original approved mission (reference) ·
        No-go zone (original plan) · Navigable area (original plan)
```

`nogo: 0` is correct and is the experiment blocker below: the currently approved mission
`msn-0d729359230f` has `planning_inputs.no_go_zones: []`.

**Agent, usv-2 selected**

```
E2 PREFLIGHT:
  Mission execution         FAIL  RUNNING            (E2 starts from READY)
  Mission id                PASS  msn-e2
  Route identity verified   PASS  hash match · upload VERIFIED
  Planning package          FAIL  RECONCILING
  Home                      FAIL  not verified
  Mission feasibility       FAIL  NOT FEASIBLE
  Risk                      FAIL  CRITICAL
  Advice                    FAIL  RETURN_HOME
  Action request            FAIL  REQUEST_RETURN_HOME
  Replanning FSM            FAIL  VALIDATING
  No-go zones (expected 1)  FAIL  0                  ← the real blocker
  No-go zones in Scout's package  FAIL  1            ← Scout disagrees with the record

FOUR LAYERS: CRITICAL / RETURN_HOME / REQUEST_RETURN_HOME / VALIDATING
SAFE RETURN MISSION REVISION: RETRACE_APPROVED · rev 1 · orig… → rev… · 14 → 6 wp ·
  preserved 5 · VALID / PENDING / —
```

Every FAIL above is the stubbed mid-replan state being read correctly against an E2 *pre-start*
checklist — that is the card working, not a defect.

## Automated coverage

`tests/e2-evidence.test.mjs` (30 tests) — labelled A–G:

* **A** one no-go zone on the record reaches the map model, converted `[lng,lat]` → `[lat,lng]`;
  tolerant polygon spellings; a degenerate ring is dropped but still counted.
* **B** before a replan the original reference and the live active route coexist as distinct layers.
* **C** a changed `active_route_hash` makes the tracker fetch (and an unchanged one does not);
  after the revised route installs, the original route and the no-go polygon both remain;
  `clearMissionOverlay` provably cannot remove the reference layer; the panes stay below 400.
* **D** a record with a route but no no-go field yields **no** zones and a **null** count — an
  explicit `[]` is a different, reported answer; the Map never builds a ring by hand.
* **E** a missing/junk record degrades quietly: the active mission still draws, nothing is invented.
* **F** the four layers come from four fields; `HOLD_REQUESTED` does not rewrite Advice as `HOLD`;
  a silent Scout is reported as silent, never as `NONE`.
* **G** only `FALLBACK_RTL` uses the word RTL; the revision card states the distinction explicitly.

Plus the preflight's own behaviour (a correct E2 passes everything; **present-but-zero** zones
fail; two zones fail; a Scout package that disagrees with the record is flagged; an unanswered
check is `UNKNOWN`, never a pass; an absent plan is not re-normalized into existence).

`tests/test_replan_integration.py` — `no_go_zone_count` / `no_go_zones_present` carried through
`_normalize_scout_package` and out on `readiness.planning_package` (from the summary, falling back
to the package's own list; zero reported as zero; a Scout that reports none stays `null`, never a
fabricated 0), and `ActiveOriginalE2GeometryTests` pinning the active-original endpoint's geometry
contract against the real captured record.

## Remaining E2 blockers

1. **The approved mission has no no-go zone.** `msn-0d729359230f` (active for usv-2) carries
   `planning_inputs.no_go_zones: []` and `metrics.no_go_zone_count: 0`. Plan and upload a new
   mission with **exactly one** no-go polygon between the survey area and Home before E2, then
   confirm the preflight's *No-go zones (expected 1)* row reads **PASS 1**.
2. **`action_request` is not on any Scout contract.** See `BACKEND_ROADMAP.md` — the row reads
   "Not emitted by this Scout build" and is deliberately not rendered as `NONE`.
