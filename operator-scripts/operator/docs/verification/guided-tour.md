# Guided tour — operator introduction (2026-07-28)

A six-step spotlight tour that introduces the Operator Station: **Welcome → Sidebar →
USV status check → Mission planning → Following the mission on the map → Return to
home and pickup.** It is a reading aid only — the tour never issues a command, never
changes a selection, and never writes to the backend.

## Entry point
A **Guide** button in the **ribbon** (`.rib-help`, in its own `rib-seg` between the
search field and the fleet summary). It lives there rather than in the nav rail because
the rail's vertical budget belongs to the frozen `NAV` list — a rail button forced the
page to be usable only at ~90% zoom. It carries `data-tour-open` rather than
`data-route`, so the router in `app.js` ignores it and no phantom `#/help` page exists.
Click or Enter/Space opens the tour at step 1. Every page renders `Ribbon()`, so the
button is present everywhere.

The tour also **auto-opens once**, 700 ms after first load, and is then remembered in
`localStorage` under `operator.tour.v1` (its own key, so resetting operator preferences
on the Configuration page does not re-trigger it). Any close — Done, Skip, ✕, Esc —
marks it seen. Storage unavailable (private mode) → treated as *seen*, so a station that
cannot remember the answer never re-opens the tour on every reload; the **?** button
still works.

## How the spotlight works
The overlay is appended to `<body>`, outside `#app` — every page rebuilds `#app`'s
innerHTML, which would otherwise wipe it.

The dim is **four rects around the cut-out**, not one sheet over it. Everything outside
is dimmed and click-blocked; the highlighted element stays fully interactive, so the
operator can actually try the control a step describes. Clicking the dimmed area is a
no-op — dismissal is deliberate (Skip / ✕ / Esc), so a stray click never loses your place.

Each step lists **target selectors in priority order**; the first one actually laid out
wins. A step whose targets are all absent degrades to a centred card with no cut-out
rather than pointing at nothing. Geometry is re-measured every frame and repainted only
when the rect actually changed, so the spotlight tracks map pans, resizes and dock
re-renders.

| Step | Route | Spotlight | Popup |
|---|---|---|---|
| 1 Welcome | map | — | centred |
| 2 Sidebar | map | `.rail` | right |
| 3 USV status | map | `#inspector` → `.dock` | left (flips: the inspector is on the right edge) |
| 4 Mission planning | plan | `.dock` → `.map-wrap` | right |
| 5 Follow on the map | map | `#legend` → `.map-wrap` | right |
| 6 Return & pickup | map | `#inspector` → `.map-wrap` | left |

Navigation: **Next / Back** in the popup's bottom row, the six progress dots (click to
jump), **← / →** arrow keys, **Esc** to close. Steps that live on another page set the
hash and wait for their target to appear (2.5 s cap) before anchoring; a stale reply
from an abandoned step is dropped by a route token, so fast clicking cannot leave the
popup pointing at the previous page.

If the operator navigates the rail mid-tour, the tour does **not** fight them: it
re-anchors on the new page and falls back to a centred card if the target isn't there.

## Content rule
The copy states only what the station really does, and repeats the invariants that
matter operationally rather than describing a friendlier system than exists:
- Values are **reported**, never simulated; absent ones read `—` / NO TELEM.
- Not CONNECTED → mode and arming read **UNKNOWN** and commands lock.
- Authority counts only once the vehicle has **confirmed** it — a pressed button is not
  control.
- Planning home is route geometry; it is **not** the Pixhawk HOME_POSITION / RTL point.
- Upload is gated on disarmed or **confirmed LOITER** (the AUTO → LOITER → Upload flow).
- RTL is **queued**, not applied, until the local agent reports back.
- **LOITER** is the active anti-drift hold for pickup; **HOLD** is passive and drifts.

## Verified
`tests/tour.test.mjs` (13 tests, part of `npm test` — 334 pass): step model integrity,
every step's `route` resolving to a real page in `NAV`, target lists being non-empty
selector arrays, steps carrying no side-effect hook, the placement policy (prefers the
requested side, flips when it doesn't fit, centres inside an over-large target, always
clamps inside the viewport margins, survives a viewport narrower than the popup), and
the first-run flag including unusable storage.

DOM behaviour was exercised under jsdom against a stubbed station layout
(1600×900, rail 78 / dock 240 / inspector 344 / ribbon 48) — 41 assertions, all passing:
overlay mounts outside `#app`; step 1 centres with no ring; step 2 rings the rail with
the padding clamped at the viewport edges and anchors the popup right; the four mask
rects tile the viewport exactly, with **no rect overlapping the cut-out** and no undimmed
gap; step 3 flips left of the inspector without overlapping it; step 4 routes to `#/plan`
and re-anchors; step 5 routes back to `#/map` and falls through to `#legend` rather than
spotlighting the whole map pane; Done/Back labels and the step counter; arrow keys; dot
jumps; Esc closes and sets the seen flag; reopening starts at step 1; navigating to a
page without the target degrades to a centred card without throwing.

Backend serving confirmed via `TestClient`: `/app/lib/tour.js`, the updated
`/app/components/NavRail.js`, `/app/lib/ui.js`, `/app/app.js` and `/app/styles/theme.css`
all return 200 with the tour markup, wiring and styles present.

Not yet done: a live-browser pass with screenshots (no browser tooling on this machine).
