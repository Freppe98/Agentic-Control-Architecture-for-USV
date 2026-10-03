# Quick move — verification

Operator-side verification of the Map's right-click **“Move &lt;vehicle&gt; here”** action
(`quick-move-v1`). Contract and Scout-side requirements: `QUICK_MOVE_HANDOFF.md`.

**Nothing here touched a real vehicle.** Every Scout response is produced by
`scripts/quick_move_fixture.py` or the browser harness below, and every vehicle they speak
for is named **“Scout (FIXTURE)”** on screen. The backend under test ran on a
**non-production port (8299)** against a **scratchpad `OPERATOR_RUNTIME_DIR`**, with
vehicle 2's outbound API pointed at a loopback stub that answers
`/agent/control_authority`. **No live movement command was sent and no production service
was started, stopped or restarted.**

---

## Automated

| Suite | Result |
|---|---|
| `python -m unittest tests.test_quick_move` | **45 passed** |
| `node --test tests/quick-move.test.mjs` | **42 passed** |
| `npm test` (whole frontend suite) | **1124 passed**, 0 failed |
| `python -m unittest discover -s tests` (whole backend suite) | **1463 passed**, 1 skipped, 0 failed |

### What the unit suites establish

**Independence from missions.** A quick move is issued, run and completed with **no mission
and no planning geometry**, and the mission store is byte-identical before and after. No
code path in the feature reaches a mission-write command, a mission overlay or the
mission-write refresh set (source-asserted, both languages).

**Capability.** A vehicle that never advertised is unsupported (`not_reported`). A vehicle
that is connected, armed, under confirmed OPERATOR authority and reporting perfect telemetry
is **still** refused until it advertises — *being in the fleet is not a capability*. An
explicit `supported:false` is refused with Scout's own reason. `nav_ready` blocks only when
explicitly `false`; absent is not turned into a readiness failure.

**Authority.** The backend re-runs the whole gate at dispatch over a **live** authority read.
`LOCAL_AGENT` and `RC` are refused as `not_operator`; an unreadable authority is refused as
`authority_unreadable`, with a message that does **not** tell the operator to Take Control —
that would send them after the wrong problem. A caller bypassing the browser entirely gets
the same refusal.

**Short-lived, never queued offline.** `DISCONNECTED` and `PARTITIONED` are refused outright
with `link_not_current`; `confirm:true` — which buys every other command type into the
queue-until-contact path — **cannot** override it, and no command record is created at all.
TTL is 45 s vs. the queue's 300 s, and an expired quick move is never delivered.

**Correlation and supersession.** Delivery retries reuse the same `command_id`. A second
move retires the first: the older queue record is terminated so it can never be delivered,
and a delayed status naming the retired id is refused (`superseded_status`) — it **cannot**
put the old target back on the map. A status for an id this station never issued is refused
(`unknown_command_id`). Within one id, an out-of-order `updated_at` is refused
(`stale_status`) and a terminal state is never walked back to a running one
(`terminal_state_regression`). A replayed older *envelope* changes no movement state.

**Delivery is never completion.** A command reaching `EXECUTED` produces no completion
anywhere. `COMPLETED` publishes as `CONFIRMED` only with **both** `arrival_confirmed` and
`loiter_verified`; a bare `COMPLETED`, or one missing either flag, publishes as
`CLAIMED_UNVERIFIED`. Scout reporting `active: null` is not a completion.

**Isolation.** Two vehicles never share quick-move state: a status for vehicle 2's command
arriving on vehicle 3's packet is refused there and changes nothing on either row.

**Parity.** `operator/lib/quick-move.js`'s `QM_REASONS` is asserted equal to
`quick_move.py`'s `REASON_ORDER`, read from both sources — a disabled menu item can never
explain a different problem than the refusal would name.

---

## Browser

Harness: Playwright, Chromium, against the isolated backend. The move is issued **through
the UI** (right-click → menu item), then the command id is read back off the fleet row and
fixture statuses are reported against it, so the whole correlation path is exercised rather
than just the rendering.

**52/52 checks passed — 26 at 1920×1080 (desktop) and the same 26 at 1366×768 (laptop).**

| # | Screenshot | What it shows |
|---|---|---|
| 01 | `quick-move-01-menu-{desktop,laptop}.png` | The one-action menu: **“Move Scout (FIXTURE) here”**, the clicked destination to 6 decimals, and `209 m from Scout (FIXTURE)`. |
| 02 | `quick-move-02-menu-edge-{…}.png` | A right-click at the bottom edge — the menu is measured and clamped fully into the viewport (the case that matters at laptop height). |
| 03 | `quick-move-03-issued-{…}.png` | Immediately after the click: a tracked move exists, delivery is **not** confirmed, and **no movement status has been invented from the click**. |
| 04 | `quick-move-04-moving-{…}.png` | `MOVING TO TARGET` + the separate delivery line *“Scout has this move.”*, `Remaining 118 m` attributed to **Scout**, `Mode GUIDED`, both ages, the **STOP / LOITER** action, and the dashed target marker on the map. |
| 05 | `quick-move-05-completed-unverified-{…}.png` | A bare `COMPLETED`: the claim is shown **with** the caveat *“Scout reported the move complete but did not confirm arrival or verified LOITER. Treat the hold as unconfirmed.”* |
| 06 | `quick-move-06-completed-verified-{…}.png` | `COMPLETED` **with** both evidence flags → *“Arrived and holding”*, no caveat, and Stop withdrawn. |
| 07 | `quick-move-07-last-known-{…}.png` | After the link ages out: *“Arrived and holding **(last known)**”* in the neutral tint, with the note that Scout is not currently reporting this move. |
| 08 | `quick-move-08-unsupported-{…}.png` | `supported:false` → the action is **disabled** with Scout's own sentence, not a generic one and not a silently missing menu item. |

Also asserted in the browser:

- **Escape** closes the menu; an **outside click** closes the menu.
- **Quick move is its own inspector section**, ordered
  `Status → Vehicle Commands → Quick move → Agent Mission → Vehicle readiness → …` — it sits
  with the immediate manual controls, ahead of agent supervision.
- **It is never rendered inside the mission progress bar** (`#mpbar` contains no quick-move
  card), and the existing companion UI, compact Pixhawk mission card and bottom progress
  strip are untouched.
- **No page errors** at either size.

### Vehicle switching

Covered by `tests/quick-move.test.mjs` against `Map.js`'s `select()`: switching vehicles
closes the menu and removes the target marker, so neither the destination nor the status can
leak between vehicles. The **movement itself is untouched** — it lives on the other vehicle's
fleet row and keeps running; only this page's view of it moves.

---

## Reproducing

```powershell
# 1. Isolated backend (non-production port + scratchpad runtime dir)
$env:OPERATOR_RUNTIME_DIR = "$env:TEMP\qm-runtime"
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8299 --no-access-log

# 2. Fixture scenarios (loopback only; refuses any other host)
.\.venv\Scripts\python.exe scripts\quick_move_fixture.py --base http://127.0.0.1:8299 --list
.\.venv\Scripts\python.exe scripts\quick_move_fixture.py --base http://127.0.0.1:8299 --issue --scenario arrive_and_hold
```

The dispatch gate needs a **live** `OPERATOR` authority read, so against a backend with no
reachable Scout the fixture's `--issue` is refused with `authority_unreadable` — that is the
gate working, not a fixture failure. The browser run above stubbed
`/agent/control_authority` on loopback for exactly this reason.

---

## Round 2 — Scout integration (Scout checkpoint `a102f21a`)

Scout implemented its side and handed over a written handoff (its captures were **not**
handed over — see `QUICK_MOVE_HANDOFF.md` §12). This round verified the operator against that
contract, fixed what it exposed, and fixed the command-delivery defect.

| Suite | Result |
|---|---|
| `tests.test_quick_move` | **46 passed** |
| `tests.test_quick_move_delivery` (new) | **21 passed** |
| `tests.test_scout_quick_move_compat` (new) | **20 passed** |
| `tests/quick-move.test.mjs` | **50 passed** |
| Whole backend suite | **1505 passed**, 1 skipped |
| Whole frontend suite | **1132 passed** |
| Browser, round 2 | **28/28** at 1920x1080 and 1366x768 |

### Capture replay

All eight of Scout's scenarios were replayed through the real `POST /agent/status` in an
isolated runtime, with correlated command ids, and the **resulting fleet state** checked per
scenario — not just HTTP 200. Results are tabulated in `QUICK_MOVE_HANDOFF.md` §12.

Two things the replay itself exposed, both now fixed in the harness:

1. **Scenario isolation is mandatory.** Scout's scenarios are independent and each starts at
   its own `t0`, so replayed into shared state every scenario after the first was silently
   refused by the per-vehicle monotonic envelope guard (`stale_timestamp`) while the run still
   looked like a pass. `scout_payload_replay.py` now isolates per scenario and prints a loud
   `!! REFUSED AS STALE` line for any envelope that changed nothing.
2. **Correlated command ids.** Scout's captured ids were never issued here, so a bare replay
   has every status correctly refused as `unknown_command_id`. `--correlate-commands` mints a
   real operator command per captured id and rewrites it **in memory**; the capture files on
   disk are never modified.

### Browser — round 2

| Screenshot | What it shows |
|---|---|
| `quick-move-r2-01-rejected-mission-bound-{desktop,laptop}.png` | A Scout `prepare()` rejection — **Rejected by Scout**, with Scout's mission-supervisor sentence verbatim. Previously read "Sent - awaiting Scout" beside its own rejection. |
| `quick-move-r2-02-arrived-not-holding-{...}.png` | `FAILED` with `arrival_confirmed: true` - the amber warning that the vehicle **reached** the target but the hold was **not** confirmed. Previously collapsed into a bare "Failed". |
| `quick-move-r2-03-incoherent-completion-{...}.png` | `COMPLETED` + `loiter_verified: true` + `mode: GUIDED` - refused as a confirmed hold, with the contradiction named. |
| `quick-move-r2-04-coherent-completion-{...}.png` | The same report with `mode: LOITER` (what Scout now sends) - **Arrived and holding**, no caveat. |
| `quick-move-r2-05-link-gap-invalidated-{...}.png` | A quick move created on a healthy link, then a real link gap: never delivered, record reads *"Link left CONNECTED (PARTITIONED) before delivery - Quick move invalidated"*, card stops presenting it as live. |

### Command delivery - the defect, reproduced and fixed

Creation-while-CONNECTED plus a 45 s TTL **was not sufficient**, and this was reproduced
rather than theorised: a quick move survived a full `CONNECTED -> PARTITIONED -> DISCONNECTED
-> CONNECTED` cycle and was handed to Scout afterwards. Three independent bounds now apply at
the moment of delivery (link-continuity epoch, invalidation on link loss, a 15 s claim window)
- see `QUICK_MOVE_HANDOFF.md` §7. `SET_MODE_LOITER` is deliberately exempt: the safety hold
must still reach the vehicle across an outage.

---

## Known limitations of this verification

1. **No hardware, and no capture of Scout's real output.** Everything above verifies the
   **operator station's** behaviour given a conforming Scout. Scout has now implemented its
   side (checkpoint `a102f21a`), but its exported captures were **not handed over**, so the
   replay runs against a **reconstruction transcribed from Scout's handoff prose** — weaker
   evidence than a capture, and the difference is spelled out in `QUICK_MOVE_HANDOFF.md` §12.
   Neither side has moved a vehicle: no SITL and no live mavlink2rest exists in either
   development environment. See §0 and §14 of that document.
2. **The action stays unavailable for every real vehicle** until one advertises
   `capability.supported: true`. Nothing in this work makes a live movement command reachable
   by accident.
3. **Mission interaction is a RESTRICTION, not a suspension feature** (handoff §11). Scout
   **rejects** a quick move while its controller is actively driving a bound mission; with no
   mission bound, entering GUIDED is itself the suspension. **There is no automatic mission
   suspension and no automatic resume on either side**, and the operator cannot pre-empt the
   rejection — it has no way to know what Scout's controller currently has bound, so the
   refusal arrives as a command rejection with Scout's own sentence.
4. **The 45 s TTL is an operator-side estimate** of “long enough to deliver and confirm a
   mode change, short enough that the intent is still current”. It is one constant and moves
   if Scout's GUIDED-entry path proves slower.
