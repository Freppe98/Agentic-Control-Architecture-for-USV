from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import asyncio
import json
import time


@asynccontextmanager
async def lifespan(app):
    # Background monitor: log comms-state transitions once per second.
    task = asyncio.create_task(_comms_monitor_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
BASE_DIR = Path(__file__).resolve().parent

STALE_AFTER_SECONDS = 8
PARTITIONED_AFTER_SECONDS = 15
DISCONNECTED_AFTER_SECONDS = 30

latest_agent_status = {}
latest_agent_received_at = None

# Newest accepted message send-time per vehicle (epoch seconds). Enforces monotonic
# current-state updates: a replayed/buffered packet whose own timestamp is older than
# what we've already accepted must not overwrite the current fleet snapshot. The
# operator backend owns current state; store-and-forward replay is history, not "now".
latest_msg_ts_by_id = {}   # {vehicle_id: float epoch seconds}

# --- Operator-side comms-state transition log (see SYSTEM_INFORMATION_MODEL.md) ---
# The operator backend owns the *arrival-age* view of reachability. A 1s monitor
# records every per-vehicle transition so the UI can draw a comms timeline and the
# thesis can measure total disconnected time — independent of frontend polling.
last_seen_by_id = {}       # {vehicle_id: datetime}
comms_state_by_id = {}     # {vehicle_id: last-logged state}
comms_history_by_id = {}   # {vehicle_id: [ {state, from, ts, since_last_seen_s} ]}

# Last-known telemetry per vehicle (store-and-forward consequence). When the Local
# Agent degrades and drops the telemetry group, the operator must show the LAST KNOWN
# position/heading/etc — NOT invent a home/zero position. The operator owns "current
# state" = last-known composite; absent != reset. Only ever updated from real values.
last_known_telemetry = {}  # {vehicle_id: {lat, lng, heading, groundspeed, battery, ...}}

# --- Persistent event log (BACKEND_ROADMAP #2; Operator-backend-owned) ---
# One server-side, append-only record that replaces the frontend's flatten-from-
# payload feed. Two sources feed it: (1) operator-side comms-state transitions from
# the monitor above (first-class, deterministic), and (2) vehicle-reported events
# forwarded in POST /agent/status.payload.events (deduped so re-sent packets don't
# spam the log). Exposed at GET /api/events. Stable ids + an `acknowledged` field
# model future acknowledgement without inventing the action yet. In-memory (resets
# on restart), like the comms log; durable storage is out of scope here.
MAX_EVENTS = 5000
event_log = []             # [ {id, ts, severity, type, source, vehicle_id, vehicle, message, acknowledged} ]
_event_seq = 0             # monotonic event id (supports later replay / since-id)
_ingested_event_keys = set()  # fingerprints of forwarded vehicle events already stored
# vehicle_names {vehicle_id: display name} is seeded from FLEET_TEMPLATE (defined below)


FLEET_TEMPLATE = [
    {
        "id": 1,
        "name": "USV-1",
        "online": False,
        "status": "UNKNOWN",
        "battery": None,
        "comms": "No data",
        "comm_state": "UNKNOWN",
        "heading": None,
        "speed": None,
        "mission": "Unknown",
        "coverage": None,
        # No position until the vehicle actually reports one — never fabricate a marker.
        "lat": None,
        "lng": None,
        "agent": {},
        "telemetry": {},
    },
    {
        "id": 2,
        "name": "Scout",
        "online": False,
        "status": "UNKNOWN",
        "battery": None,
        "comms": "No data",
        "comm_state": "UNKNOWN",
        "heading": None,
        "speed": None,
        "mission": "Unknown",
        "coverage": None,
        "lat": None,
        "lng": None,
        "agent": {},
        "telemetry": {},
    },
    {
        "id": 3,
        "name": "USV-3",
        "online": False,
        "status": "UNKNOWN",
        "battery": None,
        "comms": "No data",
        "comm_state": "UNKNOWN",
        "heading": None,
        "speed": None,
        "mission": "Unknown",
        "coverage": None,
        "lat": None,
        "lng": None,
        "agent": {},
        "telemetry": {},
    },
]

vehicle_names = {usv["id"]: usv["name"] for usv in FLEET_TEMPLATE}


def normalize_agent_message(message: dict) -> dict:
    """
    Accepts both:
    1. Envelope format:
       {"message_type": "...", "source": "...", "payload": {...}}

    2. Direct payload format:
       {"usv_id": ..., "comm_state": ..., "telemetry": {...}}
    """
    if "payload" in message and isinstance(message["payload"], dict):
        payload = message["payload"]
        envelope = message
    else:
        payload = message
        envelope = {}

    telemetry = payload.get("telemetry", {}) or {}
    mission = payload.get("mission", {}) or {}
    communication = payload.get("communication", {}) or {}
    health = payload.get("health", {}) or {}
    measurements = payload.get("measurements", {}) or {}
    fleet_info = payload.get("fleet", {}) or {}
    events = payload.get("events", []) or []

    usv_id_raw = payload.get("usv_id", payload.get("id", 2))
    try:
        usv_id = int(str(usv_id_raw).replace("usv-", ""))
    except Exception:
        usv_id = 2

    comm_state = payload.get("comm_state", "UNKNOWN")

    # Last-known fallback (store-and-forward): when this packet omits/zeroes a field,
    # use the last real value the vehicle reported rather than inventing one. Never
    # substitute a home/zero position — an absent position stays None so the UI shows
    # "no fix"/last-known and the map does not plot a fabricated marker.
    lk = last_known_telemetry.get(usv_id, {})
    battery = telemetry.get("battery")
    if battery is None:
        battery = lk.get("battery", payload.get("battery"))

    lat = telemetry.get("lat") or payload.get("lat") or lk.get("lat")
    lng = telemetry.get("lng") or payload.get("lng") or lk.get("lng")

    def age_seconds(iso_time):
        if not iso_time:
            return None
        t = datetime.fromisoformat(iso_time)
        return (datetime.now(timezone.utc) - t).total_seconds()

    age = age_seconds(latest_agent_received_at)
    if age is None:
        online = False
        comm_state = "UNKNOWN"
    elif age > DISCONNECTED_AFTER_SECONDS:
        online = False
        comm_state = "DISCONNECTED"
    elif age > PARTITIONED_AFTER_SECONDS:
        online = True
        comm_state = "PARTITIONED"
    else:
        # Operator-side comm-state is arrival-age-derived, NOT the vehicle's
        # self-assessment: a fresh arrival means the operator link is up now, even
        # if a replayed/buffered payload self-reports DISCONNECTED. (The vehicle's
        # own link view stays available under `communication`/`raw`.)
        online = True
        comm_state = "CONNECTED"

    heading = telemetry.get("heading")
    if heading is None:
        heading = lk.get("heading")
    speed = telemetry.get("groundspeed", telemetry.get("speed"))
    if speed is None:
        speed = lk.get("groundspeed", lk.get("speed"))

    return {
        "id": usv_id,
        "name": payload.get("name") or name_of(usv_id),
        "online": online,
        "status": mission.get("mission_state", payload.get("mission_state", "UNKNOWN")),
        "battery": battery if battery != -1 else None,
        "comms": comm_state,
        "comm_state": comm_state,
        "last_seen_age_s": round(age, 1) if age is not None else None,
        "heading": heading,
        "speed": speed,
        "mission": mission.get("mission_state", payload.get("mission", "Unknown")),
        "coverage": payload.get("coverage"),
        "lat": lat,
        "lng": lng,
        "mission_data": payload.get("mission", {}) or {},
        "communication": payload.get("communication", {}) or {},
        "health": payload.get("health", {}) or {},
        "measurements": payload.get("measurements", {}) or {},
        "events": payload.get("events", []) or [],
        "fleet_info": fleet_info,
        "agent": {
            "groups": payload.get("groups", []),
            "source": envelope.get("source", payload.get("source")),
            "target": envelope.get("target", payload.get("target")),
            "message_type": envelope.get("message_type", "status"),
            "schema_version": envelope.get("schema_version", "unknown"),
            "timestamp": envelope.get("timestamp", time.time()),
        },
        "telemetry": telemetry,
        "raw": message,
        "last_seen": latest_agent_received_at,
    }


def extract_usv_id(message: dict) -> int:
    """Vehicle id from either envelope or direct-payload form (mirrors normalize)."""
    payload = message.get("payload", message) if isinstance(message, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    raw = payload.get("usv_id", payload.get("id", 2))
    try:
        return int(str(raw).replace("usv-", ""))
    except Exception:
        return 2


def extract_name(message: dict):
    """Display name from either envelope or direct-payload form (or None)."""
    payload = message.get("payload", message) if isinstance(message, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    return payload.get("name")


def parse_vehicle_id(raw) -> int:
    """Vehicle id from a path/query value in either '2' or 'usv-2' form (or -1)."""
    try:
        return int(str(raw).lower().replace("usv-", ""))
    except (TypeError, ValueError):
        return -1


def extract_message_ts(message: dict):
    """Best-effort send-time (epoch seconds) of a status message, for the monotonic
    current-state guard. Reads the envelope `timestamp` (local_agent sends
    `time.time()`), falling back to a payload timestamp. Accepts epoch numbers,
    numeric strings, or ISO-8601. Returns None if the message carries no time."""
    if not isinstance(message, dict):
        return None
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    raw = (message.get("timestamp") or message.get("ts")
           or payload.get("timestamp") or payload.get("timestamp_s") or payload.get("ts"))
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(raw)  # numeric string
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def name_of(vid: int) -> str:
    return vehicle_names.get(vid, f"USV-{vid}")


def derive_comm_state(age_s):
    """Operator-side comm-state from arrival age (same thresholds as normalize)."""
    if age_s is None:
        return "UNKNOWN"
    if age_s > DISCONNECTED_AFTER_SECONDS:
        return "DISCONNECTED"
    if age_s > PARTITIONED_AFTER_SECONDS:
        return "PARTITIONED"
    return "CONNECTED"


def record_comms_state(vid: int, state: str, ts: datetime, age_s):
    """Append a transition only when the state actually changes."""
    prev = comms_state_by_id.get(vid)
    if state == prev:
        return None
    comms_state_by_id[vid] = state
    entry = {
        "state": state,
        "from": prev,
        "ts": ts.isoformat(),
        "since_last_seen_s": round(age_s, 1) if age_s is not None else None,
    }
    comms_history_by_id.setdefault(vid, []).append(entry)
    print(f"[COMMS] USV-{vid}: {prev} -> {state}")
    _emit_comms_event(vid, prev, state, ts)
    return entry


def evaluate_comms_transitions():
    """Re-derive each tracked vehicle's comm-state and log any change."""
    now = datetime.now(timezone.utc)
    for vid, seen in list(last_seen_by_id.items()):
        age = (now - seen).total_seconds()
        record_comms_state(vid, derive_comm_state(age), now, age)


async def _comms_monitor_loop():
    while True:
        try:
            evaluate_comms_transitions()
        except Exception as exc:  # keep the loop alive
            print("[COMMS-MONITOR] error:", exc)
        await asyncio.sleep(1)


# --- Event store (see the "Persistent event log" block above) ---

def _append_event(*, severity, message, etype, source, vehicle_id=None,
                  vehicle=None, ts=None):
    """Append one event to the server-side log and return it."""
    global _event_seq
    _event_seq += 1
    entry = {
        "id": _event_seq,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "severity": severity,          # info|caution|warning|emergency or None (UNSPEC)
        "type": etype,                 # e.g. "comms", "vehicle"
        "source": source,              # "operator-backend" or a vehicle/agent id
        "vehicle_id": vehicle_id,
        "vehicle": vehicle,
        "message": message,
        "acknowledged": False,         # modelled now; POST ack endpoint is a later item
    }
    event_log.append(entry)
    if len(event_log) > MAX_EVENTS:
        del event_log[0:len(event_log) - MAX_EVENTS]
    print(f"[EVENT] #{entry['id']} {severity or 'unspec'} {source}: {message}")
    return entry


def _comms_event_for(prev, state):
    """Deterministic (severity, message) for an operator-side comms transition."""
    if state == "DISCONNECTED":
        return ("warning", "Communication lost")
    if state == "PARTITIONED":
        return ("caution", "Communication partitioned")
    if state == "CONNECTED":
        if prev is None:
            return ("info", "First contact established")
        return ("info", "Communication restored")
    return None  # UNKNOWN / other → not an operator event


def _emit_comms_event(vid, prev, state, ts):
    """Turn a comms-state transition into a first-class event."""
    sev_msg = _comms_event_for(prev, state)
    if sev_msg is None:
        return
    severity, message = sev_msg
    _append_event(
        severity=severity, message=message, etype="comms",
        source="operator-backend", vehicle_id=vid, vehicle=name_of(vid),
        ts=ts.isoformat(),
    )


# Typed vehicle/agent events (e.g. the Local Agent's own comm-state notifications)
# carry a `type` but no severity/message field, so the generic path would render them
# as raw JSON at UNSPEC severity. Map the known types to (severity, message) so the
# Events page shows a clean row. Keeps the event's own `type` and `source`.
TYPED_EVENT_MAP = {
    "comm_recovered":    ("info",    "Local agent communication recovered"),
    "comm_restored":     ("info",    "Local agent communication recovered"),
    "comm_connected":    ("info",    "Local agent communication recovered"),
    "comm_partitioned":  ("caution", "Local agent communication partitioned"),
    "comm_partition":    ("caution", "Local agent communication partitioned"),
    "comm_degraded":     ("caution", "Local agent communication degraded"),
    "comm_lost":         ("warning", "Local agent communication lost"),
    "comm_disconnected": ("warning", "Local agent communication lost"),
}


def normalize_typed_event(ev):
    """(severity, message) for a known typed agent event, else None.
    Appends the reported previous state (`detail.from`) when present, so the row is
    informative without ever falling back to raw JSON."""
    if not isinstance(ev, dict):
        return None
    mapped = TYPED_EVENT_MAP.get(str(ev.get("type") or "").lower())
    if not mapped:
        return None
    severity, message = mapped
    detail = ev.get("detail")
    if isinstance(detail, dict) and detail.get("from"):
        message = f"{message} (from {detail['from']})"
    return severity, message


def derive_event_severity(ev):
    """Severity of a forwarded vehicle event, or None when it carries no level.
    Mirrors the frontend `evSeverity` (lib/ui.js) so it is deterministic and the
    UI never has to re-guess: the backend decides once and stores it."""
    if isinstance(ev, dict):
        raw = str(ev.get("severity") or ev.get("level")
                  or ev.get("priority") or ev.get("sev") or "").lower()
    else:
        raw = ""
    if not raw:
        return None
    if raw.startswith("emerg") or raw in ("critical", "fatal"):
        return "emergency"
    if raw.startswith("warn"):
        return "warning"
    if raw.startswith("caut") or raw in ("alert", "major"):
        return "caution"
    if raw.startswith("info") or raw in ("notice", "debug", "minor"):
        return "info"
    return None


def extract_event_message(ev):
    """Human title of a forwarded event (mirrors frontend `evText`)."""
    if ev is None:
        return ""
    if isinstance(ev, str):
        return ev
    if isinstance(ev, list):
        return " • ".join(str(x) for x in ev)
    if isinstance(ev, dict):
        for k in ("title", "message", "text", "event", "name", "action"):
            if ev.get(k):
                return str(ev[k])
        return json.dumps(ev, sort_keys=True)
    return str(ev)


def to_iso(raw, now):
    """Coerce an event timestamp to ISO-8601 so the whole log is one format.
    Accepts epoch seconds (number or numeric string — local_agent sends time.time())
    or an existing ISO string; anything unparseable falls back to arrival time. Without
    this, epoch-float stamps reach the UI as unparseable strings (wrong order + label)."""
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, timezone.utc).isoformat()
        except (OSError, ValueError, OverflowError):
            return now.isoformat()
    s = str(raw)
    try:
        return datetime.fromtimestamp(float(s), timezone.utc).isoformat()  # numeric epoch string
    except (ValueError, OSError, OverflowError):
        pass
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))  # already ISO — validate, keep
        return s
    except ValueError:
        return now.isoformat()


def event_timestamp(ev, now):
    """The event's own timestamp (normalized to ISO) if it carries one, else arrival."""
    if isinstance(ev, dict):
        raw = (ev.get("timestamp") or ev.get("time") or ev.get("ts")
               or ev.get("created_at") or ev.get("date"))
        if raw is not None and raw != "":
            return to_iso(raw, now)
    return now.isoformat()


def event_fingerprint(vid, ev):
    """Stable identity for a forwarded event so re-sent packets ingest once.
    Prefers an explicit id, else (own timestamp + message); untimestamped repeats
    of identical content collapse to one entry (a log, not per-packet spam)."""
    if isinstance(ev, dict):
        if ev.get("id") is not None:
            return f"{vid}|id={ev['id']}"
        stamp = (ev.get("timestamp") or ev.get("time") or ev.get("ts")
                 or ev.get("created_at") or ev.get("date") or "")
        return f"{vid}|{stamp}|{extract_event_message(ev)}"
    return f"{vid}|{ev}"


def ingest_payload_events(vid, message, now):
    """Store any new vehicle-reported events from a POST /agent/status payload."""
    payload = message.get("payload", message) if isinstance(message, dict) else {}
    if not isinstance(payload, dict):
        return
    events = payload.get("events") or []
    if not isinstance(events, list):
        return
    for ev in events:
        key = event_fingerprint(vid, ev)
        if key in _ingested_event_keys:
            continue
        _ingested_event_keys.add(key)
        etype = (ev.get("type") if isinstance(ev, dict) else None) or "vehicle"
        source = (ev.get("source") if isinstance(ev, dict) else None) or f"usv-{vid}"
        # Known typed events (e.g. local_agent comm notifications) get a clean
        # severity+message; everything else keeps the generic derivation.
        typed = normalize_typed_event(ev)
        if typed is not None:
            severity, message = typed
        else:
            severity, message = derive_event_severity(ev), extract_event_message(ev)
        _append_event(
            severity=severity, message=message,
            etype=etype, source=source,
            vehicle_id=vid, vehicle=name_of(vid),
            ts=event_timestamp(ev, now),
        )


@app.post("/agent/status")
async def receive_agent_status(request: Request):
    global latest_agent_status, latest_agent_received_at

    incoming = await request.json()
    now = datetime.now(timezone.utc)
    vid = extract_usv_id(incoming)

    # Monotonic current-state guard (backend owns "now"): if this packet's own send
    # time is older than the newest we've already accepted for this vehicle, it is a
    # replayed/buffered snapshot — do NOT let it overwrite the current fleet state.
    msg_ts = extract_message_ts(incoming)
    prev_ts = latest_msg_ts_by_id.get(vid)
    stale = msg_ts is not None and prev_ts is not None and msg_ts < prev_ts

    if not stale:
        latest_agent_status = incoming
        if msg_ts is not None:
            latest_msg_ts_by_id[vid] = msg_ts
        name = extract_name(incoming)
        if name:
            vehicle_names[vid] = name
        # Carry forward real telemetry values so a later degraded (telemetry-less)
        # packet renders last-known instead of a fabricated position.
        payload = incoming.get("payload", incoming) if isinstance(incoming, dict) else {}
        tel = payload.get("telemetry") if isinstance(payload, dict) else None
        if isinstance(tel, dict) and tel:
            lk = last_known_telemetry.setdefault(vid, {})
            lk.update({k: v for k, v in tel.items() if v is not None})

    # Arrival-age reachability is about *arrival*, not payload age: any packet that
    # reaches us (even a replayed one) proves the operator link is carrying data now,
    # so refresh last-seen / received-at and log CONNECTED. Buffered events are still
    # ingested as history (deduped) regardless of the snapshot guard.
    latest_agent_received_at = now.isoformat()
    last_seen_by_id[vid] = now
    record_comms_state(vid, "CONNECTED", now, 0.0)
    ingest_payload_events(vid, incoming, now)

    print(f"[OPERATOR] Received agent status{' (stale/replayed — snapshot kept)' if stale else ''}:")
    print(incoming)

    return {
        "ok": True,
        "message": "status received",
        "stale": stale,
        "received_at": latest_agent_received_at,
    }


@app.get("/agent/status")
def get_agent_status():
    return {
        "latest_status": latest_agent_status,
        "received_at": latest_agent_received_at,
    }


@app.get("/api/fleet/status")
def fleet_status():
    fleet = [dict(usv) for usv in FLEET_TEMPLATE]

    if latest_agent_status:
        live_usv = normalize_agent_message(latest_agent_status)

        replaced = False
        for i, usv in enumerate(fleet):
            if usv["id"] == live_usv["id"]:
                fleet[i] = live_usv
                replaced = True

        if not replaced:
            fleet.append(live_usv)

    return fleet


def summarize_comms_durations(transitions, now):
    """Total seconds spent in each comm-state (last segment runs to `now`)."""
    durations = {}
    for i, tr in enumerate(transitions):
        start = datetime.fromisoformat(tr["ts"])
        end = datetime.fromisoformat(transitions[i + 1]["ts"]) if i + 1 < len(transitions) else now
        durations[tr["state"]] = round(durations.get(tr["state"], 0.0) + (end - start).total_seconds(), 1)
    return durations


@app.get("/api/comms/history/{vehicle_id}")
def comms_history(vehicle_id: str):
    """Operator-side comms-state transition log for one vehicle.

    Accepts the id in either form the rest of the system uses — '2' or 'usv-2' (the
    Scout's source id) — so callers don't have to guess. Powers the Map comms
    timeline, the Autonomy decision-trace comms nodes, and the thesis 'total
    disconnected time' metric. Empty transitions => never contacted.
    """
    now = datetime.now(timezone.utc)
    vid = parse_vehicle_id(vehicle_id)
    transitions = comms_history_by_id.get(vid, [])
    return {
        "vehicle_id": vid,
        "current": comms_state_by_id.get(vid, "UNKNOWN"),
        "transitions": transitions,
        "durations_s": summarize_comms_durations(transitions, now),
        "generated_at": now.isoformat(),
    }


@app.get("/api/events")
def get_events(limit: int = 500):
    """Persistent operator event log (comms transitions + vehicle-reported events).

    Single backend source for the Events page (and later Timeline / Replay / stats).
    Returns events in chronological order (oldest→newest); the frontend sorts for
    display. `limit` caps to the most recent N. Empty => nothing has happened yet.
    """
    now = datetime.now(timezone.utc)
    items = event_log[-limit:] if limit and limit > 0 else list(event_log)
    return {
        "events": items,
        "count": len(event_log),
        "generated_at": now.isoformat(),
    }


@app.get("/api/environment")
def environment():
    lat = 56.699893
    lng = 13.002148

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        "&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m"
        "&timezone=Europe%2FStockholm"
    )

    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        current = data.get("current", {})

        return {
            "local_time": datetime.now(ZoneInfo("Europe/Stockholm")).strftime("%H:%M:%S"),
            "temperature": current.get("temperature_2m"),
            "weather_code": current.get("weather_code"),
            "wind_speed": current.get("wind_speed_10m"),
            "wind_direction": current.get("wind_direction_10m"),
        }

    except Exception as e:
        return {
            "error": str(e),
            "local_time": datetime.now(ZoneInfo("Europe/Stockholm")).strftime("%H:%M:%S"),
        }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


# New operator station (design-system frontend). Served alongside the classic
# dashboard at "/" during incremental migration — existing routes are unchanged.
app.mount("/app", StaticFiles(directory=BASE_DIR / "operator", html=True), name="operator")

app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")