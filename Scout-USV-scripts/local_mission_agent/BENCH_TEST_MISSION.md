# Prop-safe bench test — mission upload / clear

**Read-only. Nothing in this document commands the vehicle.**

There is no `upload`, `clear`, `ARM`, `AUTO`, `Set Home`, or mode change here,
by design. Every command below is a `GET`, a log tail, or a service
start/stop. If a step seems to call for a write, it is not in this checklist —
stop and get a second person before improvising one.

**Before starting:** props off or vehicle out of the water, and confirm
disarmed via §2 before anything else.

---

## Environment (this bench)

| Thing | Where |
|---|---|
| Vehicle Flask | Docker container `flask-app`, host port `8080` |
| Local Agent | plain host process `local_agent.py`, HTTP on `8090` |
| mavlink2rest | `http://127.0.0.1:6040` (`host.docker.internal:6040` inside the container) |
| Vehicle | ArduRover 4.5.0, `MAV_TYPE_SURFACE_BOAT` |

`flask-app` **volume-mounts** `./services/flask`, so code changes are live —
a restart picks them up and **no rebuild is needed**.

---

## 1. Start the two services without rebuilding anything else

Flask — restart just this container, leaving `sensor-service`, `influxdb`,
`blueos-*` and the rest untouched:

```bash
cd /home/motherpi/AqualityONE/motherpi
docker compose restart flask-app
docker compose ps flask-app
```

> Use `restart`. Do **not** use `docker compose up --build`, which would
> rebuild unrelated services.

**The restart is not optional.** A container started before the
mission-contract-v1 work is still serving the old code, and it fails in a way
that looks like a working vehicle rather than an error: `/agent/pixhawk_mission`
returns `200` with the legacy `count`/`hash` fields but **`null` for every
contract field**. Confirm the restart took before trusting anything below:

```bash
curl -s http://127.0.0.1:8080/agent/pixhawk_mission \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
ok = d.get("pixhawk_item_count") is not None and d.get("contract_version")=="mission-contract-v1"; \
print("contract fields present:", ok, "|", {k:d.get(k) for k in ("contract_version","pixhawk_item_count","route_waypoint_count")})'
```

If that prints `False`, the container is running stale code — restart it again
and re-check before continuing.

Local Agent — foreground, so you can watch it and stop it with Ctrl-C:

```bash
cd /home/motherpi/AqualityONE/motherpi/services/local_mission_agent
./run_local_agent.sh
```

In a second terminal, confirm both are up:

```bash
./check_local_agent.sh
curl -s http://127.0.0.1:8080/agent/state >/dev/null && echo "flask OK"
```

---

## 2. Check control authority (and that it is safe to proceed)

```bash
curl -s http://127.0.0.1:8080/agent/control_authority
```

Expect `{"authority":"OPERATOR"}`.

Confirm the vehicle is **disarmed** before anything else — bit `128`
(`MAV_MODE_FLAG_SAFETY_ARMED`) must be clear:

```bash
curl -s http://127.0.0.1:6040/mavlink/vehicles/1/components/1/messages/HEARTBEAT \
  | python3 -c 'import json,sys; m=json.load(sys.stdin)["message"]; b=m["base_mode"]["bits"]; \
print("base_mode:",b,"ARMED" if b&128 else "disarmed","| custom_mode:",m["custom_mode"], \
"(10=AUTO)" if m["custom_mode"]==10 else "")'
```

**If it prints `ARMED`, or `custom_mode: 10`, stop.** Upload and clear are
refused in both states by design, and the bench test has nothing to observe.

---

## 3. Fetch the current mission

Vehicle Flask (authoritative, does the real MAVLink readback):

```bash
curl -s http://127.0.0.1:8080/agent/pixhawk_mission \
  | python3 -m json.tool | head -40
```

Compact form — the contract fields that matter:

```bash
curl -s http://127.0.0.1:8080/agent/pixhawk_mission \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
print({k:d.get(k) for k in ("mission_valid","pixhawk_item_count","route_waypoint_count", \
"route_content_hash","full_mission_hash","current_seq","generation")})'
```

Local Agent's resilience-wrapped view (adds `last_fetch_age`):

```bash
curl -s http://127.0.0.1:8090/agent/pixhawk_mission | python3 -m json.tool | head -30
```

---

## 4. Monitor mission-operation state

The authoritative persistent record — survives the operation finishing **and**
an agent restart:

```bash
curl -s http://127.0.0.1:8090/agent/mission_operation | python3 -m json.tool
```

Watch it live (2 s cadence):

```bash
watch -n2 'curl -s http://127.0.0.1:8090/agent/mission_operation \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
print(d[\"state\"], d[\"command_type\"], d[\"command_id\"], \"elapsed\", d[\"elapsed_s\"]); \
print(\" expected\", d[\"expected_route_waypoint_count\"], d[\"expected_route_content_hash\"]); \
print(\" observed\", d[\"observed_route_waypoint_count\"], d[\"observed_route_content_hash\"]); \
print(\" ack\", d[\"acknowledgement\"], \"| error\", d[\"error\"])"'
```

Expected progression: `IDLE → ACCEPTED → EXECUTING → DELIVERING_RESULT →
COMPLETED` (or `FAILED`).

The raw state file, if the agent is stopped:

```bash
python3 -c 'import json,config; print(json.dumps(json.load(open(config.MISSION_OPERATION_STATE_FILE)),indent=2))'
```

The lightweight live worker block (`agent.mission_upload`) is separate and goes
idle the moment an upload ends — that is why the record above exists.

---

## 5. Inspect command results

Recent command lifecycles:

```bash
curl -s http://127.0.0.1:8090/agent/command_history | python3 -m json.tool | head -50
```

Persisted terminal results still awaiting operator acknowledgement — non-empty
here means at-least-once redelivery still has something to resend:

```bash
python3 -c 'import json,config; d=json.load(open(config.COMMAND_RESULTS_FILE)); \
print(len(d),"retained:",list(d)); print(json.dumps(d,indent=2)[:1500])'
```

Transaction diagnostics from the last operation:

```bash
curl -s http://127.0.0.1:8090/agent/mission_operation \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("diagnostics"),indent=2))'
```

---

## 6. Tail only mission-related logs

Flask side — just the two write-side services:

```bash
docker compose logs -f --tail=50 flask-app | grep --line-buffered -E "MISSION_UPLOAD|MISSION_CLEAR"
```

Local Agent side (it runs in the foreground, so tee it when you start it):

```bash
./run_local_agent.sh 2>&1 | tee /tmp/local_agent_bench.log
# then, in another terminal:
grep --line-buffered -E "MISSION_UPLOAD|MISSION_CLEAR|MISSION_OP" /tmp/local_agent_bench.log
```

Everything in one stream:

```bash
docker compose logs -f --tail=20 flask-app 2>&1 \
  | grep --line-buffered -E "MISSION_UPLOAD|MISSION_CLEAR|MISSION_OP"
```

---

## 7. Restore services after the test

```bash
# Stop the Local Agent: Ctrl-C in its terminal, or
pkill -f "[l]ocal_agent.py"

# Return Flask to its normal state (no rebuild)
cd /home/motherpi/AqualityONE/motherpi
docker compose restart flask-app

# Confirm
./services/local_mission_agent/check_local_agent.sh
curl -s http://127.0.0.1:8080/agent/control_authority
```

Clear the bench's mission-operation record so the next run starts from `IDLE`
(runtime state only — it is gitignored, and it is **not** the authoritative
per-command result, which lives in `command_results.json`):

```bash
rm -f /home/motherpi/AqualityONE/motherpi/services/local_mission_agent/mission_operation_state.json
```

Leave `command_log.jsonl` and `command_results.json` alone unless you
deliberately want to forget which command IDs were processed — deleting them
re-arms every already-judged command for re-execution on its next redelivery.

---

## What this checklist cannot prove

Everything above observes. The following need an actual mission write and are
deliberately **not** here — see `MISSION_CONTRACT_v1.md` §10:

- which empty representation this airframe produces (`NO_ITEMS` / `HOME_ONLY`)
- whether this build emits a `MISSION_ACK` for `MISSION_CLEAR_ALL`
- `route_content_hash` agreement against a real vehicle readback
- mission-start rollback on real hardware
- redelivery timing under a genuinely slow upload
- real mission storage capacity and per-item handshake rate (§4a)
- `UNKNOWN_AFTER_RESTART` against a real interrupted transfer
