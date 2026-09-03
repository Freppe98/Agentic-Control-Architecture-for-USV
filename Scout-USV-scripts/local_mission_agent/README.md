# Scout Local Mission Agent

This is the on-vehicle autonomy service for the AqualityONE **Scout** USV
(Raspberry Pi 5 + Pixhawk 6C, ArduRover firmware). It runs alongside the
vehicle's Flask API, watches mission progress, vehicle health, battery
energy margin and Operator-link communication quality, and -- only when
explicitly enabled and authorized -- takes autonomous safety actions
(safety hold / safe-return replanning) when the Operator can't be reached
or reserves run out. It is one of the components exercised by the thesis
field experiments (`final_thesis_experiment_commands.txt` at the repo
root); it is not itself the thesis document.

## Architecture at a glance

```
Operator Station  <--4G + WireGuard-->  Local Mission Agent (:8090, this service)
                                              |  HTTP (127.0.0.1)
                                         Vehicle Flask API (:8080)
                                              |  mavlink2rest (HTTP/REST)
                                         Pixhawk 6C (MAVLink)
```

- **Local Mission Agent** (this directory) -- polls/relays Operator commands,
  monitors comm/energy/mission state, and (when authorized) drives mission
  execution and safe-return replanning through the vehicle's own verified
  Flask endpoints. Never talks MAVLink directly. Deployed as the systemd
  service `local-mission-agent.service`, with its own read-only inbound HTTP
  API on `127.0.0.1:8090` (`agent_server.py`).
- **Vehicle Flask API** (`motherpi/services/flask/`) -- owns the one
  MAVLink/mavlink2rest connection to the Pixhawk, all verified mode/arm/
  mission-upload writes, control authority, and raw telemetry. Runs on
  `:8080`. See [MISSION_CONTRACT_v1.md](../flask/MISSION_CONTRACT_v1.md).
- **Operator link** -- 4G + WireGuard (`wg0`) back to the Operator Station;
  the Local Agent's own `CommunicationMonitor` (`communication.py`) is the
  one source of truth for how healthy that link currently is (see
  "Communication-aware autonomy" below), independent of the vehicle Flask
  service.

## Requirements

- Python 3 (stdlib + the one third-party dependency, `requests`, already
  used throughout `api_client.py`/`communication.py`/`command_executor.py`/
  the gateway modules -- no `requirements.txt` in this directory because
  nothing else is needed).
- A reachable vehicle Flask API (`LOCAL_FLASK_URL`, default
  `http://127.0.0.1:8080`) with mavlink2rest/Pixhawk connected.
- `wg`/`sudo -n wg show` for WireGuard handshake-freshness evidence
  (`communication.py`); passwordless sudo for that one command is expected
  to already be configured on the deployed Pi.

## Install / run

There is no package install step -- this is a plain script run in place.

1. `cp local_config.example.py local_config.py` and set `OPERATOR_URLS` to
   this machine's Operator Station (gitignored, machine-specific; see
   "Configuring the operator endpoint(s)" below).
2. Run in the foreground for development: `./run_local_agent.sh` (or
   `python3 -u local_agent.py`).
3. **On the deployed Scout**, this runs as the systemd service
   `local-mission-agent.service` instead (started/stopped/inspected the
   normal systemd way -- the unit file itself is installed on the vehicle
   and is not tracked in this repo):
   ```bash
   sudo systemctl restart local-mission-agent
   sudo systemctl status local-mission-agent --no-pager
   sudo journalctl -u local-mission-agent -f
   ```
4. `./check_local_agent.sh` -- quick read-only status check (process,
   vehicle Flask reachability, operator reachability, buffer count,
   authority).

## Ports

| Port | What | Owner |
|---|---|---|
| `8090` | Local Mission Agent's own read-only HTTP API (`LOCAL_AGENT_HTTP_PORT`) | this service (`agent_server.py`) |
| `8080` | Vehicle Flask API (mission control, telemetry, `/nav/*`) | `motherpi/services/flask/` |
| `6040` | mavlink2rest (Pixhawk bridge), host-local, not exposed by this service | standalone, outside this repo |

## Inspecting status / diagnostics

```bash
curl -s http://127.0.0.1:8090/agent/diagnostics | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8090/agent/system_check | python3 -m json.tool
curl -s http://127.0.0.1:8090/agent/command_history | python3 -m json.tool
curl -s http://127.0.0.1:8090/agent/decision_timeline | python3 -m json.tool
curl -s http://127.0.0.1:8090/agent/mission_execution/status | python3 -m json.tool
curl -s http://127.0.0.1:8090/agent/replan/status | python3 -m json.tool
```
See "Vehicle Health diagnostics" below for the full read-only endpoint set.

## API overview

- **Mission execution** (`/agent/mission_execution/*`, `mission_execution_controller.py`)
  -- Start / Pause / Resume / Stop / Rearm for the operator-prepared
  mission, with mission identity/hash readback proof, guarded Home
  verification, and fail-closed restart recovery. See
  [BENCH_MISSION_EXECUTION.md](BENCH_MISSION_EXECUTION.md) and
  [BENCH_TEST_MISSION.md](BENCH_TEST_MISSION.md).
- **Replanning / config** (`/agent/replan/*`, `replan_controller.py`,
  `replan_config.py`) -- the safe-return-on-degradation transaction (energy
  or communication triggered), its `PATCH /agent/replan/config` runtime
  flags (`autonomous_execution_enabled`/`dry_run`/`rtl_fallback_enabled`),
  and the `PUT/DELETE /agent/replan/experiment` synthetic-injection
  endpoint used for bench/dry-run trials. See "Enabling autonomous
  execution for a live trial" below.
- **Experiment recorder** (`/agent/experiment_recording/*`,
  `experiment_recorder.py`) -- records telemetry (~2 Hz), decisions,
  risk/action/FSM transitions, communication state (incl. WireGuard
  handshake age/freshness), mission/planning hashes and revisions, a
  config snapshot, checksums, and source commit/branch for each run, under
  `experiment_runs/<run-id>/` (gitignored -- runtime evidence, not source).
  `analyze_energy_run.py` post-processes an energy-focused run. See
  `final_thesis_experiment_commands.txt` at the repo root for the
  consolidated final field-experiment command set (E1-E4, below).

## Control-authority semantics

Two stored software authorities, **OPERATOR** (default/startup) and
**LOCAL_AGENT**; autonomous writes require fresh `LOCAL_AGENT` authority,
re-checked before every individual write, never cached. Full detail,
including the narrowly safety-exempt LOITER carve-out, is in "Authority
model" below.

## Communication states

**CONNECTED** / **PARTITIONED** / **DISCONNECTED**, computed by
`communication.py` from Operator reachability, general internet
reachability, and WireGuard handshake freshness (`WG_RECENT_HANDSHAKE_S =
180s`). PARTITIONED is degraded-but-application-unreliable communication
with the underlying VPN/network still evidenced live; DISCONNECTED
requires full loss evidence, including the WireGuard handshake itself
going stale. PARTITIONED maps to ELEVATED risk / CONTINUE_WITH_CAUTION;
DISCONNECTED maps to HIGH risk / HOLD / REQUEST_HOLD. Full detail in
"Communication-aware autonomy" and "E3" below.

## Safety / fail-closed behavior (summary)

- A comm- or energy-triggered safety hold that cannot be positively proven
  (verified LOITER + physical settle), or any attempted-and-failed replan,
  fails closed to mission-execution state **SUSPENDED** (rearmable, no
  auto-resume).
- A comm- or energy-triggered hold-only safety hold that **is** positively
  proven settles into mission-execution state **PAUSED** instead -- a
  successful, deliberate controlled pause (mission/sequence retained,
  vehicle in verified LOITER), not a failure. Communication/energy recovery
  never auto-resumes a PAUSED (or SUSPENDED) mission -- only an explicit
  operator Resume/Stop (or rearm) does.
- A Local Agent restart mid-operation fails closed
  (`UNKNOWN_AFTER_RESTART`); nothing autonomous is ever resumed blind.
- No blind AUTO: mode changes are always verified against a
  `HEARTBEAT.custom_mode` readback, and `AUTO`/`RTL`/`MISSION_RESUME`
  additionally require a verified Pixhawk Home.

## Limitations

- Validated on a **single Scout USV**; multi-USV/fleet behavior is not
  exercised by these experiments.
- Obstacle-aware replanning groundwork exists (`obstacle_model.py`,
  `detour_planner.py`) but is **not enabled** in the final field
  configuration -- replanning uses geometry constraints (navigable area,
  home corridor, no-go zones) from an operator-authored, immutable planning
  package, not live obstacle avoidance.
- The energy feasibility model (`mission_feasibility.py`) is a simple,
  transparent, conservative capacity/current/time model, **not** a
  high-fidelity battery model, and is calibrated/validated from a single
  field energy-characterization exercise, not an independently repeated
  validation campaign.
- No formal safety proof is claimed anywhere in this codebase or its
  documentation -- behavior is validated by the field experiments described
  in `final_thesis_experiment_commands.txt`, not by formal verification.

## Testing

```bash
cd motherpi/services/local_mission_agent
python3 -m unittest discover -s . -p "test_*.py"
```
Individual files also run standalone, e.g. `python3 test_communication.py`.

## Repository / thesis context

This directory is part of the `AqualityONE` repository
(`feature/local-mission-agent-v2` branch at the time of the thesis field
experiments). The final field-experiment families are **E1** (nominal
mission execution), **E2** (energy-triggered mission adaptation), **E3**
(communication degradation and full loss), and **E4** (operator authority
takeover); energy characterization is supporting calibration for E2, not a
separate experiment family. See `final_thesis_experiment_commands.txt` at
the repo root for the exact commands, and `experiment_runs/` (gitignored)
for retained run evidence with checksums.

---

## Directory contents

```
local_mission_agent/
├── __init__.py
├── local_agent.py          # main loop
├── communication.py        # CommunicationMonitor: CONNECTED/PARTITIONED/DISCONNECTED + RECOVERED edge
├── information_policy.py   # what data to send in each state
├── api_client.py           # HTTP to Flask/operator
├── state_machine.py        # MissionRunner: owns mission phase (TRANSIT/SEARCH/RETURN/...)
├── models.py               # JSON message + event structure
├── buffer.py               # local storage + flush for unsent packets
├── collectors.py           # comm-aware communication status + base agent status builders
├── decision_engine.py      # Agent page reasoning layer: current_decision, watch_conditions, policy, confidence
├── transition_reasons.py   # concrete "why" text for comm/mission/authority transitions
├── transition_log.py       # rolling ~100-entry transition audit trail (payload.transitions, payload.agent.decision_timeline)
├── process_health.py       # this process's own cpu_percent (no psutil, /proc/<pid>/stat)
├── command_executor.py     # operator command_type -> local Flask endpoint mapping; HOME_VERIFICATION_REQUIRED gate
├── command_handler.py      # validate (expiry/dedup/support/authority/home-verification), execute, build ack for one command
├── autonomy_gate.py        # gate for a future Local Agent *autonomous* vehicle-control write (not the operator-command relay above)
├── command_log.py          # persisted dedup record of processed command_ids
├── command_history.py      # rolling record of recent command lifecycles (GET /agent/command_history)
├── agent_server.py         # inbound HTTP server (read-only): GET /agent/diagnostics, POST /agent/system_check, GET /agent/command_history, GET /agent/decision_timeline, GET /agent/mission, GET /agent/pixhawk_mission
├── diagnostics.py          # builds the diagnostics/system_check payloads agent_server.py serves
├── mission.py              # builds the GET /agent/mission payload (legacy schema) agent_server.py serves
├── pixhawk_mission.py      # builds the GET /agent/pixhawk_mission payload (Pixhawk Mission card schema) agent_server.py serves
├── runtime_status.py       # thread-safe last-main-loop-iteration timestamp, for the "local_agent" component
├── config.py               # IPs, ports, rates, thresholds
├── local_config.example.py # template for machine-specific OPERATOR_URLS override
├── mock_operator.py        # stdlib-only fake operator backend for dev/testing the command path
├── pretest_check.py        # read-only pre-test printout: comm/mavlink/battery/mission/authority/decision (see "Practical comm-degradation test")
├── bench_preflight.py      # read-only bench-readiness gate: mavlink2rest counter/age, disarmed/MANUAL, Flask/agent/operator, authority, mission contract, home, no unresolved mission op, buffer; PASS/FAIL + JSON evidence
├── OUTBOUND_BUFFER_REVIEW.md # review of outbound-buffer behaviour for operator-rejected command_results + the safest retry/drop assumption
├── test_bench_preflight.py # standalone unittest coverage for bench_preflight.py (all checks mocked, no live services)
├── test_buffer.py          # standalone unittest coverage for buffer.py, incl. the MAX_BUFFERED_MESSAGES cap
├── test_command_handler.py # standalone unittest coverage for command_handler, incl. the Home-verification gate
├── test_control_authority.py # standalone unittest coverage for the command-relay authority gate
├── test_autonomy_gate.py   # standalone unittest coverage for autonomy_gate.py
├── test_diagnostics.py     # standalone unittest coverage for diagnostics.py
├── test_mission.py         # standalone unittest coverage for mission.py
├── test_pixhawk_mission.py # standalone unittest coverage for pixhawk_mission.py
├── test_decision_engine.py # standalone unittest coverage for decision_engine.py
├── test_transition_reasons.py # standalone unittest coverage for transition_reasons.py
├── test_transition_log.py  # standalone unittest coverage for transition_log.py
├── test_process_health.py  # standalone unittest coverage for process_health.py
├── run_local_agent.sh      # cd + run local_agent.py
└── check_local_agent.sh    # process/endpoint/operator/buffer/authority/diagnostics status check
```

## Authority model

There are **three distinct things** that can affect the vehicle, and they
must not be confused with each other:

1. **Explicit operator-command relay** -- commands an operator (or the
   Operator Backend on their behalf) explicitly queued, polled and executed
   by `command_handler.py`/`command_executor.py`, documented below.
2. **Local Agent autonomous vehicle-control writes** -- the Local Agent
   deciding on its own (not from a queued command) to write to the vehicle.
   Two such paths exist: `replan_controller.py` (agent-initiated safe-return/
   hold transactions -- LOITER/plan/validate/upload/verify/AUTO, and the
   guarded RTL fallback) and `mission_execution_controller.py` (the ORIGINAL
   mission's own Start/Pause/Resume/return-to-Home lifecycle). Both are
   gated OFF by default (see "Enabling autonomous execution for a live
   trial" below) and both call `autonomy_gate.check_autonomous_write_
   authority()` fresh before every individual write -- see "Local Agent
   autonomous writes" below. `decision_engine.py` and `risk_model.py`
   themselves remain read-only reasoning/scoring layers -- they compute a
   recommendation (`decision_policy.py`) that only ever becomes an
   `ActionRequest` fed into `replan_controller.observe()`; neither calls a
   vehicle Flask write endpoint. `state_machine.py` is in-memory phase
   bookkeeping with no I/O.

   `decision_policy.ActionRequest` is the **sole** authoritative trigger
   into the replan FSM (`replan_controller.observe()`'s `want`). The legacy
   `energy_policy.py` signal that used to independently start a transaction
   no longer does -- `EnergyResult` is still computed every iteration and
   passed into `observe()`, but retained only as evidence/debounce/
   diagnostics (its persistence-debounced `.decision`, `.reason_codes`, and
   `.inputs` are recorded on `status()` for observability); it cannot start
   a transaction on its own. One FSM, one entry point, one trigger-
   generation latch -- never two independent triggers.
3. **Physical RC override** -- a human on a transmitter driving the vehicle
   directly through the Pixhawk, independent of either software authority.
   See "Physical RC override" below.

Priority when more than one could apply: **RC override > OPERATOR >
LOCAL_AGENT**.

### Stored software authority: OPERATOR / LOCAL_AGENT

The *stored* software authority is exactly two values, owned by the vehicle
Flask service (`motherpi/services/flask/services/control_authority.py`,
wired into `routes/agent_routes.py`), not by the Local Agent and not on a
server of its own. Setting it never sends a MAVLink message and never
changes Pixhawk mode by itself.

- **`OPERATOR`** (default and startup value): the operator command queue is
  explicit operator intent, so **every supported command_type executes** --
  `SET_HOME`, `LOITER`, `SET_MODE_AUTO`, `SET_MODE_MANUAL`, `SET_MODE_HOLD`,
  `RTL`/`RETURN_HOME`, `ARM`/`DISARM`, `MISSION_PAUSE`/`MISSION_RESUME`, no
  exceptions (strict model -- neither `SET_HOME` nor `LOITER` is exempt from
  this gate the way they are from the separate Home-verification gate
  below). The Local Agent's own autonomous vehicle-control writes (item 2
  above) are blocked. Telemetry, health, events, buffering, diagnostics, and
  communication monitoring all continue regardless.
- **`LOCAL_AGENT`**: the operator queue is *not* the Local Agent's source of
  vehicle-control intent while it holds this authority, so every queued
  command_type is instead **rejected with a terminal result** (a reason
  like `"blocked: SET_MODE_AUTO requires OPERATOR control authority"`). The
  Local Agent's own autonomous vehicle-control writes are allowed, gated by
  `autonomy_gate.check_autonomous_write_authority()` (see below).

The gate is checked in `command_handler.process_command()`, as a blanket
check on the whole queue (no per-command_type exemption). `local_agent.py`'s
`_poll_and_execute_commands` always polls the operator backend and passes
`control_authority` straight through -- polling only ever skips while
`comm_state == DISCONNECTED` (there's nothing to poll against). Because the
operator backend's command queue is deliver-once (a poll always claims
whatever's pending, whether or not it's ultimately executed -- see
`mock_operator.py`'s `GET /agent/commands`), a command blocked by this gate
is a **terminal rejection**, not left silently pending until authority
changes -- an operator who wants it to run must reissue it once the right
authority is in effect.

**Startup**: the vehicle Flask service always initializes `control_authority`
to `OPERATOR` in memory -- it is never persisted to disk and never assumed,
so it resets to `OPERATOR` on every restart of that service (e.g. a gunicorn
worker restart). The Local Agent doesn't own or cache this value across its
own restarts either: every loop iteration reads it fresh off the same
`GET /agent/state` response it already fetches for telemetry
(`vehicle_state["agent"]["control_authority"]`, see `local_agent._current_authority`),
and if that field is ever missing or the fetch fails outright, the Local
Agent fails safe to `OPERATOR` rather than assuming it still has authority.

**Take Control (`LOCAL_AGENT` -> `OPERATOR`) always succeeds immediately.**
`control_authority.set_authority()` is a synchronous, unconditional in-memory
assignment -- no lock, no wait, no check of mission or vehicle state. The
Local Agent has no part in this transition at all (it only ever reads
`control_authority`, via its existing `GET /agent/state` poll -- see
`api_client.get_control_authority()`'s docstring); it cannot delay or deny
it. Combined with autonomous writes re-checking authority fresh every cycle
(never caching it -- see `autonomy_gate.py`'s contract below), a Take
Control request takes effect within one Local Agent cycle with no
coordination required.

```bash
# read current authority
curl -s http://127.0.0.1:8080/agent/control_authority

# hand control to the Local Agent (enables its autonomous writes, blocks the operator command queue)
curl -s -X POST http://127.0.0.1:8080/agent/control_authority \
    -H 'Content-Type: application/json' -d '{"authority": "LOCAL_AGENT"}'

# Take Control: hand control back to the operator (always succeeds immediately)
curl -s -X POST http://127.0.0.1:8080/agent/control_authority \
    -H 'Content-Type: application/json' -d '{"authority": "OPERATOR", "reason": "Take Control"}'
```

Every transition is logged (`print` plus an event in the vehicle's own event
log, `services/control_authority.set_authority` in the Flask app -- shows up
in `GET /agent/state`'s `events` the same way `RTL_COMMANDED`/`NAV_STARTED`
already do), and the current value is reported on every Local Agent status
message as `payload.agent.control_authority`. `api_client.py` also exposes
`get_control_authority()`/`set_control_authority()` for tooling/tests that
want to read or drive this the same way an operator console would -- the
Local Agent's own main loop only ever reads it (bundled into its existing
`get_vehicle_state()` call), it never writes it.

### Local Agent autonomous writes

`autonomy_gate.check_autonomous_write_authority(control_authority)` is the
gate every autonomous vehicle-control write path calls immediately before
every write attempt (never once at startup, never cached) -- it returns
`(allowed, reason)`, `allowed` only when `control_authority == "LOCAL_AGENT"`
exactly. `replan_controller.py` and `mission_execution_controller.py` both
call it fresh before each individual write (LOITER/AUTO/RTL/upload/Set
Home), so authority moving away from `LOCAL_AGENT` stops writes within an
in-flight transaction rather than being raced. It intentionally never gates
on RC -- see "Physical RC override" below for why.

Being *authorized* to write is not the same as the feature being *enabled*
at all -- see the next section for the separate `autonomous_execution_
enabled`/`dry_run` master switches both controllers also check.

### Enabling autonomous execution for a live trial

Three independent config flags, all in `replan_config.py` (`ReplanConfig`),
all **`False`/`True`-safe by default** (the controller reasons and, in
dry-run, plans, but never writes to the vehicle) and all `REPLAN_*`
environment-variable overridable or runtime-PATCH-able via
`PATCH /agent/replan/config`:

| Flag | Default | Meaning |
|---|---|---|
| `autonomous_execution_enabled` | `False` | Master switch: may `replan_controller.py` EVER write to the vehicle. Checked once per `observe()`, before a transaction can even start. |
| `dry_run` | `True` | Full transaction lifecycle (LOITER/plan/validate/upload/verify/AUTO) is simulated -- every vehicle write is substituted with a flagged simulated result. |
| `rtl_fallback_enabled` | `False` | A **separate** policy: whether a verified Pixhawk RTL is permitted as the last-resort fallback once a planned-return transaction's retries are exhausted. Independent of the two flags above -- RTL does not respect operator no-go polygons, so this is opt-in on its own even with autonomous execution fully enabled. |

RTL fallback additionally requires (`replan_controller._fallback()`), **all** of:
`rtl_fallback_enabled=True`, a fresh authority re-check (`LOCAL_AGENT`), a
verified Pixhawk Home, **and** the CURRENT `rtl_return_feasible` (from
`mission_feasibility.py`, exposed via `decision_policy_instance.
latest_feasibility_evidence`, continuously refreshed by the main loop) is
proven `True` -- not merely a verified Home. `False` (proven infeasible) or
`None`/unknown (unproven, or the callback is unwired) both fail closed to
`SAFE_HOLD`, never a blind RTL; the skipped-fallback reason is recorded as
`RTL_FALLBACK_INFEASIBLE` in `status()["last_error"]`.

None of these should be committed as new repository defaults for a live
trial -- the smallest defensible mechanism is to set them as environment
variables in the launch shell/session for that specific trial run only:

```bash
REPLAN_AUTONOMOUS_EXECUTION=1 REPLAN_DRY_RUN=0 REPLAN_RTL_FALLBACK_ENABLED=1 \
    ./run_local_agent.sh
```

(`REPLAN_RTL_FALLBACK_ENABLED=1` is only needed if the trial intends to
exercise the RTL-fallback path on a deliberately-failed planned return --
decide it per trial, not as a standing default.) Alternatively, `PATCH
/agent/replan/config` sets them for the current process only, in memory,
reverting to the environment/default on the next restart -- useful for
toggling mid-session without a restart, but never a persisted deployment
mechanism. There is currently no `local_replan_config.py`-style override
file analogous to `local_config.example.py` (that file only covers
`OPERATOR_URLS`) -- adding one was judged out of scope for this task.

### Physical RC override

A human on a transmitter can drive the vehicle, or switch its Pixhawk mode,
directly -- independent of either stored software authority, and with
strictly higher priority than both. **This codebase does not, and must
not, turn RC input into a mutation of `control_authority`, and does not gate
anything in software on it.** The receiver and Pixhawk already handle
physical RC input on their own; software fighting that (e.g. auto-reverting
`control_authority`, or blocking a queued command because RC "looks" active)
would only add surprising failure modes without adding real safety, since RC
already wins at the hardware level regardless of what either software
authority says.

What *is* reported today, purely for the operator to observe -- not to gate
anything:

- `GET /agent/diagnostics`' `rc_receiver` component (`services/diagnostics_service.py`'s
  `_diag_rc_receiver()`) -- whether an RC receiver is connected and
  `RC_CHANNELS` is fresh. This proves a receiver is broadcasting, **not**
  that a human is actively moving the sticks right now (a receiver can
  broadcast continuously with the sticks centered) -- it must never be
  read as "RC is overriding."
- `GET /agent/state`'s `telemetry.mode`/`mode_name`/`armed`
  (`services/agent_state.py`, from `HEARTBEAT.custom_mode`/`base_mode`) --
  the Pixhawk's current mode/armed state, however it got there (RC,
  operator command, or Local Agent write). This does not distinguish an
  RC-commanded mode change from a software-commanded one.

A reliable "RC is actively overriding right now" signal (e.g. cross-checking
`HEARTBEAT.base_mode`'s `MAV_MODE_FLAG_MANUAL_INPUT_ENABLED` bit against
non-centered `RC_CHANNELS` values) is real MAVLink-domain work intentionally
left for separate future work rather than guessed at here -- see
`autonomy_gate.py`'s module docstring.

## Communication-aware autonomy

Ownership split:
- The vehicle (Flask app + Pixhawk) owns raw telemetry and mission identity/
  progress (`current_mission_id`, `mission_active`, `current_waypoint`,
  `mission_count`), set via the existing `/start_mission` / `/nav/upload_mission`
  flow. The Pixhawk executes an uploaded AUTO mission on its own regardless of
  comm state -- the Local Agent does not drive the vehicle.
- The Local Agent owns the *interpretation* of that progress into a mission
  phase (`MissionRunner` in `state_machine.py`), and owns its own perception
  of the operator link (`CommunicationMonitor` in `communication.py`).

Behavior per comm state:
- **CONNECTED**: full reporting at `CONNECTED_INTERVAL`.
- **PARTITIONED**: mission continues onboard; reporting drops to
  `PARTITIONED_INTERVAL` and sheds the `measurements` group
  (`information_policy.py`); mission/comm transitions are still recorded as
  events.
- **DISCONNECTED**: mission continues onboard; status messages fail to send
  and are appended to `agent_buffer.jsonl` (`buffer.buffer_message`) instead
  of being dropped.
- **RECOVERED** (edge from PARTITIONED/DISCONNECTED back to CONNECTED,
  `CommunicationMonitor.just_recovered`): a fresh live status must be sent
  successfully *first* -- only then does `buffer.flush_buffer` drain the
  backlog (`local_agent.py`'s `pending_flush`, cleared only after a
  successful send). This way the operator's current-state view is always
  the freshest write, and buffered history never overwrites it, without
  discarding anything: every buffered message is still replayed, in order,
  regardless of age.

`collectors.py` builds the `communication`/`agent` status blocks from the
Local Agent's own `comm_state`, instead of forwarding the vehicle's
`/agent/state`, which has no visibility into operator reachability.

## Operator -> USV commands

The Local Agent polls the operator backend for pending commands and executes
them by calling the vehicle's existing local Flask endpoints -- the browser
never talks to the Scout directly, and the Local Agent never talks to
MAVLink directly, only through the same `/nav/*` endpoints the operator UI
already uses.

**Allowed command types** (`command_executor.ALLOWED_COMMANDS`):

| command_type | Local Flask endpoint | Requires verified Home? |
|---|---|---|
| `SET_MODE_AUTO` | `POST /nav/AutoModeOn` | **Yes** |
| `SET_MODE_MANUAL` | `POST /nav/manual` | No |
| `SET_MODE_HOLD` | `POST /nav/hold` | No |
| `LOITER` / `SET_MODE_LOITER` | `POST /nav/loiter` | **No -- never** (safety command, see "Set Home" below) |
| `RTL` / `RETURN_HOME` | `POST /nav/rtl` | **Yes** |
| `MISSION_PAUSE` | `POST /nav/pause` (a **verified LOITER** mode change under the hood) | No |
| `MISSION_RESUME` | `POST /nav/resume` (an AUTO mode change under the hood) | **Yes** |
| `ARM` | `POST /nav/ArmOn` (verified via HEARTBEAT base_mode) | No |
| `DISARM` | `POST /nav/Disarm` (verified via HEARTBEAT base_mode) | No |
| `MISSION_UPLOAD` | `POST /agent/upload_mission` (validated, verified by fresh readback; runs on a **bounded background worker**) | No |

**Verified outcomes.** A 2xx from the vehicle Flask service is not, by itself,
a successful vehicle action. For every command_type with a verifiable end
state -- the mode changes above, `MISSION_PAUSE`/`MISSION_RESUME`, `ARM`/
`DISARM`, and `MISSION_UPLOAD` -- `command_normalization.py` turns the raw
Flask response into a stable normalized `result` block
(`accepted`/`executed`/`verified`/`expected_state`/`observed_state`/`error`/
`lifecycle`) and the command's terminal `status` is `executed` **only** when
the vehicle actually reached and held the expected state (mode change verified
via `HEARTBEAT.custom_mode`, arm state via `HEARTBEAT` `base_mode` SAFETY_ARMED
bit, upload via a full fresh readback whose count and content hash match).
Otherwise it is `failed`, with the reason surfaced. `SET_HOME` keeps its
existing pass-through contract (its own `accepted`/`verified` block is returned
unchanged). The raw Flask fields are always preserved alongside the normalized
ones for compatibility.

**`MISSION_UPLOAD`** is the one command whose vehicle-side execution is slow
(MISSION_CLEAR_ALL + the full `MISSION_REQUEST_INT` handshake + a complete
fresh readback), so it runs on a **bounded single-command background worker**
(`mission_upload_worker.py`) instead of blocking the main reporting loop:
only one upload at a time (a second is rejected terminally), the
accepted/executing state is surfaced in the periodic status payload
(`agent.mission_upload`), and the terminal `executed`/`failed` result is
delivered as a normal `command_result` when it completes. The mission is
carried in the command `params` as either a canonical `waypoints` list
(`[{latitude,longitude,loiter_time}, ...]`) or the operator UI's `geojson`.
The vehicle Flask service (`services/mission_upload_service.py`) validates and
canonicalizes it, **refuses to upload while armed or mid-mission**, runs the
real upload handshake, and reports `verified` only after the fresh readback's
count and content hash match -- never merely because the items were sent.

`LOITER` and `SET_MODE_LOITER` are two command_type names for the exact same
`command_executor.CommandSpec` object (`_LOITER_SPEC`), not two independently
maintained registry entries -- `SET_MODE_LOITER` matches the `SET_MODE_AUTO`/
`SET_MODE_MANUAL`/`SET_MODE_HOLD` naming convention (and is what production
operator traffic actually sends); `LOITER` is kept for backward compatibility
with the name this registry, this README, and the existing test suite have
used since LOITER was first added. Both reach `POST /nav/loiter`, the one
real vehicle Flask endpoint -- there is no parallel LOITER path. ArduRover
reports this mode back as `custom_mode=5`/`"LOITER"`
(`mode_verification.ARDUROVER_CUSTOM_MODES`/`ARDUROVER_MODE_NAMES`, the one
table `services/agent_state.py`'s `telemetry.mode_name` reuses rather than
keeping its own copy) -- this firmware has a distinct `HOLD` mode
(`custom_mode=4`) as well, so no HOLD/LOITER name normalization is needed;
if a future firmware ever collapsed the two, that normalization would belong
in `ARDUROVER_CUSTOM_MODES`, not here.

`SET_MODE_GUIDED` is intentionally absent -- no Flask endpoint exists yet for
ArduRover GUIDED, so it's rejected as unsupported until one is added. Mission
*upload* is now supported (`MISSION_UPLOAD`, above); mission download is a
read-only status surface (`GET /agent/pixhawk_mission`), while mission *clear*
and jump-to-waypoint remain deliberately **not** supported as operator
commands yet (they carry higher risk and need an explicit follow-up decision).

ARM/DISARM go through the exact same validate-then-execute path as every
other command type (expiry, duplicate command_id, support check) -- the
Local Agent adds no second confirmation gate of its own. They are now
**verified**: the vehicle Flask service (`services/arm_verification.py`) sends
`MAV_CMD_COMPONENT_ARM_DISARM`, matches its `COMMAND_ACK`, and confirms the
final armed state via fresh `HEARTBEAT` `base_mode` evidence -- never from the
request being sent or a fixed sleep. The Local Agent must never autonomously
ARM or DISARM; these only ever run in response to an explicit operator
command. Gating an ARM
command on operator/human confirmation is the operator backend's
responsibility (e.g. only ever enqueueing `ARM` after that confirmation has
happened); the Local Agent's job is to execute exactly what's queued for it,
exactly once, not to re-decide whether it should have been queued.

**Mode verification, shared by every mode-changing route (`services/mode_verification.py`)**:
none of `/nav/AutoModeOn`, `/nav/manual`, `/nav/hold`, `/nav/loiter`,
`/nav/rtl`, `/nav/pause` (**LOITER** -- the active safety hold, not HOLD), or
`/nav/resume` (AUTO) report success
merely because a `MAV_CMD_DO_SET_MODE` request was sent -- all seven call
the one function, `mode_verification.set_mode_and_verify(requested_mode_name,
...)`. There is no per-route MAVLink call left to hand-roll: each route
supplies a mode name (`"AUTO"`/`"MANUAL"`/`"HOLD"`/`"LOITER"`/`"RTL"`, looked
up in `mode_verification.ARDUROVER_CUSTOM_MODES` -- the one name<->int
table this module and `services/agent_state.py`'s `telemetry.mode_name`
both read, not two independently maintained copies of the same ArduRover
enum) and, optionally, its own timeout bounds (LOITER's are shorter -- see
below). `set_mode_and_verify` sends the request (`param1`=209 base_mode,
`param2`=the resolved custom_mode int, `target_system`/`target_component`
defaulting to 1/1 but overridable per call -- see "Future-proofing for
multiple USVs" below) and then polls `HEARTBEAT.custom_mode` for up to
`verify_timeout_s` to prove the Pixhawk actually entered that mode, then for
a further `stable_window_s` to prove it *held* rather than immediately
reverting (RC mode channel override, a failsafe, another GCS, or the Local
Agent's own autonomy all being possible causes). The route always returns
HTTP 200 with a structured body -- same convention as `POST /agent/set_home`
-- so the caller must inspect `accepted`/`verified`, never assume a `2xx`
means the mode change happened:

```json
{
  "previous_mode": 0,
  "requested_mode": "RTL",
  "requested_custom_mode": 11,
  "observed_mode": 11,
  "target_system": 1,
  "target_component": 1,
  "ack_result": "MAV_RESULT_ACCEPTED",
  "accepted": true,
  "verified": true,
  "reason": null,
  "samples": [0, 11, 11, 11, 11]
}
```

`accepted` is true once `HEARTBEAT.custom_mode` reports the requested value
at all; `verified` is additionally true only once it has held for the whole
stability window. `ack_result` is read from `COMMAND_ACK` when the Pixhawk's
firmware happens to ACK `MAV_CMD_DO_SET_MODE` (not every firmware version
does) but is never required for `verified` -- the HEARTBEAT readback is the
one authoritative signal, `ack_result` is corroborating evidence only.
`samples` is the raw sequence of `HEARTBEAT.custom_mode` values polled during
verification, kept for diagnosing exactly what the Pixhawk did (e.g. a
`[0, 11, 0]` sequence proves RTL was briefly entered and then reverted). This
result is threaded through unmodified as `command_result.result` -- see
"Command result delivery contract" above -- exactly like every other command
type's Flask response, so the operator backend can inspect `verified`/
`observed_mode`/`reason` directly rather than trusting `status: "executed"`
alone. `/nav/rtl` additionally emits an `RTL_COMMANDED` event on a verified
RTL or `RTL_NOT_VERIFIED` (with the failure `reason`) otherwise, visible in
`GET /agent/state`'s `events`.

Root cause this fixed: `/nav/rtl` previously posted `MAV_CMD_DO_SET_MODE`
with `param1=11, param2=0` -- `param1`/`param2` swapped relative to the four
*other* mode routes, each of which independently hand-rolled its own
(correct) copy of the same `param1=209`/`param2=<mode>` call with no ACK or
`HEARTBEAT.custom_mode` readback of its own either. Four duplicated call
sites made the fifth (RTL) an easy place for the params to silently drift
out of sync -- and unconditionally returning `{"status": "Returning home"}`
meant the route reported success on every call regardless of what the
Pixhawk actually did, which is why production command history showed `RTL`
repeatedly `EXECUTED` while the vehicle never actually returned home.
`mode_verification.py` is now the *one* place any route sends
`MAV_CMD_DO_SET_MODE` -- a future sixth mode route can't reintroduce this
class of bug by hand-rolling its own copy.

**Future-proofing for multiple USVs**: `target_system`/`target_component`
are parameters of every `set_mode_and_verify()` call
(`mode_verification.DEFAULT_TARGET_SYSTEM`/`DEFAULT_TARGET_COMPONENT` = 1/1,
the one vehicle every route in this codebase talks to today), not module-
level constants baked into a fixed mavlink2rest URL at import time --
mavlink2rest addresses vehicles by
`/mavlink/vehicles/{system_id}/components/{component_id}/...`, so a future
deployment fronting more than one `system_id` through the same mavlink2rest
(or a Pixhawk configured with a non-default `SYSID_THISMAV`) needs no
change to this module beyond passing a different `target_system`/
`target_component` -- the result always echoes back exactly which one it
used.

**Home-verification gate**: `SET_MODE_AUTO`/`RTL`/`RETURN_HOME`/
`MISSION_RESUME` (`command_executor.HOME_VERIFICATION_REQUIRED`) are
additionally rejected (`"home unverified: ... requires a verified Pixhawk
Home position"`) unless `GET /agent/home_status` on the vehicle Flask
service reports `verified: true` -- see "Set Home" below for why (an old/
garage Home could otherwise send the USV toward the wrong location) and for
how Home gets verified in the first place. `command_executor.home_verified()`
fails safe to `False` on any fetch error, so an unreachable vehicle Flask
service is never read as "verified". This is checked in `command_handler.py`
*after* the support check, so a request for one of these types while Home is
unverified is reported as `rejected` with a specific reason, not silently
dropped or reported as a generic execution `failed`. The vehicle Flask
service enforces the identical gate independently on `/nav/AutoModeOn`,
`/nav/rtl`, `/nav/resume` themselves (defense in depth against a caller that
bypasses the Local Agent entirely, e.g. the legacy direct operator web UI in
`motherpi/services/flask/static/index.html`). `LOITER`/`SET_MODE_MANUAL`/
`SET_MODE_HOLD`/`MISSION_PAUSE`/`ARM`/`DISARM` are never in
`HOME_VERIFICATION_REQUIRED` and must never be added to it -- LOITER in
particular is one of the most important safety commands and must remain
available even when Home is unverified.

**Flow** (`local_agent.py`'s `_poll_and_execute_commands`, run once per main
loop iteration, skipped entirely while control authority is `OPERATOR` -- see
"Control authority" above -- or while `comm_state == DISCONNECTED` since
there's nothing to poll against):

1. `api_client.get_pending_commands(usv_id)` -- `GET {operator}/agent/commands?usv_id=usv-2`,
   tried against each configured `OPERATOR_URLS` entry same as status sends.
   Returns `[]` (not an error) if no operator is reachable -- the operator
   backend owns the queue and keeps commands pending until the next
   successful poll.
2. For each returned command, `command_handler.process_command(command)`
   validates in order: malformed (missing `command_id`/`command_type`) ->
   expired (`expires_at` in the past) -> duplicate (`command_id` already in
   `command_log.jsonl`) -> unsupported type. A command_id is marked
   processed *before* execution is attempted, so a redelivery of that exact
   id is always rejected as duplicate even if execution itself later fails
   -- an operator-side retry is expected to arrive as a new command_id.
3. A supported, non-duplicate, non-expired command is executed by calling
   its mapped local Flask endpoint (`command_executor.call_local_endpoint`).
4. The result (`accepted`/`rejected`/`executed`/`failed`, with a reason) is
   POSTed back via `POST {operator}/agent/command_result`. If that send
   fails, the result is buffered through the same `buffer.py` mechanism
   used for status messages (routed by `message_type` on flush) rather than
   dropped.
5. Command-received/executed/rejected outcomes are also appended as local
   events, so they show up in the next status message's `events` group the
   same way `comm_recovered`/`mission_state_changed` do.

**Testing without a real operator backend**: `mock_operator.py` is a
stdlib-only fake implementing `GET /agent/commands`, `POST
/agent/command_result`, and `POST /test/queue` (to inject a test command).
Run it, point `OPERATOR_URLS` at it, and run the Local Agent normally.
`test_command_handler.py` covers the validation logic (expiry, dedup,
unsupported types, execution failure) without needing Flask/mavlink2rest/
Pixhawk at all -- run with `python3 test_command_handler.py`.

### Command result delivery contract (what the operator backend must implement)

This repo does not contain the operator backend -- `mock_operator.py` above
is the only implementation of the receiving side that exists here, and it is
the reference for the contract below. If a real operator backend responds
`405 Method Not Allowed` to `POST /agent/command_result`, that backend has
either not registered this route at all or registered it for a different
HTTP method (e.g. `GET`-only); nothing on the Local Agent side can fix a
405 -- it is purely a receiver-side gap.

**Endpoint**: `POST {operator_base_url}/agent/command_result`
(`api_client.send_to_operator("/agent/command_result", message)`, tried
against each configured `OPERATOR_URLS` entry in order, same failover as
every other outbound send -- see "Configuring the operator endpoint(s)"
below).

**Method**: `POST`, `Content-Type: application/json`, response body is not
parsed by the Local Agent beyond confirming a non-error HTTP status
(`response.raise_for_status()` in `api_client.send_to_operator`) -- any
`2xx` body is accepted.

**Request body** -- the same envelope every outbound message uses
(`models.make_message`), `payload` built by `command_handler.process_command()`:

```json
{
  "message_type": "command_result",
  "schema_version": "1.0",
  "source": "usv-2",
  "target": "operator",
  "timestamp": 1783852642.08,
  "payload": {
    "command_id": "c-123",
    "usv_id": "usv-2",
    "command_type": "SET_MODE_HOLD",
    "source": "operator",
    "status": "executed",
    "reason": "command executed successfully",
    "timestamp": 1783852642.10,
    "lifecycle": [
      {"status": "requested", "timestamp": 1783852642.05},
      {"status": "accepted", "timestamp": 1783852642.06},
      {"status": "executing", "timestamp": 1783852642.07},
      {"status": "executed", "timestamp": 1783852642.10}
    ],
    "result": {"status": "ok"}
  }
}
```

`payload.status` is one of `accepted` / `rejected` / `executed` / `failed`
(never a value outside that vocabulary); `payload.result` is only present
when the local Flask endpoint returned a body (`command_executor.call_local_endpoint`'s
result), omitted otherwise. Full field provenance is in `command_handler.py`.

**Buffering on failure**: if this POST fails (unreachable operator, timeout,
or an HTTP error status), the result is appended to `agent_buffer.jsonl` via
`buffer.buffer_message` and retried on the next flush
(`local_agent.py`'s `_send_buffered`, routed back to `/agent/command_result`
by `message_type`) -- same mechanism as a buffered status message, not
dropped. **Known limitation this session addressed**: prior to
`config.MAX_BUFFERED_MESSAGES`, a persistent route mismatch (every retry
405ing identically, not a transient connectivity gap) would grow that file
without bound, since nothing ever ages a buffered message out and the old
`flush_buffer` only retried on a comm reconnect edge. `buffer.py` now caps
the file at `MAX_BUFFERED_MESSAGES` (500, oldest dropped first), and
`local_agent.py`'s main loop now also retries the backlog on every iteration
while `comm_state == "CONNECTED"` and the buffer is non-empty (not only on a
PARTITIONED/DISCONNECTED -> CONNECTED edge), so a fixed endpoint gets
drained promptly instead of only at the next real disconnect/reconnect.
This bounds the failure mode but does not fix its root cause -- the receiver
side still needs the matching route.

## Richer status payload: MAVLink health, telemetry, transitions, command lifecycle

Every field below is a real read or explicitly `null`/`None` -- nothing is
invented. See the top-level project deliverable notes for the full
provenance table (MAVLink-direct vs Local-Agent-derived vs still needing
future hardware).

**`payload.mavlink`** (new top-level key in the status message, and
`vehicle_state["mavlink"]` from `GET /agent/state`) -- explicit MAVLink link
evidence instead of making the operator infer it from telemetry presence,
built by `services/mavlink_health.py` on the vehicle Flask side from the
same HEARTBEAT/GLOBAL_POSITION_INT/VFR_HUD/BATTERY_STATUS/GPS_RAW_INT
envelopes `agent_state.py` already fetches (no extra mavlink2rest calls):

```json
{
  "mavlink_connected": true,
  "heartbeat_age_s": 0.4,
  "mavlink_last_msg_age_s": 0.1,
  "mavlink_msg_rate_hz": 0.98,
  "parser_errors": null,
  "measured_at": 1783852642.08,
  "last_message_age_s": 0.1,
  "gps_fix_type": 3,
  "gps_satellites": 14,
  "vehicle_mode": "AUTO",
  "armed": true,
  "ekf_ok": true
}
```

`mavlink_connected` is `null` (not `false`) when HEARTBEAT has never been
cached at all -- this process can't distinguish "mavlink2rest is
unreachable" from "Pixhawk hasn't sent one yet" (same limitation
`agent_state.py`'s `_pixhawk_ok` already had). `mavlink_msg_rate_hz` is
measured from actual HEARTBEAT arrivals over a rolling window and stays
`null` until at least two distinct arrivals have been observed -- never
assumed from a nominal 1Hz. `parser_errors` is always `null`: mavlink2rest's
message-cache API doesn't expose a parse-error counter, so there is nothing
real to report yet.

**`last_message_age_s` through `ekf_ok`** (`decision_engine.build_mavlink_evidence()`,
`local_agent.py`) are a Local-Agent-side, purely additive merge on top of the
above -- nothing from `services/mavlink_health.py` is renamed or removed.
`last_message_age_s` is an explicit alias for `mavlink_last_msg_age_s` (same
value, same source, kept under both names since operator-side consumers may
look for either). `gps_fix_type`/`gps_satellites`/`vehicle_mode`/`armed`/
`ekf_ok` are the same MAVLink-derived reads `telemetry` already carries
(`gps_fix_type`/`gps_satellites`/`mode_name`/`armed`/`ekf_ok` there), copied
through so the Agent page's "is MAVLink evidence trustworthy right now"
question can be answered from one block instead of cross-referencing
`telemetry` separately. Every one of these is a direct read of the same
value `payload.agent.decision_inputs`/`payload.agent.situation.vehicle_health`
already use (`build_decision_inputs()`/`build_situation()`) -- never
re-derived differently in three places. Nothing here is inferred from an
unrelated field: if HEARTBEAT was never cached, `mavlink_connected` stays
`null` even though GPS/telemetry may be reporting live values from earlier
BATTERY_STATUS/GPS_RAW_INT traffic.

**`telemetry`** gained new fields alongside the existing `lat`/`lng`/`alt`/
`heading`/`groundspeed`/`battery`/`mode`/`armed` (unchanged, not duplicated
under a second name): `mode_name` (decoded ArduRover mode, e.g. `"AUTO"`,
falls back to `"MODE_<n>"` for an unmapped value rather than guessing),
`battery_voltage`/`battery_current` (summed cell voltages / current from
`BATTERY_STATUS`), `gps_fix_type`/`gps_satellites` (numeric, from
`GPS_RAW_INT`), `ekf_ok` (standard attitude+velocity+position EKF health
bitmask check, `null` if `EKF_STATUS_REPORT` was never cached), and
`airspeed` (always `null` -- Scout is a surface vehicle with no airspeed
sensor; `VFR_HUD.airspeed` is a synthetic ArduPilot value on a rover, not a
real measurement, so this deliberately never reports a fake 0).

**`payload.agent.decision_reason`** and **`payload.agent.last_transition`**
now reflect the actual most recent transition this process has recorded
(communication/mission/authority), not a static per-comm-state description
-- see `transition_reasons.py` for the concrete trigger text (e.g. "Final
waypoint reached (9/10); returning to base.", "Operator endpoint stopped
responding; VPN link still active, continuing mission with reduced
reporting."). Authority's reason comes from whoever called
`POST /agent/control_authority` on the vehicle Flask service (that route now
accepts an optional `"reason"` field, e.g. `"Operator explicitly requested
TAKE CONTROL"`, stored by `services/control_authority.py` and threaded back
through `agent.control_authority_last_transition`) -- the Local Agent only
ever observes control authority, so it reports the caller's own stated
reason rather than guessing operator intent.

**`payload.transitions`** -- a rolling audit trail (capped at 100,
`transition_log.py`) of every communication/mission/authority transition
this process has observed, each `{"timestamp", "type", "from", "to",
"reason"}`. Unlike `payload.events` (cleared on every successful send, capped
at 20), this is never cleared, so a reconnecting operator backend always
gets the full recent history, not just a delta since the last poll.

**Command lifecycle** -- `command_handler.py`'s result payload (POSTed to
`/agent/command_result`) now includes `source` (`command.source` /
`command.requested_by` from the operator backend, defaulting to
`"operator"`) and `lifecycle`, a list of `{"status", "timestamp"}` stages:
`requested -> accepted -> executing -> executed`/`failed`, or
`requested -> rejected`. Execution here is synchronous (one blocking HTTP
call to the local Flask service), so these stages happen within a single
`process_command()` call rather than being reported incrementally -- the
outer `status` field keeps the exact `accepted`/`rejected`/`executed`/
`failed` vocabulary already documented above, unchanged. `command_history.py`
additionally keeps a rolling record (last 50) of every command's lifecycle,
served read-only via `GET /agent/command_history` on the same diagnostics
HTTP server as below.

## Agent page reasoning (payload.agent: decisions, watch conditions, policy, confidence)

The operator station is being reorganized so the map page only exposes
essential vehicle/agent commands, while a separate **Agent page** becomes the
primary place to see *why* the Local Agent believes what it believes. This
section is Scout's side of that: everything below is new fields on the
existing `payload.agent` block of the status message (`local_agent.py`'s
`main()`, `decision_engine.py`), re-evaluated fresh every loop iteration from
observations already fetched that iteration -- no new mavlink2rest/Flask
calls, no UI text generated on the Scout side. The operator backend still
owns aggregation across USVs and the operator frontend still owns
presentation; nothing here decides how the Agent page renders.

```
Scout (this module)          Operator Backend        Operator Frontend
measurements (vehicle_state)
  -> observations (decision_inputs)
  -> decision (current_decision, decision_reason)
  -> reasoning (watch_conditions, current_policy, decision_confidence)
  -> transition history (transitions, decision_timeline)
                          -->  aggregation (fan-in across USVs)  -->  presentation (Agent page)
```

### Current decision

`payload.agent.current_decision` / `payload.agent.decision_reason` --
`decision_engine.decide()`'s single label for what the situation calls for
right now, and the concrete evidence behind it (never a boilerplate "state
changed"). Priority-ordered, first match wins, vehicle safety before mission
phase before authority deference:

| current_decision | Triggered by | decision_reason example |
|---|---|---|
| `Return Home` | `battery_percent` below `config.BATTERY_RTL_THRESHOLD_PERCENT` (30%), or `mission_state == RETURN` | `"Battery at 22% is below the 30% RTL threshold."` |
| `Hold Position` | `gps_fix_type` below `config.GPS_MIN_FIX_TYPE` (2), `mavlink_connected is False`, mission completed, `WAITING`/`ERROR`/no mission | `"GPS fix lost (fix_type=0); holding position rather than navigating without a reliable position estimate."` |
| `Pause Mission` | `control_authority == "OPERATOR"` while mission is `TRANSIT`/`SEARCH`/`RETURN` | `"Control authority is OPERATOR; Local Agent is standing by and will not relay mission-affecting commands until authority returns to LOCAL_AGENT."` |
| `Continue Search` | `mission_state == SEARCH` | `"Waypoint 4/10 reached; continuing search pattern."` |
| `Continue Mission` | `mission_state == TRANSIT` | `"Mission activated; heading to first waypoint."` |

Note `current_decision` is a **label the Agent page reads, not a command the
Local Agent executes** -- it never calls a `/nav/*` endpoint or changes
control authority. It only ever polls/relays commands that were already
queued by the operator backend (see "Operator -> USV commands" above), and
only while `control_authority == LOCAL_AGENT`.

Re-evaluated every iteration, so `decision_reason` always reflects *current*
evidence (e.g. an updated waypoint count) even when `current_decision`
itself hasn't changed. `transition_log.py` only records a `"decision"`
transition -- and only then does `payload.events` get a
`decision_changed` entry -- at the moment `current_decision` actually changes
label, so re-affirming the same decision every second doesn't spam the log.

### Decision inputs

`payload.agent.decision_inputs` -- the exact observations `decide()` read,
untouched (`decision_engine.build_decision_inputs()`). No UI summary is
computed here; every value is a direct field read or an explicit `null`:

| Field | Source |
|---|---|
| `communication_state`, `operator_reachable` | Local Agent's own `CommunicationMonitor` (`communication.py`) |
| `heartbeat_age_s`, `mavlink_connected` | **MAVLink-direct** -- `payload.mavlink`, built by the vehicle Flask side from HEARTBEAT (`services/mavlink_health.py`) |
| `battery_percent` | **MAVLink-direct, normalized** -- `telemetry.battery` (BATTERY_STATUS `battery_remaining`), passed through `decision_engine._normalize_battery_percent()`: `-1` (ArduPilot's "no charge estimate available" sentinel, e.g. power module disconnected) and any other value outside `0-100` become `null`. A `null` battery is excluded from the RTL check entirely (never compared against the threshold) and counts as a missing input for `decision_confidence` -- it must never read as "below threshold" or drive `Return Home`. |
| `gps_fix_type`, `gps_satellites` | **MAVLink-direct** -- `telemetry.gps_fix_type`/`gps_satellites` (GPS_RAW_INT) |
| `vehicle_mode` | **MAVLink-direct** -- `telemetry.mode_name` (decoded HEARTBEAT `custom_mode`) |
| `armed` | **MAVLink-direct** -- `telemetry.armed` (HEARTBEAT `base_mode` safety-armed bit) |
| `mission_state` | **Local-Agent-derived** -- `state_machine.MissionRunner` phase interpretation |
| `mission_id`, `mission_active`, `current_waypoint`, `mission_count` | Vehicle-owned mission identity/progress, passed through from `payload.mission` |
| `control_authority` | Vehicle Flask service (`services/control_authority.py`), read fresh each iteration |

### Current policy

`payload.agent.current_policy` (`decision_engine.build_policy()`), replacing
the old single-string `current_policy` field:

```json
{
  "communication_policy": "FULL_REPORTING",
  "mission_policy": "SUPERVISED_CONTINUATION",
  "autonomy_level": "ASSISTED",
  "current_behaviour": "monitoring"
}
```

`communication_policy` is `collectors.policy_for_comm()`'s existing
`FULL_REPORTING` / `REDUCED_REPORTING_LOCAL_AUTONOMY` /
`BUFFER_AND_LOCAL_FALLBACK` (unchanged). `mission_policy` is new:
`OPERATOR_DIRECTED` (authority is `OPERATOR` with an active mission),
`AUTONOMOUS_CONTINUATION_BUFFERED` (`DISCONNECTED`),
`AUTONOMOUS_CONTINUATION_REDUCED_REPORTING` (`PARTITIONED`), or
`SUPERVISED_CONTINUATION` (`CONNECTED` + `LOCAL_AGENT`, operator commands
relayed normally). `autonomy_level`/`current_behaviour` are the same
comm-state-driven values previously computed in `collectors.py` --
`decision_engine.py` is now their single source of truth so policy and
decision reasoning aren't duplicated in two modules.

### Watch conditions

`payload.agent.watch_conditions` (`decision_engine.build_watch_conditions()`)
-- the actual transition conditions `decide()` evaluates, each with the real
current value and threshold, not a static description. `triggered` is
`null` (not `false`) when the underlying reading is unavailable, so "not
triggered" is never confused with "unknown":

```json
[
  {"condition": "Battery < RTL threshold", "metric": "battery_percent", "current_value": 22, "threshold": 30, "comparator": "<", "triggered": true},
  {"condition": "Heartbeat timeout", "metric": "heartbeat_age_s", "current_value": 0.3, "threshold": 3.0, "comparator": ">=", "triggered": false},
  {"condition": "GPS lost", "metric": "gps_fix_type", "current_value": 3, "threshold": 2, "comparator": "<", "triggered": false},
  {"condition": "Mission completed", "metric": "mission_active", "current_value": true, "threshold": false, "comparator": "==", "triggered": false},
  {"condition": "Operator Take Control", "metric": "control_authority", "current_value": "LOCAL_AGENT", "threshold": "OPERATOR", "comparator": "==", "triggered": false}
]
```

### Decision confidence

`payload.agent.decision_confidence` -- `HIGH`/`MEDIUM`/`LOW`, purely a
measure of how complete `decide()`'s own inputs were (`battery_percent`,
`gps_fix_type`, `mavlink_connected`), not a verdict on whether the decision
is "correct". `payload.agent.decision_confidence_missing_inputs` names the
actual fields that were `null`, e.g. `["battery_percent"]`, so the Agent page
can show which reading is absent instead of only a bare label.

### Current situation

`payload.agent.situation` (`decision_engine.build_situation()`) -- a
structured pointer into evidence already fetched this iteration, for the
Agent page's top-of-page summary. No new computation and no invented overall
health verdict (that remains `GET /agent/diagnostics`' job):

```json
{
  "communication_state": "CONNECTED",
  "operator_reachable": true,
  "mission_state": "SEARCH",
  "control_authority": "LOCAL_AGENT",
  "vehicle_health": {
    "battery_percent": 22, "gps_fix_type": 3, "gps_satellites": 14,
    "ekf_ok": true, "mavlink_connected": true, "armed": true, "vehicle_mode": "AUTO"
  },
  "autonomy_level": "ASSISTED",
  "decision_confidence": "HIGH"
}
```

### Previous decision and decision timeline

`payload.agent.previous_decision` / `previous_decision_reason` -- the
`current_decision`/`decision_reason` in effect immediately before the most
recent change, so the Agent page can show "what changed from what" without
having to inspect the timeline. Both are `null` until the first decision
change since this Local Agent process started.

`payload.agent.decision_timeline` -- a rolling history (capped at 100
internally by `transition_log.MAX_TRANSITIONS`, same deque as
`payload.transitions`) of every `current_decision` change, each
`{"timestamp", "type": "decision", "from", "to", "reason"}`
(`transition_log.get_recent_by_type("decision")`). The operator station is
only expected to display a handful of the most recent entries; the full ~100
are kept so a reconnecting operator backend can still reconstruct recent
history, not just a delta since its last poll.

Also available on demand, same data, via the Local Agent's own inbound HTTP
server (see "Vehicle Health diagnostics" below for the rest of that server):

```bash
curl -s http://127.0.0.1:8090/agent/decision_timeline | python3 -m json.tool
```

## Vehicle Health diagnostics (GET /agent/diagnostics, POST /agent/system_check)

The Local Agent runs its own inbound HTTP server (`agent_server.py`,
`http.server.ThreadingHTTPServer`, same stdlib approach as
`mock_operator.py`) on `LOCAL_AGENT_HTTP_HOST:LOCAL_AGENT_HTTP_PORT`
(default `0.0.0.0:8090`, override via `LOCAL_AGENT_HTTP_PORT` env var) --
started as a daemon thread from `local_agent.py`'s `main()` alongside the
existing polling loop, so it doesn't block or get blocked by it. This is a
new listener; previously the Local Agent was a pure outbound client with no
server of its own (see "How this fits the rest of the architecture" below).

All endpoints on this server are **strictly read-only**: everything they
call is either a GET against the vehicle Flask API/mavlink2rest, a local
`/proc`,`/sys` read, an in-memory log read, or a ping. None of them can
reach a `/nav/*` write endpoint, change control authority, arm/disarm, or
change Pixhawk mode -- there is no code path from `agent_server.py` into
`command_executor.py`. RC/manual control is completely unaffected by
anything in this section.

```bash
curl -s http://127.0.0.1:8090/agent/diagnostics | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8090/agent/system_check | python3 -m json.tool
curl -s http://127.0.0.1:8090/agent/command_history | python3 -m json.tool
curl -s http://127.0.0.1:8090/agent/decision_timeline | python3 -m json.tool
```

Each `GET /agent/diagnostics` component now also carries raw evidence fields
alongside its `status`/`message` verdict (see "Keep architecture clean"
below) -- e.g. `pixhawk.available`/`pixhawk.age_s`, `gps.fix_type`/
`gps.satellites`, `battery.voltage`/`battery.current`/`battery.remaining`,
`local_agent.alive`/`local_agent.cpu_percent` (this process's own CPU usage,
`process_health.py`, distinct from the vehicle Flask host's cpu/memory
components below), `storage.free_percent`/`storage.used_percent`, and
`cpu.load1`/`cpu.cores`/`cpu.temp_c`. `status`/`message` are unchanged and
still authoritative for `system_check`; the evidence fields let the operator
backend derive its own PASS/WARN/FAIL from the numbers directly instead of
only ever seeing this module's own thresholding.

`GET /agent/command_history` (new, read-only, `command_history.py`) returns
`{"commands": [...]}`, the last 50 operator commands this process has
processed, each with its full lifecycle (see "Richer status payload" above)
-- lets the operator station show recent command outcomes even if it wasn't
listening when the corresponding `command_result` was pushed.

### GET /agent/diagnostics

Per-component health for the Vehicle Health page. Each component is
`{"status": ..., "measured_at": <unix seconds>, "message": "..."}` --
status one of `OK` / `WARNING` / `FAIL` / `UNKNOWN`, `message` optional,
`measured_at` is when *that* component was last evaluated (not necessarily
identical across components -- the Flask-owned ones are measured at the
time the vehicle Flask API answered `GET /agent/diagnostics`, a moment
before this response's own top-level `generated_at`). `UNKNOWN` always
means "could not be determined right now" -- no field is ever a guessed or
invented value.

Composed from two sources, merged by `diagnostics.py`:

| Component | Owner | How it's determined |
|---|---|---|
| `communication` | Local Agent | `communication.get_comm_state()` -- the same CONNECTED/PARTITIONED/DISCONNECTED signal already used for telemetry reporting cadence. **Real.** |
| `local_agent` | Local Agent | `runtime_status.py` -- seconds since `main()`'s loop last completed an iteration (OK &lt;10s, WARNING &lt;30s, FAIL beyond, UNKNOWN before the first iteration). Proves the polling loop hasn't stalled, not just that the HTTP thread is up. **Real.** |
| `network` | Local Agent | `communication.internet_ok()` (ping 8.8.8.8) / `vpn_ok()` (`wg show`). **Real, with a known caveat** -- see inline comment in `diagnostics.py`'s `_diag_network`: `vpn_ok()` can't currently distinguish "no handshake" from "the `wg show` command itself failed" (e.g. passwordless sudo not set up on this Pi); the latter case only surfaces as a one-time `print` in `communication.py`, not as `UNKNOWN` here. |
| `authority` | Local Agent | `GET /agent/control_authority` on the vehicle Flask API -- current control authority (`OPERATOR`/`LOCAL_AGENT`) as an observation, not a health verdict: `OK` if reachable (whichever value it reports), `UNKNOWN` if the vehicle Flask API can't be reached. Not to be confused with `system_check`'s stricter "Authority Service" check below, which `FAIL`s on unreachable instead, since that's a pre-deployment gate rather than an always-on status field. **Real.** |
| `mavlink` | Vehicle Flask API | `mavlink2rest_reachable()` -- direct `requests.get` against the mavlink2rest base URL, distinct from whether Pixhawk itself has ever sent a HEARTBEAT. **Real.** |
| `pixhawk` | Vehicle Flask API | HEARTBEAT message presence (mavlink2rest's own liveness signal, same one `health_service.py` already uses for `pixhawk.connected`). `UNKNOWN` if mavlink2rest itself is unreachable (can't know Pixhawk's state without the bridge). **Real.** |
| `gps` | Vehicle Flask API | GPS_RAW_INT `fix_type` (OK if 3D/DGPS/RTK, WARNING if 2D, FAIL if no fix, UNKNOWN if the message has never been cached by mavlink2rest). **Real.** |
| `battery` | Vehicle Flask API | BATTERY_STATUS `battery_remaining` thresholded at 30%/15%. **Real.** |
| `rc_receiver` | Vehicle Flask API | RC_CHANNELS `chancount` **and** freshness. An RF link/RSSI indication alone doesn't prove Pixhawk is getting RC input, and mavlink2rest never expires a cached message -- a transmitter switched off hours ago still answers with its last-known channel values. `OK` requires a non-zero `chancount` *and* `status.time.last_update` within the last 5s (`_RC_STALE_SECONDS`); a stale or timestamp-less response reports `UNKNOWN` rather than a false `OK`, and `chancount == 0` reports `WARNING`. **Real.** |
| `camera` | Vehicle Flask API | `service_reachable()` against the RealSense host services' own `GET /health` (`realsense.py`), one per stream (rgb/depth). **Real**, but only reachability, not stream quality/framerate. |
| `mission_service` | Vehicle Flask API | Whether the vehicle Flask process can currently compute its own mission block (`mission_active`/`current_mission_id`/waypoint progress) without raising. There is no standalone "mission service" anywhere in this codebase to check independently of the Flask process itself -- see `diagnostics_service.py`'s `_diag_mission_service` docstring. **Effectively "is this Flask process alive," not a distinct dependency.** |
| `storage` / `cpu` / `memory` | Vehicle Flask API | Manual `/proc`,`/sys` reads (`health_service.py`, no `psutil` per repo convention), same numbers already surfaced in `GET /agent/state`'s `health` block, now thresholded into OK/WARNING/FAIL here. **Real**, and specifically the vehicle Flask host's stats, not the Local Agent process's own (they run on the same Pi, so in practice this is the same machine either way). |

If the vehicle Flask API (`LOCAL_FLASK_URL`) can't be reached at all, all ten
Flask-owned components report `UNKNOWN` with a message naming the failure --
never guessed as `OK` or `FAIL`.

### POST /agent/system_check

A quick pre-deployment readiness check, built from the same
`build_diagnostics()` call plus two additional read-only fetches
(`GET /agent/state` for telemetry, `GET /agent/control_authority` for the
authority check) -- see `diagnostics.build_system_check()`. Response shape:

```json
{
  "overall": "PASS",
  "checks": [
    {"name": "MAVLink2Rest Reachability", "status": "PASS"},
    {"name": "Pixhawk Heartbeat", "status": "PASS", "message": "heartbeat received"},
    {"name": "Local Agent", "status": "PASS", "message": "main loop iterated 0.4s ago"},
    {"name": "Telemetry", "status": "PASS", "message": "lat=... lng=..."},
    {"name": "GPS", "status": "PASS"},
    {"name": "Authority Service", "status": "PASS", "message": "responding, authority=OPERATOR"}
  ],
  "generated_at": 1783852642.08,
  "started_at": 1783852637.81,
  "finished_at": 1783852642.16,
  "duration_seconds": 4.35
}
```

Per-check status is `PASS` / `WARN` / `FAIL` / `UNKNOWN` (mapped 1:1 from the
diagnostics vocabulary: `OK`→`PASS`, `WARNING`→`WARN`). `overall` is the
worst status across all checks, with `FAIL` &gt; `WARN` &gt; `UNKNOWN` &gt;
`PASS` -- an unresolved `UNKNOWN` deliberately still blocks a clean `PASS`,
on the theory that "we couldn't check it" shouldn't read the same as "it's
fine" on a pre-deployment gate. `started_at`/`finished_at`/`duration_seconds`
bound the whole check run (not per-check); a `duration_seconds` climbing well
past the sub-second norm is itself a signal something downstream is slow to
answer.

This performs **no vehicle movement, no mode changes, no arming, and no RC
interaction** -- it never calls anything in `command_executor.py`, and
`test_diagnostics.py`'s `test_never_calls_a_write_endpoint` patches
`command_executor.call_local_endpoint` to raise and asserts the check still
passes cleanly, proving that code path is never exercised.

## Pixhawk Mission (GET /agent/mission)

Read-only download of the mission actually stored on the Pixhawk right now
-- for the operator station's Pixhawk Mission card. **Never uploads,
modifies, deletes, or overwrites a mission**; every write-capable mission
message (`MISSION_COUNT`, `MISSION_ITEM_INT`, `MISSION_CLEAR_ALL`,
`MISSION_ACK`) is exclusively used by the existing `/nav/upload_mission` /
`/nav/clear_mission` write endpoints, never by this path.

```bash
curl -s http://127.0.0.1:8090/agent/mission | python3 -m json.tool
```

Same two-layer split as diagnostics:

- **Vehicle Flask API** (`services/mission_service.py` there, exposed as its
  own `GET /agent/mission`) does the real work: the actual MAVLink
  mission-download handshake against mavlink2rest --
  `MISSION_REQUEST_LIST -> MISSION_COUNT`, then
  `MISSION_REQUEST_INT(seq) -> MISSION_ITEM_INT(seq)` for every `seq` in
  `range(count)`. mavlink2rest never expires a cached message on its own
  (same limitation documented in `mavlink_message_utils.py`), so a naive
  "post the request, sleep a fixed amount, read whatever's cached" -- the
  pattern the older `/nav/fetch_mission` and `/nav/mission_count` routes in
  `app.py` use -- can silently return a stale response instead of the one
  just requested. This module instead waits for each message's
  `status.time.last_update` token to actually change before accepting it,
  and additionally confirms a returned `MISSION_ITEM_INT.seq` matches the
  `seq` just requested, with bounded per-message timeout/retry and an
  overall 20s cap on the whole download -- a partial/timed-out download
  still returns whatever waypoints it did confirm (flagged via
  `mission_valid: false` and `error`) rather than being discarded.
- **Local Agent** (`mission.py` here) is a thin resilience wrapper: it adds
  `last_fetch_age` (seconds since the last *fully valid* mission download
  succeeded) and, if the vehicle Flask API is briefly unreachable or a
  download times out mid-transfer, falls back to the last confirmed-good
  mission instead of leaving the operator card blank -- always clearly
  marked via a non-null `error` naming the failure, never silently
  presented as fresh. A live (if degraded, e.g. `mission_valid: false`)
  response from the vehicle Flask API is always surfaced as-is and never
  masked by the cache; the cache only kicks in when that API couldn't be
  reached at all.

### Response schema

```json
{
  "available": true,
  "reachable": true,
  "fetched_at": 1783852642.08,
  "mission_count": 6,
  "current_waypoint": 2,
  "home_position": {"latitude": 57.1234567, "longitude": 11.1234567, "altitude": 12.3},
  "waypoints": [
    {
      "sequence": 0,
      "latitude": 57.1234567,
      "longitude": 11.1234567,
      "altitude": 0.0,
      "command": "MAV_CMD_NAV_WAYPOINT",
      "frame": "MAV_FRAME_GLOBAL_RELATIVE_ALT",
      "autocontinue": true,
      "loiter_time": 5
    }
  ],
  "mission_loaded": true,
  "mission_valid": true,
  "last_fetch_age": 0.4,
  "error": null,
  "mission_hash": null,
  "mission_version": null,
  "schema_version": 1
}
```

- `available` -- a download was actually attempted and answered (even a
  0-waypoint mission is `available: true`); `false` only when mavlink2rest
  was unreachable or `MISSION_COUNT` never arrived at all.
- `reachable` -- mavlink2rest itself answered, independent of whether the
  mission download succeeded.
- `mission_loaded` -- `mission_count > 0` (a real, non-empty mission is on
  the Pixhawk); `null` when `available` is `false`.
- `mission_valid` -- every waypoint from `0` to `mission_count - 1` was
  confirmed (`len(waypoints) == mission_count` and no timeout occurred);
  `false` on a partial download, `null` when `available` is `false`.
- `home_position` -- best-effort, cache-only read of `HOME_POSITION`.
  Unlike `MISSION_COUNT`/`MISSION_ITEM_INT`, ArduPilot only broadcasts this
  once when home/EKF origin is set, not on a repeating stream, so there is
  no "request and wait for a fresh reply" handshake to perform here (see
  "MAVLink limitations" below) -- `null` can mean either "no home set" or
  "mavlink2rest hasn't received one since it last restarted", and this
  service cannot currently tell those apart.
- `mission_hash` / `mission_version` -- reserved for future support
  (comparing the Pixhawk's mission against the operator's own copy);
  always `null` today, same convention as `mavlink_health.py`'s always-null
  `parser_errors` -- present now so populating them later doesn't change
  this response's shape.
- `schema_version` -- bump this if a future change to this schema isn't
  purely additive, so a consumer can tell old/new shapes apart.

Any field this service could not determine is `null` -- never guessed or
invented, same convention as `GET /agent/diagnostics`.

### MAVLink limitations

- **No MISSION_ACK.** The real MAVLink mission-download handshake
  conventionally ends with the requester sending `MISSION_ACK` to close the
  transfer on the autopilot's side. This module does not send one: `MISSION_ACK`'s
  own field is itself named `type` (the ack result, e.g.
  `MAV_MISSION_ACCEPTED`), which collides with mavlink2rest's REST
  convention of using the top-level `type` key for the *message name*
  itself, and the exact resolution of that collision in mavlink2rest's
  JSON schema is unverified in this codebase (no example of it exists
  anywhere here). Omitting it is safe for a read-only download -- nothing
  is written, so there's nothing to corrupt -- but ArduPilot may log an
  internal mission-transfer timeout after answering. Flagged as a known
  follow-up rather than guessed at.
- **No shared connection, no cross-request lock at the mavlink2rest
  layer.** mavlink2rest itself is a separate process (not in this repo)
  and owns the one real MAVLink link; `mission_service.py` only serializes
  concurrent calls to its own `download_mission()` within this Flask
  process (a single gunicorn worker, `gunicorn_config.py`) via an in-process
  lock, so two overlapping `GET /agent/mission` calls can't interleave
  `MISSION_REQUEST_INT`/`MISSION_ITEM_INT` traffic against each other. It
  does **not** serialize against `/nav/upload_mission`'s own internal
  verification fetch, or against `/nav/fetch_mission`/`/nav/mission_count`
  -- those older routes have no lock of their own (pre-existing, not
  introduced by this feature) and could still race a concurrent
  `/agent/mission` call's requests/responses against the same mavlink2rest
  cache.
- **`current_waypoint` and `home_position` are cache reads, not
  request/response round-trips.** `MISSION_CURRENT` is a periodic
  ArduPilot broadcast and `HOME_POSITION` a one-time broadcast on
  home/EKF-origin change -- neither has a `*_REQUEST_*` counterpart the
  way `MISSION_COUNT`/`MISSION_ITEM_INT` do, so this service cannot force a
  fresh answer for either; it reports whatever mavlink2rest has most
  recently cached, or `null` if nothing has arrived yet.

### Remaining dependencies

- Depends on mavlink2rest already exposing `HOME_POSITION` in its message
  cache the same way it does `HEARTBEAT`/`GPS_RAW_INT`/etc -- unverified
  against a live mavlink2rest instance (no live Pixhawk/mavlink2rest in
  this dev environment); if it doesn't, `home_position` simply stays
  `null`, which is still a correct (if less useful) answer.
- `mission_hash`/`mission_version`/mission comparison against the
  operator's own copy are schema placeholders only -- no hashing or
  comparison logic exists yet.

## Set Home (SET_HOME operator command)

**Root problem**: ArduPilot mission sequence 0 always reflects Pixhawk
`HOME_POSITION`, set once (typically at the garage, on power-up) and never
moved automatically when a new mission is later uploaded for a different
launch site (e.g. a lake). `HOME_POSITION` stayed at the garage even though
the uploaded mission was at the lake -- `AUTO`/`RTL`/`MISSION_RESUME`
executed against that stale Home would navigate relative to, or return to,
the wrong location. The fix: an explicit "Set Home Here" action the operator
performs at each deployment's safe recovery point, verified end-to-end
(not just "the MAVLink command was sent") before `AUTO`/`RTL`/`MISSION_RESUME`
are allowed to run.

**The Operator Backend is the only thing the frontend/operator UI ever
talks to.** SET_HOME is not a special case: it is queued by the operator
backend and executed exactly like ARM/RTL/every other command type --
`api_client.get_pending_commands()` (`GET {operator}/agent/commands`) ->
`command_handler.process_command()` (expiry/dedup/support/home-verification
checks, identical order to every other type -- see the command table above)
-> `command_executor.py` -> the vehicle Flask service's `POST
/agent/set_home` -> result pushed back via `POST /agent/command_result`,
same as any other command's result. The Local Agent has **no inbound HTTP
surface of its own** for this (see `agent_server.py`'s docstring, strictly
read-only) -- there is no way for a browser or operator UI to reach Scout's
Set Home flow except through the operator backend's existing queue.

The one real difference from every other command_type: its vehicle Flask
endpoint needs a JSON body (`command_id` + `params`), not a bare call, since
`services/set_home_service.py` there needs `command_id`/`mode`/
`tolerance_m`/`freshness_s` to do its own verification work. There is still
exactly one execution function, `command_executor.call_local_endpoint(command)`
-- every command_type is described declaratively in `ALLOWED_COMMANDS` by a
`CommandSpec(method, path, build_body, timeout)`, and `call_local_endpoint`
branches on whether that spec carries a `build_body` callable (SET_HOME's
is the only one today) rather than on the command_type itself. A future
command type that needs a body is "write a small `build_body` function and
reference it in its `CommandSpec`" -- never a new function, a new registry,
or a new branch in `command_handler.py`. Every other command type's
`CommandSpec` has `build_body=None` and is completely unaffected.

**Command shape** the operator backend queues (`GET /agent/commands`
response, same envelope as every other command type):

```json
{
  "command_id": "unique-id",
  "usv_id": "usv-2",
  "command_type": "SET_HOME",
  "issued_at": 1783970000.0,
  "expires_at": 1783970060.0,
  "params": {"mode": "current_position", "tolerance_m": 5.0, "freshness_s": 3.0},
  "requested_by": "operator"
}
```

`params.mode` is currently required to be `"current_position"` -- the
Scout's own fresh GPS position; `params.tolerance_m`/`params.freshness_s`
are optional overrides of the safe defaults (see "Configurable thresholds"
below). Validation order (`command_handler.process_command()`, identical to
every other command type): malformed (missing `command_id`/`command_type`)
-> expired (`expires_at` in the past) -> duplicate (`command_id` already in
`command_log.jsonl`, the same dedup store every command type uses) ->
unsupported `command_type` -> (SET_HOME is never in
`HOME_VERIFICATION_REQUIRED` -- see below, would be circular). A
`command_id` is marked processed *before* the vehicle Flask service is ever
called, so a redelivery of that exact id is always rejected as duplicate
even if the actual attempt then fails -- an operator retry (a fresh "Set
Home Here" press) is expected to be queued as a new `command_id`.

The vehicle Flask service's flow (`set_home_service.set_home_current_position()`):

1. `mavlink2rest`/Pixhawk must be reachable.
2. Read the Scout's own current `GLOBAL_POSITION_INT`; it must be fresh
   (age <= `freshness_s`, default 3.0s) and a valid, non-zero lat/lon.
3. Send `MAV_CMD_DO_SET_HOME` (`param1=1`, "use current location" -- the
   Pixhawk uses its own live position estimate) and wait for a matching
   `COMMAND_ACK` (only one naming `MAV_CMD_DO_SET_HOME` is accepted --
   mavlink2rest caches exactly one `COMMAND_ACK` regardless of which
   command it acks); it must be `MAV_RESULT_ACCEPTED`.
4. Force and wait for an updated `HOME_POSITION` broadcast
   (`MAV_CMD_GET_HOME_POSITION` -- `HOME_POSITION` has no
   `*_REQUEST_*` counterpart of its own, see "MAVLink limitations" above).
5. The updated `HOME_POSITION` must be within `tolerance_m` (default 5.0m)
   of the position read in step 2.

Every `COMMAND_ACK`/`HOME_POSITION` reply is proven fresh the same way
`mission_service.py` proves `MISSION_COUNT`/`MISSION_ITEM_INT` fresh (a
`status.time.last_update` token that changed since immediately before the
request, *and* an arrival time not earlier than when the request was sent)
-- mavlink2rest never expires a cached message, so a naive "post, sleep,
read whatever's cached" could silently accept a stale or unrelated reply.

Only when every condition above holds does the response report
`accepted: true, verified: true` and the vehicle Flask service's
verification latch get set. Never uploads, modifies, clears, or renumbers
the mission -- Home (mission sequence 0) and the executable mission items
(sequence 1..N) are handled by entirely separate MAVLink messages; no
`MISSION_*` message is ever posted by this flow, and nothing here ever runs
automatically during mission upload/download or on startup.

This is the vehicle Flask service's raw response, which lands unchanged as
`payload.result` in the `POST /agent/command_result` push
(`command_handler.py`'s `_result("executed", ..., extra=flask_result)` --
identical to how any other command_type's `result` block is populated). The
operator backend is responsible for inspecting `result.accepted`/
`result.verified`/`result.error` itself, exactly as it would for any other
command's result -- the command-protocol `status` field (`executed`/
`failed`/`rejected`) only reflects whether the HTTP call itself succeeded,
not whether Home was actually verified.

**Success response**:

```json
{
  "accepted": true,
  "verified": true,
  "command_id": "unique-id",
  "requested_position": {"latitude": 56.6505, "longitude": 12.8707},
  "home_position": {"latitude": 56.6505, "longitude": 12.8707, "altitude": 1.2},
  "verification_distance_m": 1.4,
  "ack_result": "MAV_RESULT_ACCEPTED",
  "error": null
}
```

**Failure response** (every field actually determined by the point of
failure is populated; only what genuinely wasn't reached is `null`):

```json
{
  "accepted": false,
  "verified": false,
  "command_id": "unique-id",
  "requested_position": null,
  "home_position": null,
  "verification_distance_m": null,
  "ack_result": null,
  "error": {"code": "POSITION_STALE", "message": "Current vehicle position is 4.2s old, exceeding the 3.0s freshness threshold."}
}
```

`error.code` is one of: `PIXHAWK_UNREACHABLE`, `POSITION_UNAVAILABLE`,
`POSITION_STALE`, `INVALID_POSITION`, `ACK_REJECTED`, `ACK_TIMEOUT`,
`HOME_POSITION_TIMEOUT`, `VERIFICATION_TOLERANCE_EXCEEDED`, or
`UNSUPPORTED_MODE` (vehicle Flask service). Command-protocol-layer
rejections (malformed/expired/duplicate/unsupported `command_type`) never
reach the vehicle Flask service at all -- those are reported the same way
every other command_type's rejections are, via `payload.status ==
"rejected"` and `payload.reason` on the `command_result` push, with no
`result` block.

### Home verification/readiness

Unlike diagnostics/mission (each their own `GET /agent/*` endpoint, polled
directly against this process's own port -- see "Vehicle Health
diagnostics" above), Home verification/readiness is **not** a separate
endpoint here: it rides the existing status push instead, so the frontend
never has anything new to poll directly against Scout. The vehicle Flask
service's `GET /agent/home_status` (`services/set_home_service.py`'s
`get_home_status()`) is folded into `services/agent_state.py`'s
`GET /agent/state` response there (`vehicle_state["agent"]["home_status"]`)
and passed straight through into this process's own status payload
(`payload.agent.home_status`, `local_agent.py`'s main loop) on every
`POST /agent/status` push -- the same idiom already used for
`control_authority`. `command_executor.home_verified()` (the AUTO/RTL/
RESUME gate) still queries the vehicle Flask service's `GET
/agent/home_status` directly and independently -- that is a Local-Agent-
internal call to the vehicle Flask service, never exposed on this
process's own inbound HTTP surface, and unrelated to what the operator
backend/frontend sees via `payload.agent.home_status`.

```json
{
  "reachable": true,
  "vehicle_position": {"latitude": 56.6505, "longitude": 12.8707, "age_s": 0.4},
  "home_position": {"latitude": 56.6505, "longitude": 12.8707, "altitude": 1.2, "age_s": 12.1, "source": "pixhawk"},
  "distance_from_vehicle_m": 0.9,
  "verified": true,
  "verified_at": 1783970001.2,
  "verification_method": "set_home_current_position",
  "verification_distance_m": 1.4,
  "ready_for_auto": true,
  "ready_for_rtl": true,
  "reason": null
}
```

`distance_from_vehicle_m` is **informational only, never a gating input** --
the expected outcome of a mission is the vehicle driving far away from its
verified recovery point, and that must never be mistaken for Home being
wrong. `ready_for_auto`/`ready_for_rtl` are always identical today (both
gate on the same `verified` latch) and are exactly what `command_executor.home_verified()`
(Local Agent) and `/nav/AutoModeOn`/`/nav/rtl`/`/nav/resume` (vehicle Flask
service) check.

`verification_distance_m` is a different distance from `distance_from_vehicle_m`
above -- it is the distance between the requested position and the
post-`MAV_CMD_DO_SET_HOME` `HOME_POSITION` reading from the Set Home attempt
that last latched `verified`, persisted alongside `verified`/`verified_at`
(see `services/set_home_service.py`'s `_verified_distance_m`). It is `null`
whenever `verified` is `false`, and is otherwise carried unchanged from that
successful attempt's own `verification_distance_m` (see "Success response"
above) -- it is not recomputed against the vehicle's current position.

### Verification latch: what "verified" actually means

`verified` is **not** "Home's coordinates happen to be close to something
reasonable right now" -- coordinates alone can never prove a real Set Home
operation ran; a stale Home could coincidentally sit near the vehicle by
chance. It is a runtime latch (`set_home_service.py`, module-level, in the
vehicle Flask service -- same in-memory-only lifetime convention as
`services/control_authority.py`):

- **Set** only by a fully successful `set_home_current_position()` call.
- **Cleared** when:
  - mavlink2rest reports the Pixhawk rebooted/reconnected -- detected via
    `GLOBAL_POSITION_INT.time_boot_ms` (milliseconds since flight-controller
    boot) going backwards, since `HEARTBEAT` itself carries no boot counter;
  - the live `HOME_POSITION` reading drifts more than `HOME_DRIFT_INVALIDATION_M`
    (default 15.0m, deliberately looser than the 5.0m verification tolerance
    to avoid re-triggering on GPS jitter) from the position last confirmed,
    without an intervening successful Set Home -- something other than this
    module moved Home.
- **Never** cleared merely by the vehicle's current position moving away
  from Home.
- **Not** reset by a Local Agent process restart -- verification state
  lives in the vehicle Flask service, which this process only ever queries
  over HTTP (`GET /agent/home_status`); restarting the Local Agent safely
  reconstructs its view by just calling that again.
- **Is** reset when the vehicle Flask service itself restarts (in-memory
  only) -- a fresh process cannot honestly know whether a Set Home actually
  completed before it started.

### Configurable thresholds

`DEFAULT_POSITION_FRESHNESS_S` (3.0s), `DEFAULT_VERIFICATION_TOLERANCE_M`
(5.0m), and `HOME_DRIFT_INVALIDATION_M` (15.0m) in `set_home_service.py` are
the safe defaults; `params.tolerance_m`/`params.freshness_s` on the queued
SET_HOME command override the first two per call, threaded through
SET_HOME's `CommandSpec.build_body` (`command_executor._set_home_body()`) ->
`command_executor.call_local_endpoint()` -> `POST /agent/set_home`.

### Structured logs

Every attempt logs (module `set_home_service.py`, `[SET_HOME]` prefix):
received request, position selected, Home before the command, `COMMAND_ACK`
result, Home after, verification distance, and final success/failure --
enough to reconstruct exactly what happened from the vehicle Flask
service's own log without needing to reproduce the failure.

## Pixhawk Mission card (GET /agent/pixhawk_mission)

The schema the operator station's Pixhawk Mission card actually consumes.
The operator backend is already implemented as a proxy for this endpoint;
this section -- `services/mission_service.py`'s `download_pixhawk_mission()`
on the vehicle Flask side, `pixhawk_mission.py` here -- is "the real Scout
side" it proxies to. Same underlying MAVLink download as `GET /agent/mission`
above (one download implementation, not two -- both share
`_fetch_mission_count`/`_fetch_mission_item`/`_download_items`), reshaped
into the schema below and extended with mission-content validation and a
comparison-ready hash. **Read-only. Upload is explicitly out of scope for
this endpoint** -- see "Ownership of mission state" below.

```bash
curl -s http://127.0.0.1:8090/agent/pixhawk_mission | python3 -m json.tool
```

### Response schema

```json
{
  "mission_loaded": true,
  "mission_valid": true,
  "count": 6,
  "current_seq": 2,
  "hash": "3b1c...e9a2",
  "waypoints": [
    {
      "sequence": 0,
      "latitude": 57.1234567,
      "longitude": 11.1234567,
      "altitude": 0.0,
      "command": "MAV_CMD_NAV_WAYPOINT",
      "frame": "MAV_FRAME_GLOBAL_RELATIVE_ALT",
      "autocontinue": true,
      "loiter_time": 5,
      "param1": 5, "param2": 0, "param3": 0, "param4": 0
    }
  ],
  "partial": false,
  "duplicate_sequences": [],
  "invalid_sequences": [],
  "unsupported_sequences": [],
  "error": null,
  "reachable": true,
  "fetched_at": 1783852642.08,
  "last_fetch_age": 0.4,
  "schema_version": 1
}
```

The exact fields the operator's proxy contract names (`mission_loaded`,
`mission_valid`, `count`, `current_seq`, `hash`, `waypoints`, `partial`) are
always present; everything else (`duplicate_sequences` onward) is additive,
for operator-side debugging of *why* `mission_valid` is `false` -- a proxy
that only reads the named fields is unaffected by their presence.

### MAVLink messages used

Exactly the same handshake as `GET /agent/mission`, no additions:

| Step | Message posted | Message read back |
|---|---|---|
| 1 | `MISSION_REQUEST_LIST` (`target_system=1`, `target_component=1`, `mission_type=MAV_MISSION_TYPE_MISSION`) | `MISSION_COUNT` |
| 2 (repeated for `seq` in `0..count-1`) | `MISSION_REQUEST_INT(seq)` | `MISSION_ITEM_INT(seq)` |

Additionally, cache-only reads with no corresponding request message
(see "MAVLink limitations" under `GET /agent/mission` above):
`MISSION_CURRENT` (for `current_seq`) and `HEARTBEAT` (only indirectly, via
`mavlink2rest_reachable()`'s base-URL probe -- not a per-item read).
`HOME_POSITION` is fetched internally by the shared core but is not part of
this endpoint's response schema (unlike `GET /agent/mission`, which does
expose `home_position`).

No `MISSION_ACK` is sent -- see "MAVLink limitations" under `GET
/agent/mission` above for why (the ack's own `type` field collides with
mavlink2rest's message-name convention, unverified in this codebase).

### Download sequence

1. Check `mavlink2rest_reachable()` (a plain HTTP GET against mavlink2rest's
   base URL). If unreachable: return immediately with `reachable: false`,
   everything else `null`/`[]`, `error` naming the failure. No MAVLink
   traffic is attempted.
2. Read `MISSION_CURRENT` (cache-only) for `current_seq`.
3. `_fetch_mission_count()`: post `MISSION_REQUEST_LIST`, then poll
   `MISSION_COUNT` until its `status.time.last_update` token changes (a
   *new* arrival, not whatever was already cached -- see next section and
   `GET /agent/mission`'s docstring for why this matters), up to
   `_COUNT_MAX_RETRIES` (3) attempts. If `count` is never obtained: return
   with `mission_loaded`/`mission_valid`/`count`/`hash` all `null`,
   `partial: true`, `error` naming the timeout.
4. For `seq` in `range(count)`: `_fetch_mission_item(seq)` posts
   `MISSION_REQUEST_INT(seq)`, then polls `MISSION_ITEM_INT` until a
   **fresh** reply arrives whose own `seq` field equals the one requested,
   up to `_ITEM_MAX_RETRIES` (3) attempts. A decodable item is appended to
   `waypoints`; a reply that arrived but couldn't be decoded (missing
   `seq`/`command`) is recorded in `unsupported_sequences` instead, and the
   loop continues to the next `seq` rather than aborting. If no matching
   reply ever arrives for a `seq` (or the overall deadline is hit first),
   the loop stops there and `partial` becomes `true`.
5. Post-processing on whatever `waypoints` were collected:
   `duplicate_sequences` (structurally unreachable via this per-seq-request
   design today, kept as a defensive check -- see `_find_duplicate_sequences`'s
   docstring), `invalid_sequences` (out-of-range/missing lat-lon on
   position-bearing commands only -- see "Error handling" below).
6. `mission_valid = not partial and no duplicates and no invalid
   coordinates and no unsupported items and len(waypoints) == count`.
7. `hash = compute_mission_hash(waypoints)` **only if** `mission_valid` is
   `true` -- see "Hash generation" below for why a partial/inconsistent
   read never gets a hash.

### Timeout behaviour

Two independent bounds, both enforced with real wall-clock deadlines (not
just a retry counter, so a slow-but-not-dead link can't consume unbounded
time across many retries):

- **Per-message**: each `MISSION_COUNT` wait is capped at `_COUNT_TIMEOUT_S`
  (2.0s) per attempt, up to `_COUNT_MAX_RETRIES` (3) attempts; each
  `MISSION_ITEM_INT` wait is capped at `_ITEM_TIMEOUT_S` (1.5s) per attempt,
  up to `_ITEM_MAX_RETRIES` (3) attempts.
- **Overall**: a single `_OVERALL_TIMEOUT_S` (20.0s) deadline set once at
  the start of the download, which every per-message wait is additionally
  clamped to (`min(overall_deadline, ...)`). A mission with many waypoints
  can't blow through this by exhausting per-item retries one at a time --
  the whole download gives up and returns whatever it has once the overall
  deadline passes, regardless of which step it's in.

A timeout never blocks indefinitely and never fabricates the missing data
-- it returns exactly what was actually confirmed, with `partial: true` and
`error` naming what didn't arrive in time. `api_client.get_pixhawk_mission()`
(the Local Agent's outbound call to this endpoint) uses a 25s HTTP read
timeout, deliberately longer than the Flask side's 20s internal cap, so a
download that would have finished normally on the Flask side is never cut
off client-side first.

### Error handling

Every failure mode returns valid JSON with real values or explicit `null`
-- never a guessed value, and never an unhandled exception past the Flask
route's own `try/except -> 500` (which itself should not trigger under
normal operation, since `download_pixhawk_mission()` catches its own
failure modes internally):

| Condition | Result |
|---|---|
| mavlink2rest unreachable | `reachable: false`, everything else `null`/`[]`, `error` set |
| `MISSION_COUNT` never arrives | `mission_loaded`/`mission_valid`/`count`/`hash`: `null`, `partial: true`, `error` set |
| Download stops mid-mission (timeout, or no reply for some `seq`) | `waypoints` holds whatever was confirmed before the stop, `partial: true`, `mission_valid: false`, `hash: null`, `error` names which `seq`/how many |
| A `MISSION_ITEM_INT` arrives but can't be decoded (missing `seq`/`command`) | its `seq` is recorded in `unsupported_sequences`, download continues to the next `seq`, `mission_valid: false`, `hash: null` |
| A position-bearing waypoint's latitude/longitude is missing or out of `[-90,90]`/`[-180,180]` | its `seq` is recorded in `invalid_sequences`, item is still returned in `waypoints` (data the vehicle actually reported is never silently dropped), `mission_valid: false`, `hash: null` |
| Duplicate `sequence` values in the assembled `waypoints` list | recorded in `duplicate_sequences`, `mission_valid: false`, `hash: null` (see the download-sequence step 5 note on why this is currently unreachable but still checked) |
| Local Agent can't reach the vehicle Flask API at all | `pixhawk_mission.py` falls back to the last confirmed-`mission_valid` result if one exists (marked via `error` containing "showing last known mission" and a non-null `last_fetch_age`), otherwise everything `null`/`[]` with `error` naming the failure |

`error` is always a single human-readable string combining every problem
found (`_summarize_error`), or `null` when clean -- never multiple
competing error representations.

### Hash generation

`compute_mission_hash(waypoints)` -- SHA256 (stdlib `hashlib`, no new
dependency) over a canonical JSON encoding of each waypoint's `sequence`,
`command`, `frame`, `latitude`, `longitude`, `altitude`, `param1`,
`param2`, `param3`, `param4`, waypoints sorted by `sequence` first
(order-of-collection-independent) and encoded with `sort_keys=True` and
fixed separators (key-order- and whitespace-independent). Two downloads of
the same mission always produce the same digest; changing any one of those
fields on any one waypoint changes it.

SHA256 over CRC32: this hash is meant for the operator to detect *any* real
mission difference for later comparison against its own copy. CRC32's much
higher collision rate makes a false "unchanged" reading plausible over the
number of comparisons a fleet accumulates over time -- not an acceptable
trade for a payload this size, where CRC32's speed advantage doesn't
matter.

**The hash is only computed when `mission_valid` is `true`** -- `null`
otherwise. A hash computed over a partial, duplicate-containing, or
otherwise inconsistent read would not represent the mission actually
stored on the Pixhawk, and could cause a false "the mission changed"
reading downstream; `null` communicates "no reliable hash available" rather
than a misleading value, the same "never fabricate" rule this whole feature
follows applied to the hash itself. `pixhawk_mission.py` never invents a
substitute hash either -- during a degraded live response it passes the
live (possibly `null`) hash through as-is, and only the cache-fallback path
(vehicle Flask API entirely unreachable) surfaces a previous, real hash
from the last confirmed-valid download.

Not yet implemented: comparison against the operator's own mission copy --
this endpoint only produces the hash, comparison logic lives on the
operator side (out of scope here, and not yet built there either as far as
this repo can tell).

### Ownership of mission state

**Scout (the vehicle/Pixhawk) remains the sole owner of the mission.**
This entire feature -- `mission_service.py`'s `download_pixhawk_mission()`
and `download_mission()`, and this file's `pixhawk_mission.py`/`mission.py`
-- is read-only by construction:

- The only MAVLink messages ever posted by either function are
  `MISSION_REQUEST_LIST` and `MISSION_REQUEST_INT` -- both *request*
  messages that ask the Pixhawk to report its own state. Neither function
  ever posts `MISSION_COUNT`, `MISSION_ITEM_INT`, `MISSION_CLEAR_ALL`, or
  `MISSION_ACK` -- the write-side messages exclusively used by the
  existing `/nav/upload_mission`/`/nav/clear_mission` routes in `app.py`,
  which this feature never calls into.
  (`test_mission_service.py`'s `test_never_posts_a_mission_write_message`
  asserts this directly, by inspecting every message type posted during a
  download.)
- `pixhawk_mission.py`/`mission.py` (Local Agent side) call exactly one
  outbound function each (`api_client.get_pixhawk_mission()` /
  `get_mission()`, both plain `requests.get`) -- there is no code path from
  either module into `command_executor.py` or any `/nav/*` write endpoint.
- **Upload is explicitly out of scope for this endpoint** -- no upload
  function exists in `mission_service.py`'s new code, and none is planned
  as part of this feature. A future mission-comparison feature (using
  `hash` against the operator's own copy) could motivate an upload path
  later, but that would be new, separately-reviewed work, not an extension
  made casually here.

### Verification

No live Pixhawk or SITL instance is available in this development
environment, so verification is via a mocked MAVLink mission
(`services/flask/test_mission_service.py`'s `FakeVehicle` -- models
mavlink2rest's actual cache-and-freshness-token behavior, not just a
canned response, so timing-dependent behavior like "don't trust a stale
cached reply" is actually exercised, not assumed) plus
`local_mission_agent/test_pixhawk_mission.py` for the caching/fallback
layer. Every scenario below is a real, currently-passing test, not a
manual claim:

| Scenario | Test |
|---|---|
| Empty mission (`count == 0`) | `TestPixhawkMissionZeroWaypoint.test_zero_waypoint_mission` |
| Normal mission, multi-waypoint | `TestPixhawkMissionNormal.test_normal_mission_matches_direct_hash_and_fields` |
| Stale/cached MAVLink response not mistaken for a fresh one | `TestStaleCache.test_preexisting_stale_cache_is_not_returned_as_the_answer` |
| Delayed responses (within timeout budget) | `TestDelayedResponses.test_succeeds_despite_delay_within_timeout_budget` |
| `MISSION_COUNT` timeout (never arrives) | `TestPixhawkMissionTimeout.test_count_never_arriving_is_partial_and_unvalidated` |
| Mid-mission item timeout (partial download) | `TestPixhawkMissionTimeout.test_item_timeout_yields_partial_true_and_invalid_hash_null` |
| mavlink2rest itself unreachable | `TestPixhawkMissionTimeout.test_mavlink2rest_unreachable` |
| Duplicate sequence numbers | `TestDuplicateSequenceDetection` (direct unit test of `_find_duplicate_sequences`, since the per-seq-request download design can't currently produce one end-to-end -- see that test's docstring) |
| Invalid coordinates (out-of-range latitude) | `TestPixhawkMissionInvalidCoordinates.test_out_of_range_latitude_flags_sequence_and_invalidates_hash` |
| Non-position command coordinates never flagged | `TestPixhawkMissionInvalidCoordinates.test_non_position_command_coordinates_are_never_checked` |
| Unsupported/undecodable mission item | `TestPixhawkMissionUnsupportedItem.test_undecodable_item_is_reported_not_fabricated` |
| Current waypoint (`current_seq`) reported directly, `null` when never broadcast | `TestPixhawkMissionNormal.test_current_seq_never_estimated_null_when_unavailable` |
| Multiple sequential downloads (hash stability + change detection) | `TestPixhawkMissionMultipleSequentialDownloads` |
| Hash determinism (same mission -> same hash, changed field -> different hash, order-independence) | `TestMissionHash` |
| Full response schema via the actual Flask route | `TestPixhawkMissionRoute.test_route_returns_full_schema` |
| Local Agent caching/fallback (valid pass-through, unreachable-with-no-cache, unreachable-with-cache, partial-not-cached, repeated polls) | `local_mission_agent/test_pixhawk_mission.py`, all classes |

Run everything above with:

```bash
python3 services/flask/test_mission_service.py
python3 services/local_mission_agent/test_pixhawk_mission.py
```

**Not verified**: behavior against a real Pixhawk/mavlink2rest/SITL stack
(none available here), and whether mavlink2rest's actual JSON shape for
`MISSION_ITEM_INT`/`MISSION_COUNT`/`MISSION_CURRENT` matches what this
module assumes (inferred from the existing, already-in-production
`/nav/upload_mission` code in `app.py`, which posts messages in the same
shape). Recommended before first live use: run
`curl http://127.0.0.1:8090/agent/pixhawk_mission` against a real Scout
with a known test mission uploaded via the existing `/nav/upload_mission`
flow, and confirm `count`/`waypoints` match what was uploaded and `hash`
stays stable across repeated calls with no mission change.

### How this fits the rest of the architecture

```
Operator --> Operator Backend --> Local Agent --> Existing Flask API --> mavlink2rest --> Pixhawk
```

This feature adds the Local Agent's first inbound HTTP listener -- everything
else in this module (status pushes, command polling) is Local-Agent-as-client.
The Operator Backend is expected to poll `GET /agent/diagnostics` for the
Vehicle Health page, trigger `POST /agent/system_check` on demand (e.g. a
"Run pre-deployment check" button), and poll `GET /agent/pixhawk_mission`
for the Pixhawk Mission card (already implemented as a proxy on the
Operator Backend side per that feature's own spec); this repo doesn't
contain the Operator Backend itself, so that wiring lives outside
`local_mission_agent/`.

Authority is unaffected by any of this: neither endpoint reads or writes
`control_authority`, so RC/operator control behaves exactly as it did before
this feature, in every authority state. See "Control authority" above.

## Configuring the operator endpoint(s)

`OPERATOR_URLS` (which operator station(s) to post status to) can be set
without editing `config.py`. Precedence, highest wins:

1. **`OPERATOR_URLS` environment variable**, comma-separated -- good for a
   one-off override or a systemd unit:

   ```bash
   OPERATOR_URLS=http://10.0.0.23:8210,http://10.0.0.24:8210 ./run_local_agent.sh
   ```

2. **`local_config.py`** -- gitignored, machine-specific, persists across
   runs without an env var. Copy the template once per machine:

   ```bash
   cp local_config.example.py local_config.py
   # edit local_config.py: set OPERATOR_URLS to this machine's operator station
   ```

   Desktop operator example (`local_config.py` on the Scout when the
   desktop is running the operator station):

   ```python
   OPERATOR_URLS = ["http://10.0.0.23:8210"]
   ```

   Laptop operator example (same file, different IP, no other changes needed):

   ```python
   OPERATOR_URLS = ["http://10.0.0.24:8210"]
   ```

3. **`DEFAULT_OPERATOR_URLS`** in `config.py` -- built-in fallback if
   neither of the above is set.

`send_to_operator` (`api_client.py`) always tries the last URL that worked
first, then falls through the rest of the list, and only raises once every
configured URL has failed for this send -- switching which machine is
"the" operator, or losing one of two, never requires a code change.

Run `./check_local_agent.sh` to see which source is currently active and
whether each configured URL is reachable right now.

## Practical comm-degradation test

Before physically breaking any link, run a read-only pre-test check:

```bash
python3 pretest_check.py
```

This prints, in one pass, exactly the evidence `decide()` itself would use
this iteration -- comm state, operator reachability, Pixhawk/heartbeat/
MAVLink message age, GPS fix, vehicle mode, arm state, battery availability
(raw value and whether it normalized to a usable percentage -- see "Battery
availability" above), mission loaded/count/current waypoint, control
authority, and the current decision + reason it would produce right now. It
makes no `/nav/*` call and changes nothing -- same read-only guarantee as
`GET /agent/diagnostics`.

**Checklist for physically running CONNECTED -> PARTITIONED -> DISCONNECTED
-> CONNECTED:**

1. **Before starting**: run `python3 pretest_check.py` (or
   `./check_local_agent.sh`). Confirm `Communication state: CONNECTED`,
   `Pixhawk connected: YES`, a real (non-`null`) GPS fix, and
   `Battery available: YES` with a sane percentage -- if battery reports
   unavailable, confirm the power module is actually connected before
   trusting any RTL-threshold behavior during the test.
2. Start the Local Agent: `./run_local_agent.sh` (foreground, so
   `[LOCAL AGENT] Comm state: ... -> ...` transitions print live) or as a
   background service per your normal deployment.
3. **CONNECTED**: confirm `payload.agent.current_policy.communication_policy
   == "FULL_REPORTING"`, `mission_policy` is `SUPERVISED_CONTINUATION` (or
   `OPERATOR_DIRECTED` if authority is `OPERATOR`), `autonomy_level ==
   "ASSISTED"`. Watch `GET http://127.0.0.1:8090/agent/diagnostics` or the
   operator's Agent page.
4. **Simulate PARTITIONED**: block the operator endpoint(s) only, while
   leaving the VPN/general internet path up (e.g. firewall the specific
   `OPERATOR_URLS` port, or stop the operator backend process, without
   pulling the VPN tunnel). Confirm within one `PARTITIONED_INTERVAL`
   (`config.py`, default 10s):
   - `payload.transitions` gains a `"communication"` entry
     `CONNECTED -> PARTITIONED` with a concrete reason (`transition_reasons.
     comm_transition_reason`), not a generic message.
   - `current_policy.communication_policy ==
     "REDUCED_REPORTING_LOCAL_AUTONOMY"`, `mission_policy ==
     "AUTONOMOUS_CONTINUATION_REDUCED_REPORTING"`, `autonomy_level ==
     "AUTONOMOUS"`.
   - `current_decision`/`decision_reason` still reflect real vehicle
     evidence (mission phase/battery/GPS/link), not a placeholder -- they
     are not directly driven by `comm_state` except through
     `mavlink_connected`/`heartbeat_age_s`, which are unaffected by an
     operator-only outage.
   - No duplicate `"decision"` entries appear in `payload.agent.decision_timeline`
     if `current_decision` itself hasn't changed (see "Do not log duplicate
     entries" below) -- only the `"communication"` transition should be new.
5. **Simulate DISCONNECTED**: additionally drop the VPN/general internet
   path (or just the VPN, per `communication.vpn_ok()`). Confirm within one
   `DISCONNECTED_INTERVAL` (default 5s):
   - `payload.transitions` gains `PARTITIONED -> DISCONNECTED`.
   - `current_policy.mission_policy == "AUTONOMOUS_CONTINUATION_BUFFERED"`.
   - Status messages are being appended to `agent_buffer.jsonl` instead of
     sent (`[LOCAL AGENT] Could not send, buffering (buffer now N total)`
     in the process log); confirm `N` is growing but stays bounded by
     `config.MAX_BUFFERED_MESSAGES` if the outage runs long.
   - `GET /agent/diagnostics` (still reachable locally on
     `LOCAL_AGENT_HTTP_PORT`, since that server doesn't depend on operator
     reachability) shows `communication.status == "FAIL"`.
6. **Restore CONNECTED**: re-enable both paths. Confirm within one
   `CONNECTED_INTERVAL` (1s):
   - `payload.transitions` gains `DISCONNECTED -> CONNECTED` with reason
     "Operator endpoint reachable again after being fully unreachable."
   - The process log shows `[LOCAL AGENT] Backlog flush: {'sent': N,
     'remaining': 0}` -- confirm `remaining` reaches `0` (a fresh live
     status is always sent *before* the flush completes, per
     `pending_flush` in `local_agent.py`, so the operator's current-state
     view is never stale).
   - `current_policy` returns to `FULL_REPORTING`/`SUPERVISED_CONTINUATION`/`ASSISTED`.
7. **After the run**: `curl -s http://127.0.0.1:8090/agent/decision_timeline`
   and `payload.transitions` should show exactly one `"communication"`
   transition per real edge above (4 total: CONNECTED->PARTITIONED->
   DISCONNECTED->CONNECTED), not one per poll interval -- confirms the
   dedup behavior in "Do not log duplicate entries every polling cycle"
   held during the test.

**Known limitation for today's test**: `communication.vpn_ok()` shells out
to `sudo -n wg show` and cannot currently distinguish "no handshake" from
"the command itself failed" (e.g. passwordless sudo not configured on this
Pi) -- see `communication.py`'s docstring and `diagnostics.py`'s
`_diag_network`. If step 5 doesn't reach `DISCONNECTED` as expected, check
the Local Agent's own stdout for a one-time
`[COMM] 'sudo -n wg show' failed ...` print before assuming the network
path itself is the problem.

**Note**: the checklist above predates `risk_model.py` / `decision_policy.py`
/ `experiment_recorder.py` and only covers `decision_engine.py`'s older
`current_policy` layer (`communication_policy`/`mission_policy`/
`autonomy_level`, still accurate and unchanged). See "E3: communication-
degradation experiment" below for the equivalent trial through the current
continuous-risk / advisory-recommendation / action-request / recorder
pipeline.

## E3: communication-degradation experiment (Operator link loss)

Demonstrates that the Scout keeps reasoning and acting locally as the
Operator link degrades and is lost -- through the **same** evidence ->
risk -> advice -> action -> FSM pipeline E2 (energy) uses, never a second
comm-state system, never a direct "DISCONNECTED means RTL" shortcut.

### Where communication evidence comes from (as of this task)

`communication.CommunicationMonitor.poll()` calls `resolve_comm_state()`
once per main-loop iteration:

1. If an experiment injection with a non-`None` `communication_state` is
   active (`experiment_injection.active()`), that value is used verbatim
   and tagged `source="SIMULATED"` -- real polling is skipped entirely.
2. Otherwise the REAL evidence chain runs and is tagged `source="REAL"`:
   `communication.get_comm_state()` --
   `operator_ok()` (`GET {OPERATOR_URLS}/agent/status`, `OPERATOR_CONNECT_TIMEOUT`=2s
   per URL) -> **CONNECTED** if any URL answers < 500;
   else `internet_ok()` (`ping -c1 -W1 8.8.8.8`) fails -> **DISCONNECTED**;
   else `vpn_ok()` (`sudo -n wg show`, "latest handshake" present) true ->
   **PARTITIONED**; else **DISCONNECTED**.

`comm_monitor.source` (REAL/SIMULATED) is carried alongside `comm_state`
into the status payload (`payload.communication.source`), the transition log
(`payload.transitions[].source` on a `"communication"` entry), and every
recorder decision/telemetry record (`communication_source`) -- a synthetic
trial can never be mistaken for a real link event in the evidence.

### Governing risk / advice / action (risk_model.py / decision_policy.py, unchanged by this task)

| `comm_state` | component score (`risk_config.py`) | severity floor | `risk_model._recommendation` | `decision_policy` action |
|---|---|---|---|---|
| `CONNECTED` | 0.0 | none | `CONTINUE` (if nothing else elevates risk) | `NONE` |
| `PARTITIONED` | `communication_partitioned_score` = 0.50 | **>= ELEVATED** (`REASON_COMMUNICATION_PARTITIONED`) | `CONTINUE_WITH_CAUTION` | `NONE` |
| `DISCONNECTED`, autonomy-capable (`control_authority=="LOCAL_AGENT"` and mission-execution state in `RUNNING/PAUSED/RETURNING_HOME/HOME_ARRIVAL_PENDING/COMPLETED_HOLD`) | `communication_disconnected_authority_healthy_score` = 0.70 | **>= HIGH** (`REASON_COMMUNICATION_DISCONNECTED_AUTONOMOUS_CONTINUATION`) | `HOLD` | `REQUEST_HOLD` |
| `DISCONNECTED`, otherwise | `communication_disconnected_score` = 0.95 | **>= HIGH** (`REASON_COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION`) | `HOLD` | `REQUEST_HOLD` |

Both DISCONNECTED floors land on **HIGH**, not CRITICAL -- `_recommendation()`
only escalates to `RECOMMEND_RETURN` when the *dominant* component is
`energy` **and** `rtl_return_feasible` is `True`; a comm-only degradation's
dominant component is `communication`, so current policy's answer for a
comm-only DISCONNECTED is **HOLD** (station-keep), not RETURN_HOME. This
matches the task's acceptable-outcome note ("DISCONNECTED -> local
autonomous safe action according to existing policy") without forcing a new
rule. `ACTION_REQUEST_HOLD` only becomes a real vehicle LOITER write if
`replan_config.autonomous_execution_enabled=True`, `dry_run=False`, and
`control_authority=="LOCAL_AGENT"` at write time (see "Enabling autonomous
execution for a live trial" above) -- with those OFF (the default), the
HOLD recommendation/action is still fully computed and recorded, just never
turned into a vehicle command, exactly like every other advisory-only run.

### Real impairment (Stage B: PARTITIONED) vs. synthetic override (Stage C: DISCONNECTED)

The vehicle Flask service already implements a real, tested tc/netem
impairment scoped to the Scout<->Operator WireGuard interface only
(`services/flask/services/network_impairment/`, `GET/POST/DELETE
/agent/experiment/network`, allowlisted to `wg0`, handle-scoped
teardown, a local expiry timer armed *before* rules are applied so
impairment can never outlive its window even if the Operator disappears).
Stage 1 supports `latency_ms` / `jitter_ms` / `packet_loss_pct`
(egress, `direction: "scout_to_operator"` only) -- `full_disconnect` /
`bandwidth_kbit_s` / ingress / `both` are accepted by the schema but
rejected as not-yet-implemented.

Because `tc netem` on `wg0` only shapes traffic tunnelled *through* it
(the Operator HTTP calls), not WireGuard's own handshake/keepalive
(generated by the kernel module directly on the underlying physical
interface) or `internet_ok()`'s ping (a different interface entirely),
100% loss on `wg0` reliably reaches **PARTITIONED** (`operator_ok()` fails,
`internet_ok()`/`vpn_ok()` stay true) but cannot deterministically reach
**DISCONNECTED** -- that would need the un-implemented `full_disconnect`,
or waiting out `WG_RECENT_HANDSHAKE_S` (180s) for the handshake itself to
go stale, neither of which is a safe, fast, repeatable trial. Per this
task's own fallback rule, Stage C therefore uses the new
`communication_state` synthetic override (`experiment_injection.py`,
same `PUT/GET/DELETE /agent/replan/experiment` endpoint E2's
`force_safe_return`/`energy_margin_percent`/`battery_percent` already use --
no new endpoint) instead of inventing an unsafe real full-outage path.

Neither path ever touches Pixhawk USB MAVLink: the tc rule is allowlisted to
`wg0` only (`network_impairment/config.py: DEFAULT_ALLOWED_INTERFACES =
("wg0",)`, `_guard_unmanaged_root` refuses to shape anything else), and the
synthetic override only changes which string `comm_state` resolves to in
Python -- it never touches `vehicle_state`/MAVLink evidence at all.

**Update (final field E3 trial):** a safe, real, repeatable true-DISCONNECTED
method was later developed and physically used for the final field
validation -- host-side `iptables` rules dropping only the one known
WireGuard peer's UDP 5-tuple (both directions), armed with a `systemd-run`
transient unit that self-restores the rule after a computed duration
independent of the SSH/wg0 session's own survival, letting the already
in-flight handshake genuinely cross `WG_RECENT_HANDSHAKE_S` (180s) and
`get_comm_state()` reach DISCONNECTED with `source=REAL`, not `SIMULATED`.
This does not replace the synthetic override above (still the right tool for
a quick bench/regression check) but is the mechanism actually used to
physically demonstrate CONNECTED -> PARTITIONED -> CONNECTED -> DISCONNECTED
-> HOLD -> LOITER -> SAFE_HOLD -> PAUSED -> (comm recovery, no auto-resume)
for the thesis. See [E3_final_procedure.md](../../../E3_final_procedure.md)
§4-§6 for the exact commands and safety/out-of-band-access checklist, and
[final_thesis_experiment_commands.txt](../../../final_thesis_experiment_commands.txt)
for the consolidated final field-experiment command set.

### Staged experiment

```bash
# 0) Baseline: recorder run starts automatically on a genuine Start (see
#    experiment_recorder.py) -- optionally tag it first:
curl -s -X PATCH http://127.0.0.1:8090/agent/experiment_recording/config \
  -H 'Content-Type: application/json' \
  -d '{"experiment_id": "E3-comm-degradation", "experiment_type": "E3",
       "scenario": "CONNECTED_PARTITIONED_DISCONNECTED_CONNECTED"}'
# ... issue the normal operator Start Mission command (POST
# /agent/mission_execution/start via the operator) so the run is RUNNING.

# Stage A -- CONNECTED (baseline). Confirm via GET /agent/diagnostics or
# python3 pretest_check.py: comm_state CONNECTED, risk level LOW/whatever the
# rest of the vehicle evidence supports, recommendation CONTINUE, action NONE.

# Stage B -- PARTITIONED (REAL impairment, on the vehicle Flask service,
# :8080). 100% loss is the simplest deterministic choice (any packet_loss_pct
# > 0 with the required latency_ms/jitter_ms would also work; 100% removes
# any chance of a slow-but-successful GET muddying the trial):
curl -s -X POST http://127.0.0.1:8080/agent/experiment/network \
  -H 'Content-Type: application/json' \
  -d '{"experiment_id": "e3-stage-b", "direction": "scout_to_operator",
       "latency_ms": 0, "packet_loss_pct": 100, "duration_s": 60}'
# Within one PARTITIONED_INTERVAL (10s, config.py) confirm comm_state ->
# PARTITIONED, source REAL, risk component_floor_level >= ELEVATED
# (component_floor_reason COMMUNICATION_PARTITIONED), recommendation
# CONTINUE_WITH_CAUTION, action NONE. Pixhawk/local telemetry (mavlink_
# connected, heartbeat_age_s) stays healthy throughout -- it never uses wg0.

# (impairment self-clears after duration_s; DELETE also works immediately:)
curl -s -X DELETE http://127.0.0.1:8080/agent/experiment/network

# Stage C -- DISCONNECTED (SYNTHETIC override, on the Local Agent, :8090):
curl -s -X PUT http://127.0.0.1:8090/agent/replan/experiment \
  -H 'Content-Type: application/json' \
  -d '{"communication_state": "DISCONNECTED", "duration_s": 60, "target_vehicle": "usv-2"}'
# Within one Local Agent loop iteration confirm comm_state -> DISCONNECTED,
# source SIMULATED (payload.communication.source, transitions[].source,
# recorder communication_source), risk component_floor_level >= HIGH,
# recommendation HOLD, action REQUEST_HOLD. Local Mission Agent stays
# alive/responsive (GET /agent/diagnostics keeps answering -- it never
# depends on Operator reachability); Pixhawk telemetry stays healthy.

# Stage D -- reconnection. Let the Stage C override expire (or DELETE it
# early) and, separately, restore the real link (Stage B impairment, if
# still applied, also expires/```DELETE```s on its own):
curl -s -X DELETE http://127.0.0.1:8090/agent/replan/experiment
# Confirm within one CONNECTED_INTERVAL (1s): comm_state -> CONNECTED,
# source REAL (real evidence resumes automatically -- resolve_comm_state()
# just stops seeing an active injection, no extra step), backlog flush
# drains (payload.communication_state transition + buffer behaviour exactly
# as the older DISCONNECTED -> CONNECTED checklist above describes),
# control_authority UNCHANGED (still whatever it was before the outage --
# reconnection never auto-transfers OPERATOR <-> LOCAL_AGENT, see "Authority
# model" above), and no mission restart / duplicate command occurs.
```

### Pass criteria

- Mission was RUNNING before Stage B (a genuine Start recorded a run).
- Stage B: real tc/netem impairment applied to `wg0` only; `comm_state`
  CONNECTED -> PARTITIONED within `PARTITIONED_INTERVAL`; `source` REAL;
  risk `component_floor_level` >= ELEVATED; `recommendation`
  CONTINUE_WITH_CAUTION; `action` NONE.
- Stage C: synthetic `communication_state=DISCONNECTED` override applied via
  the existing `/agent/replan/experiment` endpoint, tagged `source`
  SIMULATED throughout (status/transitions/recorder); risk
  `component_floor_level` >= HIGH; `recommendation` HOLD; `action`
  REQUEST_HOLD (and, if `autonomous_execution_enabled`/`!dry_run`/
  `LOCAL_AGENT` authority were configured for the trial, a real LOITER hold
  executes and is recorded).
- Throughout B and C: the Local Agent's own inbound HTTP surface (`GET
  /agent/diagnostics`, `LOCAL_AGENT_HTTP_PORT`) keeps answering, and
  Pixhawk/local MAVLink evidence (`mavlink_connected`, `heartbeat_age_s`,
  GPS fix) stays healthy and fresh -- neither impairment path touches that
  link.
- Stage D: `comm_state` returns to CONNECTED with `source` REAL once the
  injection expires/is cleared (and any real impairment is also gone);
  `control_authority` is unchanged from its pre-outage value; no mission
  restart or duplicate command occurs; the buffered backlog (if any)
  drains.
- `payload.transitions` shows exactly one `"communication"` entry per real
  edge (CONNECTED->PARTITIONED->DISCONNECTED->CONNECTED), each carrying the
  correct `source`.
- The recorder's `decision_snapshots.jsonl`/`telemetry.csv` for the run
  reconstruct the whole timeline (`communication_state`,
  `communication_source`, `risk.level`, `risk.component_floor_level`,
  `risk.recommendation`, `action_request.action`, `mission_execution_state`,
  `authority`, `mode`/`current_waypoint` are all already-recorded fields --
  see experiment_recorder.py's `TELEMETRY_COLUMNS` / `record_decision`).

