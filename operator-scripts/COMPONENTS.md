# COMPONENTS.md

Blueprint for the operator-station frontend. Vanilla JS ES modules, no framework. **Components are pure functions that return HTML strings** (mirroring the frozen wireframes); pages compose them, set `innerHTML`, then wire events via delegation. This keeps one implementation of each visual — no `BatteryBar2` / `BatteryBarFinal`.

Rule: when a page needs a repeated visual, extract it into `components/` immediately. The codebase should get *simpler* per migration, not larger.

Status: `built` = implemented & in use · `planned` = spec'd here, build when the first consuming page is migrated.

## Foundation (not components)
| File | Purpose |
|---|---|
| `styles/variables.css` | Frozen design tokens: color, spacing, radii, shadow, type scale, motion timings. The only place literal values live. |
| `styles/theme.css` | Reset + base typography + the shared component classes (ribbon, rail, pill, dock, bar, inspector…). |
| `lib/ui.js` | Shared helpers/constants: `COL`, `cls()`, `SHORT`, `battColor()`, `fmtAge()`, `commState()`, `NAV`, `ICON`, `el()`. |
| `services/api.js` | The **only** module that talks to FastAPI. Pages call `api.getFleet()`, never `fetch()`. |
| `app.js` | Hash router; mounts the active page into `#app`. |

## Components
| Component | File | Status | Props | States / notes | Used by |
|---|---|---|---|---|---|
| `Ribbon(data)` | `components/Ribbon.js` | built | `{missionLabel, missionStatus, counts:{c,p,d}, alertCount, clock}` | Renders mission scope selector, status pill, search, fleet summary, bell, mission clock. Page updates `#rib-clock`, counts by id. | all pages |
| `NavRail(active)` | `components/NavRail.js` | built | `active` = page key | Icon nav in frozen order with divider before dev tools; `.active` + tooltip; click → router. | all pages |
| `CommsPill(state,{full})` | `components/CommsPill.js` | built | comms state string | `full` → dot + word (`commpill`); else short `CONN/PART/DISC/UNK`. The **comms axis** — never used for health. | Map, Fleet, Vehicle, Video, Pilot, Autonomy |
| `StatusDot(state)` | `lib/ui.js` (`statusDot`) | built | comms state | 9px comms-colored dot. | roster, matrix |
| `BatteryBar(pct)` | `components/BatteryBar.js` | built | `pct` (0-100 or null) | Threshold color (`<20` red, `<40` amber). **Stays vivid even when telemetry stale.** `null` → `—`. | Map, Fleet, Vehicle, Video, Pilot |
| `Bar(pct,color)` | `lib/ui.js` (`bar`) | built | generic progress | coverage, disk, sector, confidence. | Mission, Vehicle, Autonomy |
| `HealthBadge(sev,condition)` | `components/HealthBadge.js` | planned | `sev` OK/CAUTION/WARN, `condition` | Round severity dot + **named condition** ("Battery low"), never "Caution" alone. The **health axis**. | Fleet, Vehicle |
| `LastContact(age_s)` | `components/LastContact.js` | planned | seconds | Live-ticking "Last contact Xs"; warn styling past thresholds. | Map inspector, Fleet, Vehicle |
| `TelemetryGrid(vehicle,{stale})` | `components/TelemetryGrid.js` | planned | vehicle | 2-col mono grid (speed, heading, lat/lng, mode…). Dims when `stale`; battery stays vivid. | Map, Vehicle |
| `VehicleDock(list,sel,onSelect)` | `components/VehicleDock.js` | planned | vehicles, selected id | Left roster; activity verb primary, lane/last-contact sub, battery. Assigned/Depot groups. | Map, Vehicle, Autonomy, Pilot |
| `Table(cols,rows,{sort})` | `components/Table.js` | planned | columns, rows | Sortable dense table; sticky header, severity/selection rows. | Fleet, Vehicle matrix, Events |
| `Timeline(segments,{hover})` | `components/Timeline.js` | planned | comms-state bands | 60-min comms history (canvas). **Needs backend comms-state log — NO TELEM until then.** | Map inspector, Video |
| `DecisionTrace(trace)` | `components/DecisionTrace.js` | planned | state-machine nodes | Vertical trace; comms nodes = comms colors, behavior nodes = accent, future = dashed. **NO TELEM until agent emits it.** | Autonomy |
| `ConstraintList(constraints)` | `components/ConstraintList.js` | planned | met/unmet inputs | ✓/✗ decision inputs. **NO TELEM.** | Autonomy |
| `CoverageDonut(pct)` | `components/CoverageDonut.js` | planned | 0-100 | Canvas donut. | Mission |
| `QuickActions(vehicle)` | `components/QuickActions.js` | planned | vehicle | Return Home / Pause / Resume / Loiter; gated by comms; "queues until next contact" when partitioned. Mock until command API exists. | Map, Mission |
| `CommandChip(state)` | `components/CommandChip.js` | planned | Pending/Executing/Finished/Failed | Command lifecycle. **Needs command API.** | Mission, Map, Autonomy |
| `CameraViewport(vehicle,cam)` | `components/CameraViewport.js` | planned | vehicle | Canvas placeholder + honest frame state (LIVE/FROZEN/NO SIGNAL). | Video, Pilot |
| `BrowserFrame(url,reach)` | `components/BrowserFrame.js` | planned | onboard URL, reachability | Address bar + reach pill + Open; embeds onboard dashboard when reachable. | Pilot |
| `EventList(events,{filters})` | `components/EventList.js` | planned | events | Severity-tinted rows, ack workflow, filters. | Events |
| `WindWidget(env)` | `components/WindWidget.js` | built (in Map) | environment | Map overlay; rotates arrow; "No data" when absent. | Map |
| `Select(opts)` | `components/form/Select.js` | built | `{id,label,value,options,hint?,disabled?}` | Labeled dropdown; page wires `change` on `[data-pref]`. | Configuration |
| `Toggle(opts)` | `components/form/Toggle.js` | built | `{id,label,value,hint?,disabled?}` | Labeled on/off switch; `role=switch`, flips via `[data-pref]`. | Configuration |
| `ThresholdTimeline(t)` | `components/ThresholdTimeline.js` | built | `{stale,partitioned,disconnected}` s | Read-only comms-timing bands; proportional so it stays correct if constants change. **Comms axis.** | Configuration |
| `NumberInput` | `components/form/NumberInput.js` | planned | — | Editable numeric config control — build when a writable config endpoint exists (thresholds are read-only today). | Configuration |
| `NotifPanel(events)` | `components/NotifPanel.js` | planned | unack events | Bell dropdown; ack removes from bell, stays in Events. | ribbon (all) |
| `SearchPalette()` | `components/SearchPalette.js` | planned | — | Ctrl-K palette → entities/pages/concepts. | ribbon (all) |

## NO-TELEM slots (design intent present, backend not yet)
Render the slot with the `no-telem` treatment; **never invent values**. Tracked in `DATA_DICTIONARY.md`:
comms `Timeline` (needs comms-state transition log) · all `Autonomy` reasoning fields · `rssi`/`latency` · Vehicle `Temperatures`, pack voltage/current, compass cal · named **mission scope** & mission ETA/remaining · command lifecycle.
