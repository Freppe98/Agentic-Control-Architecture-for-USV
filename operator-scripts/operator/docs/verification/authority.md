# Control authority & command panel (Map + Vehicle pages)

Take Control / Release Control plus the reverse-path command buttons live on **both**
the Map inspector (primary operational view) and the Vehicle Control card. Both drive
the same shared authority state machine (`operator/lib/authority.js`) and the same
command queue.

**Revision 2026-07-12 — corrected authority semantics.** An earlier version mapped the
buttons the wrong way round (Take Control → `LOCAL_AGENT`, `hasControl` keyed off
`LOCAL_AGENT`). The effective-authority axis is now:

| Effective authority | Meaning | Requested by |
|---|---|---|
| `OPERATOR` | operator holds the wheel; operator commands may execute | **Take Control** |
| `LOCAL_AGENT` | the autonomous local agent holds the wheel (mission runs) | **Release Control** |
| `RC` | RC transmitter override physically active | *reported only* — not requestable |
| `null` | unknown / unreachable / no source configured | — |

Scout Flask remains the **sole source of truth** for the authority value — the operator
backend holds none of its own (see `commands.md`).

**Revision 2026-07-17 — finalized strict-ownership contract.** Startup/default authority
is `OPERATOR`. The axis above is unchanged; what is now pinned is the *ownership model*:

| Authority | Operator Station | Local Agent autonomous writes | Hand-off |
|---|---|---|---|
| `OPERATOR` | read/write — every supported action enabled subject to **its own** gates (Home interlock, GPS, connectivity) | disabled on Scout | Release Control available |
| `LOCAL_AGENT` | **read-only** for vehicle-control/configuration writes — telemetry, health, mission state, events and Agent info stay visible | enabled on Scout | **Take Control always available** |
| `RC` | read-only; highest priority, independent physical override | — | reported only, never requestable |

`SET_HOME` and `LOITER` are **deliberately NOT exempt** under `LOCAL_AGENT` — a strict
ownership model was chosen over per-command exceptions. `hasControl` (`value ===
"OPERATOR"`) is the single write-enable predicate for the whole station; `handoffGate`
(`lib/authority.js`) is the single Take/Release predicate. Map and Vehicle both render
from those two — neither re-derives the policy. Pinned by `tests/authority.test.mjs`.

A hand-off uses the **dedicated authority endpoint** (`POST /api/control_authority/{id}`,
a live Scout proxy) and never the command queue.

**Backend gap — RC detection.** RC is plumbed end-to-end as a *reportable* effective
authority (`REPORTABLE_AUTHORITY` includes it; `normAuthority` accepts it; `AuthoritySeg`
lights it; `hasControl` goes false so writes lock). But it only ever appears if Scout's
`GET /agent/control_authority` actually returns `authority: "RC"`. That is unverified —
Scout Flask (`motherpi/services/flask`) is not in this repo, and this repo's own agent-side
client (`Scripts/control_authority.py`) treats anything other than `OPERATOR`/`LOCAL_AGENT`
as unrecognized. Nothing is invented operator-side: with no `RC` from Scout, the UI simply
never shows RC as active. The Vehicle page's "RC override policy → Always" row is the
*architecture invariant* (RC hardware override always exists), **not** a live detection
field — do not read it as "RC is currently active".

## Pending → confirmed / rejected / timeout (never optimistic)
A Take/Release click never asserts success. `createAuthorityController` puts the
request into **PENDING** and only settles it when the *effective* authority Scout
reports matches what was requested (**CONFIRMED**), Scout answers with a
failure/different value (**REJECTED**), or no confirmation arrives within
`AUTH_TIMEOUT_MS` (8 s → **TIMEOUT**). Command buttons enable **only** on a confirmed
`OPERATOR`, and are withheld while any request is in flight.

## Backend (`main.py`)
- `POST /api/control_authority/{vehicle}` accepts only `OPERATOR` | `LOCAL_AGENT`
  (`RC` is a hardware takeover, never requestable → 400). Forwards to Scout and returns
  a normalized ack `{requested, authority(effective), available, reachable}` so the UI
  confirms against the effective value, not the button.
- `GET /api/control_authority/{vehicle}` distinguishes three cases so the 2 s poll is
  quiet and honest: **unknown vehicle id → 404**; **known but no Scout API → 200
  `available:false`** (not a 404 — that spammed the console and conflated "no such
  vehicle" with "no authority backend"); **known + Scout configured → live proxy**, with
  `reachable:false`+`authority:null` if Scout doesn't answer (never a console 5xx).
- On a confirmed **Release** (`LOCAL_AGENT`) the backend cancels still-pending queue
  commands (`cancel_pending_commands`) so a stale operator command cannot fire once
  autonomy is back in control. **Take Control does not touch the queue.**

## Frontend
- `operator/lib/authority.js` — shared controller (pending/confirm/reject/timeout),
  used by both `pages/Map.js` and `pages/Vehicle.js`; no per-page duplication.
- `components/AuthoritySeg.js` — RC · Operator · Local Agent segments; RC lights
  **active** on an `RC` takeover (distinct from its baseline always-available "ready"),
  and a hand-off in flight pulses the requested segment.
- Stale link (`opsStale`, comm ≠ CONNECTED) → authority/arm/mode render **UNKNOWN**,
  commands locked. Never shows a last-known authority as if current.

## Notes / honesty
- Taking or releasing control performs **no** vehicle action by itself — it only changes
  who may issue commands; no arm/disarm/mode change.
- Command execution is still only ever confirmed by a Local Agent result (see
  `commands.md`).

## Verified 2026-07-12 (Playwright against live backend + reachable Scout)
- Default-selected vehicle = Scout (first reporting vehicle), not the placeholder id 1.
- Take Control from `LOCAL_AGENT`: pending → **confirmed OPERATOR**; the 10 command
  buttons enable only after confirmation. Release → `LOCAL_AGENT` re-locks them.
- Selecting id 1 (no Scout API) then back to id 2: endpoint vehicle id follows the
  selection; **no 404/500**, no console errors; id 1 reads UNKNOWN (stale), not an error.
