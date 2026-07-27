# Mission default-visibility + single toggle + armed/LOITER upload gating

Branch `feature/operator-plan-route-quality`. Three narrow changes. Frontend-only except one
additive backend passthrough (`upload_context`). No mission-contract-v1 change; Scout stays the
safety authority.

## Added / changed
- `operator/lib/mission-visibility.js` (new) — pure per-USV policy: `missionShowable()`,
  `nextVisibility()`, `toggleVisibility()`, `toggleButton()`.
- `operator/lib/upload-policy.js` (new) — pure `uploadEligibility()` (armed + confirmed-LOITER).
- `operator/pages/Map.js` — per-USV `{ shown, userHidden }` visibility; auto-show a newly-valid
  mission after select / auto-fetch / manual Fetch / successful upload/clear; single stateful
  Show/Hide toggle (replaces the two buttons) with icon + `aria-pressed`; Center gated to a
  showable mission.
- `operator/pages/Plan.js` — Upload gated through `uploadEligibility()`; inline eligibility hint;
  `doUpload` guards on it (never auto-commands LOITER).
- `operator/lib/planning.js` — `finalizePayload()` adds `upload_context: "OPERATOR_REPLACEMENT"`.
- `main.py` `finalize_mission` — threads a string `upload_context` into command params verbatim
  (forwarded to Scout by `agent_command_view`); additive, older Scouts ignore it.
- `operator/styles/theme.css` — `.pxm-toggle`, `.pl-upload-hint`.

## Mission visibility state model (per USV, never global)
`pxm[id] = { …, shown, userHidden }`. `shown` = overlay drawn; `userHidden` = operator explicitly
hid THIS USV. Both live in the per-Map-instance `pxm` map, keyed by USV id.

## Default-visibility rules
- First valid load (select / auto-fetch / manual Fetch): visible by default.
- Invalid / partial / unreachable-without-cache / empty: never shown as valid; toggle disabled.
- Explicit hide → `userHidden=true`; persists across unchanged periodic reads and USV switches.
- Geometry change (successful upload/replan/clear): clears `userHidden` → newly loaded mission
  shows again; empty-after-clear removes the overlay and disables the toggle.
- USV switch / return: each USV's own `{ shown, userHidden }` is restored (`nextVisibility` with
  `geometryChanged:false`); on Map re-mount the mission re-fetches and auto-shows.
- Comm lost: last-known overlay retained + marked stale (unreachable read is `geometryChanged:false,
  progressChanged:false` → no redraw, no clear).

## Toggle states (derived from actual state, not last label)
| loading | showable | shown | label | disabled | aria-pressed |
|---|---|---|---|---|---|
| true | – | s | Loading mission… | yes | s |
| false | false | – | No mission | yes | false |
| false | true | false | Show mission | no | false |
| false | true | true | Hide mission | no | true |

## Operator upload-gating policy (Scout remains authoritative)
Order: another mission op pending → block · disconnected → block · authority required & missing →
block · **armed===true** → require fresh mode: unknown/stale mode → "Waiting for fresh vehicle
mode"; non-LOITER → "Armed upload requires confirmed LOITER (mode is X)"; LOITER → **allowed**
(warn), plus a soft groundspeed hint when high · **armed===false** → allowed · **armed unknown
(field absent)** → allowed (Scout enforces). Upload button enabled iff `P.canUpload(model) &&
eligibility.allowed`. Upload never issues LOITER.

## Tests (all green)
- `tests/mission-visibility.test.mjs` — showable, default-visibility rules, toggle flip/button
  states, per-USV independence, + static check that no separate Show/Hide buttons remain.
- `tests/upload-policy.test.mjs` — disarmed / armed-LOITER / armed-AUTO/MANUAL/RTL/GUIDED /
  unknown-stale mode / disconnected / missing-authority / pending / unknown-armed; + static
  checks (no auto-LOITER, upload_context present).
- `tests/test_planning.py` — `upload_context` threads into command params (and absent when not
  supplied); verification fields untouched.
- Baselines: frontend `npm test` → **310 pass**; backend `python -m unittest` → **348 pass**.

## Manual verification
1. `/app#/map`, select a USV with a valid mission → overlay appears automatically; button reads
   "Hide mission" (`aria-pressed=true`).
2. Click it → overlay hides, button "Show mission"; a periodic auto-fetch does NOT re-show it.
3. Switch to another USV and back → each USV's own shown/hidden is restored.
4. Plan → Finish & Upload a new route → returning to Map, the new geometry shows by default again.
5. Clear the mission → overlay removed, toggle disabled "No mission".
6. `/app#/plan`, select an ARMED vehicle in AUTO → Upload disabled, hint "Armed upload requires
   confirmed LOITER (mode is AUTO)". Press LOITER on Map, wait for confirmed LOITER → Upload
   enables with "Upload allowed while armed: USV is holding position in LOITER". Disarmed → allowed.

## Remaining Scout dependency
The armed-LOITER upload *allowance* depends on the Scout-side change that permits MISSION_UPLOAD
when armed + freshly-confirmed LOITER + stationary within its safety threshold. The Operator UI
now matches that policy for early feedback but does not enforce it — Scout performs the final
authoritative stationary/groundspeed check and may still reject (surfaced via the upload error).
`upload_context: "OPERATOR_REPLACEMENT"` is forwarded but optional; older Scouts ignore it.
