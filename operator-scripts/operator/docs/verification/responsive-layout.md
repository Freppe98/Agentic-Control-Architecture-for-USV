# Responsive layout & clipping audit — whole station (2026-08-04)

Station-wide audit and repair of viewport-height ownership, desktop/laptop scaling,
Leaflet layout and message containment. Target range 1366×768 → 2560×1440, browser zoom
90–110 %. Phone/mobile is explicitly out of scope.

Measured with a Playwright harness (13 routes × 4 viewports) that reports, per page:
document overflow, any element painted outside the viewport with no scrollable ancestor,
content clipped inside an `overflow:hidden` box with no scroller and no ellipsis, pairwise
rect intersection of every map overlay/Leaflet control, and whether every nav-rail item is
inside the rail's box.

**Result: 134 findings → 0.** (Raw baseline output was 862; 728 of those were one
false positive repeated per page — the nav tooltip is absolutely positioned outside its
button, which the first version of the probe read as content overflow. The probe was then
strengthened — it now also ignores content that legitimately sits inside a scroller, and
additionally sweeps every element for silently-clipped text and for spilling past a
clipping ancestor. The final run below uses that stricter probe.)

---

## 1. Root cause

One defect explains almost every reported symptom.

`.app` was `grid-template-rows: var(--ribbon-h) 1fr`. A bare `1fr` track is
`minmax(auto, 1fr)` — it keeps a **min-content floor**. The tallest child of row 2 is the
navigation rail: 13 fixed items × 60 px + gaps + separator = **886 px**, on every screen.
So row 2 was 886 px tall regardless of the viewport, the shell measured 934 px, and
`html,body { overflow:hidden }` silently discarded everything below the fold.

At 1920×1080 the body row has 1032 px available, so 886 px fits and the station looked
correct — which is exactly why it "appeared designed around one specific resolution".
At 1440×900 and 1366×768 it does not fit, and the discarded region was:

| Reported symptom | Actual cause |
|---|---|
| Top bar overlaps/clips content | shell 934 px in a 768 px viewport; every page's bottom 166 px unreachable |
| Bottom task bars overlap maps/panels | `.plan-actionbar` was `position:absolute` inside a 886 px `.map-wrap` → drawn at y≈890, off-screen |
| Legend clipped at the bottom | `.legend` anchored `bottom:14px` of the same over-tall box, and `z 500` under the action bar's `z 700` |
| Terminal / Messages unreachable | last two `.nav` items at y 858 and 923 |

Independent secondary causes found by the same sweep:

2. **Leaflet zoom collided with the status banner.** Both defaulted to the map's
   top-left corner — a measured 30×38 px overlap on Plan at *every* viewport, including
   1920 and 2560. The zoom buttons sat directly on the text that reports drawing
   instructions and every generation/validation error.
3. **A scrolling flex column compresses its children.** `flex-shrink` defaults to `1`, so
   `.cfg` (a flex column with `overflow-y:auto`) squeezed each `.cfg-card` to fit and the
   card's own `overflow:hidden` ate the remainder — instead of scrolling. Configuration
   rendered 900 px of content into a 377 px box: **two of four comm-threshold rows and two
   of three registry vehicles were simply not on the page**, with no scrollbar to hint at
   it. Same latent defect on `.mission-body`, `.veh-list`, `.legend-body`.
4. **No resize handling.** Map.js had no `invalidateSize` at all; Plan.js had one
   `setTimeout(…, 60)` after first paint. Nothing covered window resize, panel changes,
   route re-entry or tab visibility.
5. **Card grids keyed off the viewport, not the container.** `.subgrid`, `.sitgrid`,
   `.diag-grid` used `@media (max-width: …)` while living inside `.content-main`, which is
   ~660 px narrower than the viewport — so the breakpoints measured the wrong box.
6. **Threshold-timeline tick labels never rendered.** `.tl-tick`/`.tl-tn` sat inside
   `.tl-track { overflow:hidden }`; the 8s/15s/30s numbers were clipped away entirely.
7. **Messages truncated rather than wrapped.** `.pl-upload-hint` was
   `white-space:nowrap; text-overflow:ellipsis` — upload eligibility is deliberately
   text-carried rather than colour-only, and it was being ellipsised away.
8. **Leaflet's own resize raced page teardown.** `trackResize` (default on) calls
   `invalidateSize({debounceMoveend:true})`, parking a 200 ms timer that fires `moveend` on
   a map the hash router may already have removed → uncaught
   `TypeError: … reading '_leaflet_pos'` in Plan's viewport-persistence handler.

---

## 2. Shared responsive architecture

### Viewport-height ownership

`.app` is the **only** element measured against the viewport.

```
.app                 height:100dvh · grid-template-rows: var(--ribbon-h) minmax(0,1fr) · overflow:hidden
├─ .ribbon           row 1, fixed height
└─ row 2  ──────────  minmax(0,1fr): no min-content floor
   ├─ .rail          shrinks by height band, then scrolls
   ├─ .dock          flex column; roster scrolls, cards below keep their space
   ├─ page cell      .page | .content-main | .map-wrap   (min-height:0, exactly one scroller)
   └─ .inspector     scrolls
```

`.app > * { min-width:0; min-height:0 }` stops the min-content floor reappearing one level
down, and every scrolling region on the chain carries `min-height:0`. `100dvh` (not `100vh`)
so a collapsing browser toolbar cannot clip the shell. `width:100%` replaces `100vw`, which
included the scrollbar gutter.

The map column is itself a two-row grid — this is what removed the class of bug entirely:

```
.map-wrap    grid-template-rows: minmax(0,1fr) auto
├─ .map-stage      the map + its overlays; the map's bottom edge IS the bottom of the map
└─ bottom bar      .plan-actionbar / .mission-progress-bar — a real row, not an overlay
```

A hidden bar collapses its row to 0 and the stage takes the height back. No reserved
offsets, no "legend sits under the action bar".

### Breakpoints

Width bands (three, deliberate):

| Band | Range | Effect |
|---|---|---|
| Compact laptop | ≤ 1450 px | `--rail-w` 64, `--nav-size` 48, `--ribbon-h` 44, `--card-min` 250; ribbon padding tightens |
| Standard desktop | 1451–1919 px | `:root` defaults |
| Large desktop | ≥ 1920 px | clamp ceilings hold; extra width goes to the map/workspace, not to padding |
| (≤ 1200 px) | — | ribbon search hidden, Guide collapses to its icon — neither carries operational state |

Height bands matter more, because the rail is height-bound:

| Band | `--nav-size` | Rail needs | Fits in |
|---|---|---|---|
| default | 60 px | 886 px | ≥ 1080 viewport |
| ≤ 900 px | 46 px | 672 px | 856 px body row |
| ≤ 800 px | 40 px | 581 px | ~590 px body row (768 screen minus chrome) |
| ≤ 680 px | 36 px | 529 px | 110 % zoom on a 768 screen |

Past that the rail scrolls (narrow 4 px thumb) rather than shrinking further — a nav button
below ~36 px stops being a usable desktop target, and unreachable navigation is worse than a
scrollbar.

### Responsive tokens (`variables.css`)

```
--dock-w        clamp(200px, 15.5vw, 240px)     --page-gap    clamp(8px,  .8vw, 16px)
--inspector-w   clamp(276px, 21vw,   344px)     --panel-pad   clamp(11px, 1vw,  20px)
--legend-w      clamp(168px, 13.5vw, 206px)     --map-pad     clamp(8px,  .7vw, 14px)
--card-min      280px (250px compact)           --map-ov-gap  8px
```

At 1366 the two side panels give back 62 px and the rail 14 px: the Map's usable map area
went **704×886 (clipped) → 809×728 (whole)**.

`--map-tl-h`, `--map-tr-h`, `--map-tr-w` are **measured at runtime** and written onto each
`.map-stage`; nothing in the map layout is a hard-coded offset.

### z-index scale

Every `z-index` in `theme.css` is now `var(--z-…)`; a test fails on any literal.

| Token | Value | Why |
|---|---|---|
| `--z-behind` | −1 | decoration behind its own siblings (upload-step connector) |
| `--z-content` | 1 | |
| `--z-sticky-head` | 5 | sticky table headers inside their scroller |
| `--z-page-bar` | 10 | in-flow page action/status bars |
| `--z-marker-hover` | 20 | hovered map marker, local to Leaflet's markerPane |
| `--z-nav-tip` | 60 | nav-rail tooltips |
| `--z-map-overlay` | 1100 | legend, banner, wind, view controls |
| `--z-map-toast` | 1200 | map-local command result |
| `--z-modal` | 2000 | confirmation dialogs |
| `--z-tour` | 3000 | guided tour, deliberately above a dialog |

The jump from 60 to 1100 is not arbitrary: Leaflet hard-codes `.leaflet-control` at 800 and
its corner containers at 1000, and our overlays carry operator state that must not be
painted under a zoom button.

---

## 3. Leaflet strategy — `operator/lib/map-layout.js`

One module, used by both maps. `attachMapLayout(map, stage, { topLeft, topRight })`.

**Size.** `ResizeObserver` on `.map-stage` → `invalidateSize({ pan:false })`, coalesced
through `requestAnimationFrame` (a drag-resize fires the observer dozens of times a second
and each raw call is a full Leaflet re-layout). Every trigger the brief lists resolves to a
change in the stage's box, so one observer covers all of them; `window resize`,
`orientationchange`, `fullscreenchange` and `visibilitychange` are wired as belt-and-braces
for the cases an observer sees late or not at all (a backgrounded tab has a zero-size
stage). `pan:false` keeps the operator's view on the vehicle they were watching. Cleanup
cancels the pending frame and disconnects every observer and listener.

`trackResize:false` on both maps: `map-layout.js` is the single owner of resize, and
Leaflet's own listener is the one that parks the dangling `moveend` timer (cause 8).

**Corner ownership.**

| Corner | Owner |
|---|---|
| top-left | drawing / status instructions (`.plan-banner`) |
| top-right | view controls (`.wind`, `.plan-viewctl`), **zoom beneath them** |
| bottom-left | legend |
| bottom-right | scale bar + attribution |

`cornerExtent()` measures what each corner already holds and publishes it as
`--map-tl-h` / `--map-tr-h` / `--map-tr-w`. `theme.css` then derives:

- `.leaflet-top.leaflet-right { padding-top: calc(var(--map-tr-h) + var(--map-ov-gap)) }`
- `.legend { max-height: calc(100% - var(--map-pad)*2 - var(--map-tl-h) - var(--map-ov-gap)) }`, body scrolls
- `.plan-banner { max-width: calc(100% - …  - var(--map-tr-w) - var(--map-ov-gap)) }`
- `.toast { top: calc(var(--map-pad) + max(var(--map-tl-h), var(--map-tr-h)) + …) }`

A hidden overlay contributes zero height *and no gap*, so a conditional banner collapses
cleanly instead of leaving a phantom offset (unit-tested).

Attribution was `attributionControl:false` on both maps; it is now on, in an uncontested
corner, and wraps rather than ellipsising — OSM tiles require it.

---

## 4. Files changed

| File | Change |
|---|---|
| `operator/styles/variables.css` | responsive layout tokens, map-corner measurement vars, z-index scale, 3 width + 3 height bands |
| `operator/styles/theme.css` | shell grid, rail scroll, map-stage/bottom-bar rows, Leaflet control + legend + toast containment, flex-shrink guard, auto-fit card grids, message wrapping, form/table/modal containment |
| `operator/lib/map-layout.js` | **new** — shared resize + corner-measurement contract |
| `operator/pages/Map.js` | `.map-stage` wrapper, zoom→top-right, scale + attribution, `attachMapLayout`, `trackResize:false`, cleanup |
| `operator/pages/Plan.js` | same, plus per-vehicle upload errors surfaced in a bounded list and a guarded `moveend` |
| `operator/app.js` | nav tooltip positioned as `position:fixed`, viewport-clamped (the rail is a scroll container now) |
| `operator/components/ThresholdTimeline.js` | ticks moved out of the clipped track |
| `operator/components/VehicleDock.js`, `pages/Fleet.js`, `pages/Vehicle.js` | `title` on names that now truncate |
| `tests/layout-shell.test.mjs` | **new** — 13 structural CSS invariants |
| `tests/map-layout.test.mjs` | **new** — 7 tests for corner arithmetic + frame coalescing |

---

## 5. Results

### Viewport matrix — 13 routes each

| Viewport | Before | After | Map area (Map / Plan) |
|---|---|---|---|
| 1366×768 | 72 findings | **0** | 809×728 / 809×674 |
| 1440×900 | 58 findings | **0** | 850×856 / 850×802 |
| 1920×1080 | 2 findings | **0** | 1258×1032 / 1258×978 |
| 2560×1440 | 2 findings | **0** | 1898×1392 / 1898×1338 |

The 1920/2560 findings are the Plan page's zoom-vs-banner and legend-vs-action-bar
overlaps, which were resolution-independent. Note the two columns are not measured with an
identical probe: the "after" column uses the stricter version described above, so the
comparison is conservative.

Routes covered: map, fleet, plan, mission, agent, video, pilot, vehicle, events, experiment,
config, terminal, messages. (Video and Messages are migration stubs; their shell is
verified, their content is not yet built.)

### Browser zoom — all pass, no overlaps, rail fully visible

| Screen | 90 % | 100 % | 110 % |
|---|---|---|---|
| 1366×768 | 900×809 map | 809×728 | 708×658 |
| 1920×1080 | 1471×1152 | 1258×1032 | 1083×934 |

### Leaflet resize

| Event | Stage | Leaflet | Synced |
|---|---|---|---|
| initial @1920×1080 | 1258×1032 | 1258×1032 | ✓ |
| window resize → 1366×768 | 809×728 | 809×728 | ✓ |
| bottom bar shown | 809×728 | 809×728 | ✓ |
| navigate away and back | 809×728 | 809×728 | ✓ |
| Plan single ⇄ fleet mode switch | 809×674 | 809×674 | ✓ |

### Stress cases

- **Long backend error** (240-char GEOS exception with a file path) — banner wraps to
  568×127 inside the stage with no horizontal overflow; toast wraps and clears both the
  banner and the view controls; action-bar hint wraps to two lines inside the bar; legend
  still clear of the action bar. Screenshot: `img/responsive-plan-long-error-1366x768.png`.
- **14 vehicles, 60-char names** on fleet / map / plan / vehicle at 1366×768 — document
  stays 1366×768, nothing escapes the viewport, names truncate with `title`, authority
  segments wrap so `LOCAL AGENT` is never cut to `LOCAL AGE`.
- **Modal with 14 paragraphs @1366×768** — dialog bottom 754 ≤ 768, footer buttons visible,
  body scrolls.
- **Console:** no page errors on any route or scenario.

### Tests

`npm test` → **429 pass / 0 fail** (409 existing + 20 new).
`python -m unittest discover -s tests` → **540 pass, OK**.

### Screenshots

`img/responsive-{map,plan,fleet,config}-{1366x768,1920x1080}.png`, plus
`img/responsive-before-{map,plan}-1366x768.png` for the before/after.

---

## 6. Remaining limitations

- **Video and Messages** are still migration stubs. The shell is verified at every viewport;
  their page-specific layout cannot be audited until the pages exist. Video in particular
  (aspect-ratio preservation, `object-fit`, no large empty space on a large desktop) is
  unverified because there is no video element yet.
- **Pilot embeds a third-party page.** The station sizes and clips the iframe correctly; the
  responsiveness of the vehicle-local dashboard *inside* it is Scout's, not ours.
- **Below ~680 px of viewport height** (e.g. 125 %+ zoom on a 768 px screen) the nav rail
  scrolls instead of shrinking further. Deliberate: a smaller button stops being a usable
  desktop target.
- **Very narrow windows (< ~1100 px)** are outside the stated target range. The shell holds
  and nothing is clipped, but the three-column pages get cramped; no work was done to
  optimise below 1366.
- Screenshots are headless Chromium at DPR 1. Font rendering on a real Windows display at
  fractional DPI scaling will differ slightly; the layout arithmetic does not.
- The audit harness lives in the session scratchpad, not in the repo — the durable
  regressions it found are pinned by `tests/layout-shell.test.mjs` instead.
