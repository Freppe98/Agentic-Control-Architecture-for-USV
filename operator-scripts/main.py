from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import asyncio
import json
import time
import uuid


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

# Last-known agent reasoning per vehicle (payload.agent.*: current_behaviour,
# decision_reason, current_policy, autonomy_level, …). Same store-and-forward rule as
# telemetry: the Agent page shows the LAST KNOWN reasoning (marked stale) when a packet
# omits the group, never a blank. Only ever updated from a real, non-stale packet.
last_known_agent = {}      # {vehicle_id: {agent reasoning dict}}

# Change-tracking for first-class agent/mission events (P3). We record a decision or a
# mission-state event ONLY when the value actually changes — never once per status poll
# (a status arrives ~1 Hz; logging every unchanged one would bury the real transitions).
last_agent_decision_by_id = {}   # {vehicle_id: (behaviour, decision_reason, policy)}
last_mission_state_by_id = {}    # {vehicle_id: mission_state}
last_authority_by_id = {}        # {vehicle_id: last event-recorded effective authority}

# --- Control authority (supervisory: who may command the Pixhawk) ---
# Separate from flight mode, and deliberately NOT part of the command queue below —
# it is vehicle state owned by the Scout Flask service (motherpi/services/flask),
# not an operator-issued mission command. The operator backend holds no authority
# state of its own; every read/write is a live, synchronous proxy to Scout's own
# GET/POST /agent/control_authority (see set_control_authority / get_control_authority
# further down). SCOUT_API_BASE is the same "no Configuration API yet" hardcoded
# per-vehicle map already used by Pilot.js's DASHBOARDS / Terminal.js's SSH_TARGETS —
# only vehicles with a real, reachable Scout Flask instance belong here.
SCOUT_API_BASE = {
    2: "http://10.0.2.10:8080",  # Scout — motherpi Flask API
}


def scout_api_base(vid: int):
    return SCOUT_API_BASE.get(vid)

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


def _first_present(*vals):
    """First value that is not None (used to merge candidate field spellings)."""
    for v in vals:
        if v is not None:
            return v
    return None


def _age_seconds_from(raw):
    """Seconds since an epoch or ISO-8601 timestamp, or None. Used to convert a
    Scout-provided 'last heartbeat / last message time' into a freshness age."""
    if raw is None or raw == "":
        return None
    ts = extract_message_ts({"timestamp": raw})  # reuses the epoch/ISO parser
    if ts is None:
        return None
    return max(0.0, (datetime.now(timezone.utc).timestamp() - ts))


def mavlink_evidence(payload: dict) -> dict:
    """Normalized MAVLink / Pixhawk-heartbeat evidence for the diagnostics page.

    Read STRICTLY from real link fields Scout may forward — never inferred from GPS or
    arrival age (a HEARTBEAT is its own MAVLink message; GPS position is not proof of
    one). When Scout exposes none of these, every field is None and the operator
    diagnostics render NOT AVAILABLE rather than a fabricated PASS. The candidate
    spellings below are the Scout-side schema this consumer expects (see
    BACKEND_ROADMAP.md → 'Pixhawk heartbeat / MAVLink evidence')."""
    comm = payload.get("communication", {}) or {}
    health = payload.get("health", {}) or {}
    mav = payload.get("mavlink") or comm.get("mavlink") or health.get("mavlink") or {}
    if not isinstance(mav, dict):
        mav = {}

    heartbeat_age = _first_present(
        mav.get("heartbeat_age_s"), comm.get("heartbeat_age_s"),
        health.get("pixhawk_heartbeat_age_s"), health.get("heartbeat_age_s"),
        _age_seconds_from(_first_present(mav.get("last_heartbeat"), comm.get("last_heartbeat"),
                                         health.get("last_heartbeat"))),
    )
    last_msg_age = _first_present(
        mav.get("last_msg_age_s"), comm.get("mavlink_last_msg_age_s"),
        _age_seconds_from(_first_present(mav.get("last_msg_time"), comm.get("mavlink_last_message"))),
    )
    return {
        "connected": _first_present(mav.get("connected"), comm.get("mavlink_connected")),
        "heartbeat_age_s": round(heartbeat_age, 2) if isinstance(heartbeat_age, (int, float)) else None,
        "last_msg_age_s": round(last_msg_age, 2) if isinstance(last_msg_age, (int, float)) else None,
        "msg_rate_hz": _first_present(mav.get("msg_rate_hz"), comm.get("mavlink_msg_rate_hz")),
        "parser_errors": _first_present(mav.get("parser_errors"), comm.get("mavlink_parser_errors")),
    }


def haversine_m(a_lat, a_lng, b_lat, b_lng):
    """Great-circle distance in metres between two lat/lng points, or None if any
    coordinate is missing. Used for the Home ↔ Scout distance the operator reads before
    committing a deployment (and for the read-back verification tolerance check)."""
    for v in (a_lat, a_lng, b_lat, b_lng):
        if v is None:
            return None
    from math import radians, sin, cos, asin, sqrt
    R = 6371000.0
    d_lat = radians(float(b_lat) - float(a_lat))
    d_lng = radians(float(b_lng) - float(a_lng))
    la, lb = radians(float(a_lat)), radians(float(b_lat))
    h = sin(d_lat / 2) ** 2 + cos(la) * cos(lb) * sin(d_lng / 2) ** 2
    return round(2 * R * asin(min(1.0, sqrt(h))), 2)


# --- Vehicle Home (Pixhawk HOME_POSITION / RTL recovery point) ---
# Two independent facts, kept separate and never conflated:
#   (1) the live HOME_POSITION Scout forwards in status (where the Pixhawk *thinks*
#       home is) — surfaced in the fleet payload's `home` block, freshness and all; and
#   (2) whether the operator has SET and read-back-VERIFIED that home at the current
#       deployment site — operator-backend state recorded only on a confirmed set-home
#       (see the set-home endpoint). Verification is what gates AUTO/RTL/RESUME in the UI.
# The backend never fabricates a home: no HOME_POSITION reported => available:false =>
# the UI reads "HOME UNKNOWN", never a garage/zero default.
HOME_VERIFY_TOLERANCE_M = 5.0     # read-back must land within this of the commanded point
home_verification_by_id = {}      # {vid: {verified, verified_at, lat, lng, distance_m}}


def _extract_home(payload: dict, telemetry: dict):
    """Pull a live HOME_POSITION (lat/lng + optional freshness) out of a status payload,
    tolerant of the spellings Scout may forward. Returns {lat, lng, age_s} or None when
    the vehicle has not reported a home — an absent home is honest, never zero-filled."""
    src = None
    for cand in (payload.get("home_position"), payload.get("home"),
                 telemetry.get("home_position"), telemetry.get("home")):
        if isinstance(cand, dict):
            src = cand
            break
    if src is None:
        return None
    lat = _mission_coord(src.get("lat"), src.get("latitude"), src.get("x"))
    lng = _mission_coord(src.get("lng"), src.get("lon"), src.get("longitude"), src.get("y"))
    if lat is None or lng is None:
        return None
    age = _age_seconds_from(_first_present(src.get("timestamp"), src.get("received_at"),
                                           src.get("time"), src.get("age_s")))
    return {"lat": lat, "lng": lng, "age_s": round(age, 1) if isinstance(age, (int, float)) else None}


def home_block(vid: int, payload: dict, telemetry: dict):
    """The fleet-payload `home` block for one vehicle: the live Pixhawk HOME_POSITION
    (or available:false) merged with the operator's read-back verification record."""
    live = _extract_home(payload, telemetry)
    ver = home_verification_by_id.get(vid) or {}
    return {
        "source": "pixhawk",
        "available": live is not None,
        "lat": live["lat"] if live else None,
        "lng": live["lng"] if live else None,
        "age_s": live["age_s"] if live else None,
        "verified": bool(ver.get("verified")),
        "verified_at": ver.get("verified_at"),
        "verified_distance_m": ver.get("distance_m"),
    }


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
    agent_reasoning = payload.get("agent", {}) or {}

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
        # Vehicle Home (Pixhawk HOME_POSITION / RTL recovery point) + the operator's
        # read-back verification record. Distinct from `lat`/`lng` (the vehicle's own
        # position) — the map plots them as two separate markers.
        "home": home_block(usv_id, payload, telemetry),
        "mission_data": payload.get("mission", {}) or {},
        "communication": payload.get("communication", {}) or {},
        "health": payload.get("health", {}) or {},
        "mavlink": mavlink_evidence(payload),
        "measurements": payload.get("measurements", {}) or {},
        "events": payload.get("events", []) or [],
        # Agent reasoning (payload.agent.*) forwarded verbatim for the Agent page:
        # current_behaviour, decision_reason, current_policy, autonomy_level,
        # current_communication_state, current_mission_state, buffer_usage,
        # last_operator_command. Falls back to the last-known reasoning (marked stale by
        # the frontend via comm-state) when a degraded packet omits the group.
        "agent_status": agent_reasoning or last_known_agent.get(usv_id, {}),
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
            expire_commands()
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


def _clean(val):
    """A displayable value, or None for absent/placeholder (None/""/'unknown'/'none')."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("unknown", "none", "n/a", "null"):
        return None
    return s


def record_agent_changes(vid, payload, now):
    """Record first-class events for agent-decision and mission-state CHANGES (P3).

    Emitted only when the value actually changes vs. the last recorded one — never once
    per status poll — so the event log shows real transitions with timestamps + reasons,
    not a flood of unchanged rows. The agent-decision key is (current_behaviour,
    decision_reason, current_policy) from payload.agent.*; the mission key is
    mission.mission_state. Reasons are taken verbatim from the agent (never invented)."""
    if not isinstance(payload, dict):
        return
    agent = payload.get("agent") or {}
    mission = payload.get("mission") or {}
    name = name_of(vid)

    if isinstance(agent, dict) and agent:
        behaviour = _clean(agent.get("current_behaviour") or agent.get("behaviour")
                           or agent.get("behavior"))
        reason = _clean(agent.get("decision_reason") or agent.get("decision_rationale"))
        policy = _clean(agent.get("current_policy") or agent.get("communication_policy"))
        key = (behaviour, reason, policy)
        prev = last_agent_decision_by_id.get(vid)
        # Only meaningful once the agent actually reports a behaviour/decision at least once.
        if key != prev and (behaviour or reason):
            last_agent_decision_by_id[vid] = key
            if prev is not None:  # skip the very first observation (not a transition)
                label = behaviour or "decision updated"
                msg = f"Agent decision: {label}"
                if reason:
                    msg = f"{msg} — {reason}"
                _append_event(severity="info", message=msg, etype="agent",
                              source=f"usv-{vid}", vehicle_id=vid, vehicle=name,
                              ts=now.isoformat())

    if isinstance(mission, dict) and mission:
        mstate = _clean(mission.get("mission_state"))
        prev_m = last_mission_state_by_id.get(vid)
        if mstate and mstate != prev_m:
            last_mission_state_by_id[vid] = mstate
            if prev_m is not None:
                _append_event(severity="info",
                              message=f"Mission state: {prev_m} → {mstate}",
                              etype="mission", source=f"usv-{vid}",
                              vehicle_id=vid, vehicle=name, ts=now.isoformat())


# --- Command queue (BACKEND_ROADMAP: reverse/control Path E; Operator-backend-owned) ---
# The smallest safe Operator → Scout command path. The operator backend is the queue's
# source of truth: it stores command records, gates them on the operator-side comm-state,
# hands pending ones to the Local Agent on next contact, and records the Agent's result.
# It NEVER fabricates execution — only a Local Agent result can mark a command EXECUTED
# (SYSTEM_INFORMATION_MODEL: the backend stores operator-side records, it does not decide
# what the vehicle did). In-memory like event_log / comms history (resets on restart).
#
# Lifecycle owners:
#   QUEUED  — backend, on create
#   SENT    — backend, when the Agent fetches it (a claim; at-least-once redelivery)
#   ACCEPTED/EXECUTED/REJECTED/FAILED — Local Agent only, via the result endpoint
#   EXPIRED — backend, when a non-terminal command passes its TTL (monitor loop)
# Vehicle/Pixhawk modes are real ArduRover modes (MANUAL, AUTO, HOLD, LOITER, GUIDED,
# RTL). MISSION_PAUSE/MISSION_RESUME are agent mission commands — NOT Pixhawk modes —
# and are kept deliberately distinct. ARM/DISARM are the safety pair.
COMMAND_TYPES = {
    "SET_MODE_AUTO", "SET_MODE_MANUAL", "SET_MODE_HOLD", "SET_MODE_LOITER",
    "SET_MODE_GUIDED", "RTL", "MISSION_PAUSE", "MISSION_RESUME",
    "ARM", "DISARM", "SET_HOME",
}
# Arming touches the motors, so both ARM and DISARM ALWAYS require an explicit
# confirm:true (independent of comm-state) — the backend rejects them otherwise.
# ARM is the higher risk of the two (the vehicle can move under power once armed);
# its record carries a caution warning. Still just the queue — no execution here.
CONFIRM_REQUIRED_TYPES = {"ARM", "DISARM", "SET_HOME"}
RISK_WARNING = {
    "ARM": "High-risk: the vehicle can move under power once armed. Confirmed by operator.",
    "DISARM": "Disarms the vehicle (motors off). Confirmed by operator.",
    "SET_HOME": "Sets the Pixhawk HOME / RTL recovery point to the Scout's current position. Confirmed by operator.",
}
COMMAND_TTL_S = 300              # queued commands survive ~5 min of disconnection, then EXPIRE
TERMINAL_STATUSES = {"EXECUTED", "REJECTED", "FAILED", "EXPIRED"}
RESULT_STATUSES = {"ACCEPTED", "EXECUTED", "REJECTED", "FAILED"}  # what an Agent may report

commands = []              # append-only [ command record ] (see the spec object below)
commands_by_id = {}        # {id: record} — uuid id is the dedup key (no duplicate execution)


def known_vehicle_ids():
    """Vehicle ids the backend recognises (template + any that have reported)."""
    return set(vehicle_names) | {u["id"] for u in FLEET_TEMPLATE}


def _command_event(cmd, *, severity, message, source):
    """Record a command lifecycle change as a first-class event (Events page, no change)."""
    _append_event(
        severity=severity, message=message, etype="command", source=source,
        vehicle_id=cmd["vehicle_id"], vehicle=cmd["vehicle"],
    )


def make_command(*, vid, ctype, params, created_by, comm_state, now):
    """Build + store a QUEUED command record (the spec command object)."""
    cmd = {
        "id": str(uuid.uuid4()),
        "vehicle_id": vid,
        "vehicle": name_of(vid),
        "type": ctype,
        "params": params or {},
        "status": "QUEUED",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=COMMAND_TTL_S)).isoformat(),
        "created_by": created_by or "operator",
        "requested_comm_state": comm_state,   # operator-side link state at creation
        "claimed_at": None,                   # set when the Agent first fetches it (SENT)
        "completed_at": None,                 # set on any terminal status
        "result": None,                       # Agent-reported result payload/string
        "reason": None,                       # rejection/failure/expiry reason
        "warning": None,                      # e.g. queued while PARTITIONED
    }
    commands.append(cmd)
    commands_by_id[cmd["id"]] = cmd
    return cmd


def expire_commands(now=None):
    """Flip any non-terminal command past its TTL to EXPIRED (backend-owned, once)."""
    now = now or datetime.now(timezone.utc)
    for cmd in commands:
        if cmd["status"] in TERMINAL_STATUSES:
            continue
        try:
            deadline = datetime.fromisoformat(cmd["expires_at"])
        except (TypeError, ValueError):
            continue
        if now >= deadline:
            cmd["status"] = "EXPIRED"
            cmd["completed_at"] = now.isoformat()
            cmd["reason"] = cmd["reason"] or "Expired before delivery/execution"
            _command_event(cmd, severity="warning",
                           message=f"Command {cmd['type']} expired",
                           source="operator-backend")


def apply_command_result(cmd, new_status, result, reason, now):
    """Apply a Local-Agent-reported result. Idempotent: a result on an already-terminal
    command is ignored (the uuid id prevents duplicate execution). Returns True if applied."""
    if cmd["status"] in TERMINAL_STATUSES:
        return False
    cmd["status"] = new_status
    cmd["result"] = result
    cmd["reason"] = reason
    if new_status in TERMINAL_STATUSES:
        cmd["completed_at"] = now.isoformat()
    return True


# Field spellings a Local Agent might use for the command id / lifecycle status /
# detail / reason, so the result receiver accepts either endpoint's payload shape.
_RESULT_ID_KEYS = ("command_id", "id", "cmd_id", "commandId", "uuid")
_RESULT_STATUS_KEYS = ("status", "result_status", "outcome", "state")
_RESULT_REASON_KEYS = ("reason", "error", "message", "detail")
# A Local Agent may report TIMEOUT/ACK/DONE/OK — normalize to the lifecycle vocabulary
# the queue owns (RESULT_STATUSES) so no valid outcome is dropped as "invalid".
_RESULT_STATUS_ALIASES = {
    "TIMEOUT": "FAILED", "TIMED_OUT": "FAILED", "TIMEDOUT": "FAILED",
    "ERROR": "FAILED", "FAIL": "FAILED", "FAILURE": "FAILED",
    "REJECT": "REJECTED", "DENIED": "REJECTED",
    "ACK": "ACCEPTED", "ACKNOWLEDGED": "ACCEPTED",
    "DONE": "EXECUTED", "COMPLETE": "EXECUTED", "COMPLETED": "EXECUTED",
    "SUCCESS": "EXECUTED", "OK": "EXECUTED", "EXECUTE": "EXECUTED",
}


def _pick(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def normalize_result_status(raw):
    """Map a reported status onto the queue's RESULT_STATUSES vocabulary, or None."""
    s = str(raw or "").upper().strip()
    s = _RESULT_STATUS_ALIASES.get(s, s)
    return s if s in RESULT_STATUSES else None


def process_command_result(command_id, raw_status, result, reason, now):
    """Look up a command and apply a Local-Agent-reported result. Single source of truth
    shared by BOTH result endpoints (POST /agent/command_result and the id-in-path
    /api/commands/{id}/result). Idempotent by the uuid command id: a duplicate/replayed
    result on an already-terminal command is a no-op (applied:false) — no double history
    row, no double execution. Returns a per-item dict; never raises."""
    if not command_id:
        return {"command_id": command_id, "found": False, "applied": False,
                "error": "missing command id"}
    cmd = commands_by_id.get(str(command_id))
    if cmd is None:
        # Unknown id: acknowledge without failing so a buffered backlog can still flush
        # (the Agent drops it from its buffer) — the record simply no longer exists here.
        return {"command_id": command_id, "found": False, "applied": False,
                "error": "unknown command id"}
    new_status = normalize_result_status(raw_status)
    if new_status is None:
        return {"command_id": command_id, "found": True, "applied": False,
                "error": "invalid result status", "status": raw_status,
                "allowed": sorted(RESULT_STATUSES)}
    applied = apply_command_result(cmd, new_status, result, reason, now)
    if applied:
        sev = "warning" if new_status in ("REJECTED", "FAILED") else "info"
        msg = f"Command {cmd['type']} {new_status.lower()}"
        if cmd.get("reason"):
            msg = f"{msg} — {cmd['reason']}"
        _command_event(cmd, severity=sev, message=msg, source=f"usv-{cmd['vehicle_id']}")
    return {"command_id": command_id, "found": True, "applied": applied,
            "status": new_status, "command": cmd}


def _result_items(body):
    """Normalize a command-result request body into a list of result dicts. Accepts a
    single object, a bare JSON list, or {results:[...]}/{command_results:[...]} — so a
    single ack and a flushed backlog both work through one endpoint."""
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for key in ("results", "command_results", "items", "acks"):
            if isinstance(body.get(key), list):
                return [x for x in body[key] if isinstance(x, dict)]
        return [body]
    return []


@app.post("/api/commands")
async def create_command(request: Request):
    """Create a command for a vehicle (Operator UI / curl). Validates the type and
    vehicle, then gates on the OPERATOR-side comm-state:
      CONNECTED   → queued immediately
      PARTITIONED → queued with a warning
      DISCONNECTED→ 409 needs_confirmation, unless body has confirm:true (then queued;
                    it survives until next contact or the TTL).
    High-risk arming (ARM/DISARM) ALWAYS needs confirm:true regardless of comm-state
    (409 otherwise); ARM additionally carries a caution warning in its record.
    Never marks the command executed — that is the Local Agent's result only.
    Body: { vehicle_id, type, params?, created_by?, confirm? }"""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    body = await request.json()

    ctype = str(body.get("type") or "").upper()
    if ctype not in COMMAND_TYPES:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "unknown command type",
            "type": body.get("type"), "allowed": sorted(COMMAND_TYPES)})

    vid = parse_vehicle_id(body.get("vehicle_id"))
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": body.get("vehicle_id")})

    comm_state = comms_state_by_id.get(vid, "UNKNOWN")
    confirm = bool(body.get("confirm"))

    # High-risk arming: ARM/DISARM always require confirm:true, whatever the link state.
    if ctype in CONFIRM_REQUIRED_TYPES and not confirm:
        return JSONResponse(status_code=409, content={
            "ok": False, "needs_confirmation": True, "type": ctype,
            "high_risk": ctype == "ARM",
            "message": f"{ctype} affects the motors and requires explicit confirmation. "
                       "Resend with confirm:true."})
    # Disconnected: queue-until-contact still needs a confirmation for any command.
    if comm_state == "DISCONNECTED" and not confirm:
        return JSONResponse(status_code=409, content={
            "ok": False, "needs_confirmation": True, "comm_state": comm_state,
            "message": "Vehicle is DISCONNECTED — command will queue until next contact. "
                       "Resend with confirm:true to queue it."})

    cmd = make_command(vid=vid, ctype=ctype, params=body.get("params"),
                       created_by=body.get("created_by"), comm_state=comm_state, now=now)

    # Accumulate any warnings (risk + link state) into the record; a warning implies caution.
    warnings = []
    if ctype in RISK_WARNING:
        warnings.append(RISK_WARNING[ctype])
    if comm_state == "PARTITIONED":
        warnings.append("Queued while communication is partitioned — delivery may be delayed.")
    elif comm_state == "DISCONNECTED":
        warnings.append("Queued while disconnected — will deliver on next contact.")
    if warnings:
        cmd["warning"] = " ".join(warnings)
    severity = "caution" if warnings else "info"
    _command_event(cmd, severity=severity,
                   message=f"Command {ctype} created ({comm_state})",
                   source="operator-backend")
    return {"ok": True, "command": cmd}


@app.get("/api/commands/pending/{vehicle_id}")
def pending_commands(vehicle_id: str):
    """Commands awaiting the Local Agent for one vehicle. This fetch is the CLAIM: a
    QUEUED command transitions to SENT (claimed_at stamped) and is redelivered while
    SENT (at-least-once) until a result arrives — the Agent dedups by the command id."""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    vid = parse_vehicle_id(vehicle_id)
    pending = []
    for cmd in commands:
        if cmd["vehicle_id"] != vid or cmd["status"] in TERMINAL_STATUSES:
            continue
        if cmd["status"] == "QUEUED":
            cmd["status"] = "SENT"
            cmd["claimed_at"] = now.isoformat()
            _command_event(cmd, severity="info",
                           message=f"Command {cmd['type']} sent to {cmd['vehicle']}",
                           source="operator-backend")
        pending.append(cmd)
    return {"vehicle_id": vid, "pending": pending, "generated_at": now.isoformat()}


@app.post("/api/commands/{command_id}/result")
async def command_result(command_id: str, request: Request):
    """Local Agent reports the outcome of a command (id in the PATH). Body:
    { status, result?, reason? } where status ∈ ACCEPTED|EXECUTED|REJECTED|FAILED.
    Idempotent — a result on an already-terminal command is a no-op (applied:false),
    so a re-sent ack never double-executes. This is the ONLY way a command becomes
    EXECUTED. Scout may instead POST /agent/command_result with the id in the body
    (same lifecycle, backlog-friendly) — both share process_command_result."""
    now = datetime.now(timezone.utc)
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    outcome = process_command_result(
        command_id, _pick(body, _RESULT_STATUS_KEYS),
        body.get("result"), _pick(body, _RESULT_REASON_KEYS), now)
    if not outcome["found"]:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": outcome.get("error", "unknown command id"),
            "command_id": command_id})
    if outcome.get("error"):
        return JSONResponse(status_code=400, content={
            "ok": False, "error": outcome["error"], "status": outcome.get("status"),
            "allowed": outcome.get("allowed")})
    return {"ok": True, "applied": outcome["applied"], "command": outcome["command"]}


@app.post("/agent/command_result")
async def agent_command_result(request: Request):
    """Command-result receiver expected by the Local Agent (Scout). The command id is in
    the BODY, not the path — this is the endpoint Scout was POSTing to (previously 405
    because only the id-in-path route existed). Accepts:
      • a single result:  { command_id, status, result?, reason?, vehicle_id? }
      • a flushed backlog: [ {...}, {...} ]  or  { results: [ {...}, ... ] }
    Tolerant of field spellings (command_id/id/cmd_id, status/outcome, reason/error) and
    status aliases (TIMEOUT/ACK/DONE → the queue vocabulary). Idempotent by the uuid
    command id: replayed/duplicate results are no-ops (applied:false) — a flushed buffer
    never creates duplicate history rows or re-executes. ALWAYS 2xx (per-item found/
    applied flags carry the detail) so a buffered Agent can drain its backlog and stop
    retrying even when some ids are unknown/already-terminal here."""
    now = datetime.now(timezone.utc)
    try:
        body = await request.json()
    except Exception:
        body = None
    items = _result_items(body)
    if not items:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "no command results in body",
            "expected": "{command_id, status} | [ ... ] | {results:[ ... ]}"})

    results = [
        process_command_result(
            _pick(it, _RESULT_ID_KEYS), _pick(it, _RESULT_STATUS_KEYS),
            it.get("result"), _pick(it, _RESULT_REASON_KEYS), now)
        for it in items
    ]
    # Strip the full command echo from batch items to keep the ack compact; a single
    # result keeps it for parity with the id-in-path endpoint.
    applied_n = sum(1 for r in results if r["applied"])
    if len(results) == 1:
        r = results[0]
        return {"ok": True, "applied": r["applied"], "found": r["found"],
                "error": r.get("error"), "command": r.get("command"),
                "received": 1, "applied_count": applied_n}
    compact = [{k: v for k, v in r.items() if k != "command"} for r in results]
    return {"ok": True, "received": len(results), "applied_count": applied_n,
            "results": compact}


# --- Control authority (direct proxy to Scout Flask, NOT the command queue above) ---
# Take Control / Release Control in the Operator UI. Deliberately bypasses the
# QUEUED→SENT→EXECUTED command lifecycle entirely: control authority is vehicle
# state owned by Scout's own Flask service (motherpi/services/flask), reachable at
# SCOUT_API_BASE. The operator backend holds no authority state of its own — every
# call here is a live, synchronous round-trip to Scout; a network failure surfaces
# as an honest reachable:false, never a guessed or cached value.
#
# Effective-authority semantics (the axis the operator acts on):
#   OPERATOR     — the operator holds the wheel; operator commands may execute.
#                  Requested via "Take Control".
#   LOCAL_AGENT  — the autonomous local agent holds the wheel; the mission runs.
#                  Requested via "Release Control".
#   RC           — an RC transmitter override has physically taken over. This is a
#                  *reported* effective state only: the operator can never request it
#                  (it is a hardware takeover), so it appears in reads, never in writes.
REQUESTABLE_AUTHORITY = ("OPERATOR", "LOCAL_AGENT")   # operator-initiated (POST)
REPORTABLE_AUTHORITY = ("OPERATOR", "LOCAL_AGENT", "RC")  # what a read may surface


def _scout_authority_read(vid: int, base: str):
    """Live GET of Scout's authority, normalized to a stable operator schema. Always
    returns a dict, never raises: an unreachable Scout is an honest reachable:false /
    authority:null so the frontend's 2 s poll never emits a console 4xx/5xx."""
    try:
        r = requests.get(f"{base}/agent/control_authority", timeout=3)
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        return {
            "ok": True, "vehicle_id": vid, "available": True, "reachable": False,
            "authority": None, "source": "scout",
            "reason": "Scout control-authority API unreachable", "detail": str(exc),
        }
    raw = str(data.get("authority") or data.get("effective")
              or data.get("control_authority") or "").upper()
    value = raw if raw in REPORTABLE_AUTHORITY else None
    return {
        "ok": True, "vehicle_id": vid, "available": True, "reachable": True,
        "authority": value, "source": "scout", "raw": data,
    }


def cancel_pending_commands(vid: int, now, reason: str):
    """Terminate every non-terminal command for one vehicle. Reuses the existing
    EXPIRED terminal status — no new lifecycle/state machine — so a command left
    QUEUED/SENT while OPERATOR held authority can never fire once LOCAL_AGENT is
    re-engaged later. Queue-only; never touches Scout's own authority value."""
    for cmd in commands:
        if cmd["vehicle_id"] != vid or cmd["status"] in TERMINAL_STATUSES:
            continue
        cmd["status"] = "EXPIRED"
        cmd["completed_at"] = now.isoformat()
        cmd["reason"] = reason
        _command_event(cmd, severity="warning",
                       message=f"Command {cmd['type']} cancelled ({reason})",
                       source="operator-backend")


@app.post("/api/control_authority/{vehicle}")
async def set_control_authority(vehicle: str, request: Request):
    """Body: { "authority": "OPERATOR" | "LOCAL_AGENT" }. Take Control → OPERATOR,
    Release Control → LOCAL_AGENT (RC is a hardware takeover and is NOT requestable).
    Forwards to Scout's POST /agent/control_authority and returns a normalized ack
    ({requested, authority} where authority is Scout's acknowledged effective value)
    so the frontend confirms against the effective state, not the button press.

    On a confirmed Release (authority=LOCAL_AGENT) also cancels any still-pending
    command-queue entries for this vehicle (queue safety — see cancel_pending_commands)
    so a stale operator command cannot fire once autonomy is back in control. Take
    Control does NOT cancel — the operator is taking the wheel. Scout remains the sole
    source of truth for the authority value itself."""
    body = await request.json()
    authority = str(body.get("authority") or "").upper()
    if authority not in REQUESTABLE_AUTHORITY:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "invalid authority",
            "authority": body.get("authority"), "allowed": list(REQUESTABLE_AUTHORITY)})

    vid = parse_vehicle_id(vehicle)
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": vehicle})

    base = scout_api_base(vid)
    if base is None:
        return JSONResponse(status_code=409, content={
            "ok": False, "available": False, "vehicle_id": vid,
            "error": "no Scout control-authority API configured for this vehicle",
            "message": "This vehicle has no control-authority backend; authority cannot be changed."})

    try:
        r = requests.post(f"{base}/agent/control_authority",
                          json={"authority": authority}, timeout=3)
        r.raise_for_status()
        result = r.json() if r.content else {}
    except requests.RequestException as exc:
        return JSONResponse(status_code=502, content={
            "ok": False, "error": "Scout control-authority API unreachable",
            "detail": str(exc)})

    if authority == "LOCAL_AGENT":
        cancel_pending_commands(vid, datetime.now(timezone.utc),
                                 "Cancelled — control released to Local Agent")

    eff = str(result.get("authority") or result.get("effective") or authority).upper()

    # First-class control-authority event (P3). Records the operator-initiated hand-off
    # with the Scout-confirmed effective value — a real transition worth a timestamp,
    # deduped so repeating the same request does not spam the log.
    eff_val = eff if eff in REPORTABLE_AUTHORITY else authority
    if last_authority_by_id.get(vid) != eff_val:
        last_authority_by_id[vid] = eff_val
        human = "Operator" if eff_val == "OPERATOR" else "Local Agent" if eff_val == "LOCAL_AGENT" else eff_val
        verb = "Take Control" if authority == "OPERATOR" else "Release Control"
        _append_event(severity="caution" if eff_val == "OPERATOR" else "info",
                      message=f"Control authority → {human} ({verb})",
                      etype="authority", source="operator-backend",
                      vehicle_id=vid, vehicle=name_of(vid))
    return {
        "ok": True, "vehicle_id": vid, "requested": authority,
        "authority": eff if eff in REPORTABLE_AUTHORITY else None,
        "available": True, "reachable": True, "source": "scout", "raw": result,
    }


@app.get("/api/control_authority/{vehicle}")
def get_control_authority(vehicle: str):
    """Live read of Scout's authority, normalized. Distinguishes three cases so the
    2 s poll is quiet and honest:
      unknown vehicle id          → deliberate JSON 404 (no such vehicle)
      known, no Scout API          → 200 available:false (nothing to read here)
      known, Scout API configured  → live proxy; reachable:false + authority:null if
                                      Scout does not answer (never a console 5xx)."""
    vid = parse_vehicle_id(vehicle)
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": vehicle})

    base = scout_api_base(vid)
    if base is None:
        return {
            "ok": True, "vehicle_id": vid, "available": False, "reachable": False,
            "authority": None, "source": "scout",
            "reason": "No Scout control-authority API configured for this vehicle",
        }
    return _scout_authority_read(vid, base)


# --- Pixhawk mission (direct proxy to Scout Flask, NOT the command queue) ---
# View-only readback of the mission currently STORED ON THE PIXHAWK for a vehicle
# (MISSION_REQUEST_LIST/MISSION_ITEM_INT over MAVLink, performed on Scout). Same
# proxy pattern as control_authority: the operator backend holds NO mission state of
# its own — every fetch is a live, synchronous round-trip to Scout's own Flask API
# (SCOUT_API_BASE). A network failure surfaces as an honest reachable:false with an
# empty waypoint list, never a fabricated or cached mission. This is deliberately a
# separate axis from the operator command queue and from `mission_state` progress:
# it is what the flight controller actually holds, for testing/verification. The card
# is designed to later carry Compare / Upload / Export against this same payload.
#
# Scout-side contract (to be exposed by motherpi/services/flask, mirroring
# /agent/control_authority): GET /agent/pixhawk_mission returning any of the shapes
# normalized below. Until Scout ships it, this endpoint returns reachable:false and
# the operator UI reads "Scout unavailable" — never a guessed mission.

# MAVLink MAV_CMD codes → readable names for the waypoint popup. Unmapped codes fall
# back to "CMD <n>" so an unusual command is still shown honestly, never blanked.
MAV_CMD_NAMES = {
    16: "WAYPOINT", 17: "LOITER_UNLIM", 18: "LOITER_TURNS", 19: "LOITER_TIME",
    20: "RETURN_TO_LAUNCH", 21: "LAND", 22: "TAKEOFF", 82: "SPLINE_WAYPOINT",
    84: "VTOL_TAKEOFF", 85: "VTOL_LAND", 93: "DELAY", 177: "DO_JUMP",
    178: "DO_CHANGE_SPEED", 183: "DO_SET_SERVO", 189: "DO_LAND_START",
}
# Commands whose param1 encodes a loiter/hold time in seconds.
LOITER_TIME_CMDS = {19}


def _mission_coord(*vals):
    """First usable lat/lng from candidate fields. Accepts float degrees OR MAVLink
    int1e7 (MISSION_ITEM_INT x/y): anything with |value| > 180 is treated as degrees*1e7.
    Returns None when nothing usable is present (a waypoint with no position stays
    positionless rather than plotting at 0,0)."""
    for v in vals:
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if abs(f) > 180:
            f = f / 1e7
        if f == 0:
            continue
        return f
    return None


def normalize_mission_item(item):
    """One Scout/MAVLink mission item → the stable waypoint schema the map overlay
    consumes. Tolerant of field-name spellings (seq/sequence, command/cmd, lat/x, …)
    so it works whether Scout forwards raw pymavlink items or a pre-shaped list."""
    if not isinstance(item, dict):
        return None
    seq = _first_present(item.get("seq"), item.get("sequence"), item.get("index"))
    cmd = _first_present(item.get("command"), item.get("cmd"), item.get("mav_cmd"))
    lat = _mission_coord(item.get("lat"), item.get("latitude"), item.get("x"))
    lng = _mission_coord(item.get("lng"), item.get("lon"), item.get("longitude"), item.get("y"))
    alt = _first_present(item.get("alt"), item.get("altitude"), item.get("z"))
    loiter = item.get("loiter_time")
    if loiter is None and cmd in LOITER_TIME_CMDS:
        loiter = item.get("param1")
    try:
        cmd_int = int(cmd) if cmd is not None else None
    except (TypeError, ValueError):
        cmd_int = None
    return {
        "seq": int(seq) if isinstance(seq, (int, float)) or (isinstance(seq, str) and seq.isdigit()) else seq,
        "command": cmd_int if cmd_int is not None else cmd,
        "command_name": item.get("command_name") or (MAV_CMD_NAMES.get(cmd_int, f"CMD {cmd_int}") if cmd_int is not None else None),
        "lat": lat,
        "lng": lng,
        "alt": round(float(alt), 2) if isinstance(alt, (int, float)) else None,
        "loiter_time": round(float(loiter), 1) if isinstance(loiter, (int, float)) else None,
        "frame": item.get("frame"),
    }


def _scout_mission_read(vid: int, base: str, now):
    """Live GET of the Pixhawk mission from Scout, normalized. Always returns a dict,
    never raises: an unreachable Scout is reachable:false with an empty list so the
    frontend's fetch never emits a console 4xx/5xx. `partial` marks an incomplete
    download (Scout said so, or fewer items arrived than the reported count)."""
    try:
        r = requests.get(f"{base}/agent/pixhawk_mission", timeout=8)
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        return {
            "ok": True, "vehicle_id": vid, "available": True, "reachable": False,
            "fetched_at": now.isoformat(), "count": 0, "current_seq": None,
            "waypoints": [], "partial": False, "source": "scout",
            "reason": "Scout mission API unreachable", "detail": str(exc),
        }
    if not isinstance(data, dict):
        data = {}
    raw_items = (data.get("waypoints") or data.get("mission")
                 or data.get("items") or data.get("mission_items") or [])
    if not isinstance(raw_items, list):
        raw_items = []
    waypoints = [w for w in (normalize_mission_item(i) for i in raw_items) if w is not None]
    current = _first_present(data.get("current_seq"), data.get("current"),
                             data.get("current_wp"), data.get("current_waypoint"))
    try:
        current = int(current) if current is not None else None
    except (TypeError, ValueError):
        current = None
    # partial: Scout flagged it, OR the mission list is shorter than the count it
    # reported (a download that dropped items). Never claim complete when it isn't.
    reported = data.get("count")
    partial = bool(data.get("partial"))
    if isinstance(reported, int) and reported > len(waypoints):
        partial = True
    out = {
        "ok": True, "vehicle_id": vid, "available": True, "reachable": True,
        "fetched_at": now.isoformat(), "count": len(waypoints),
        "current_seq": current, "waypoints": waypoints, "partial": partial,
        "source": "scout", "raw_count": reported,
    }
    # Optional Scout-provided integrity/identity fields for a future mission-compare
    # (hash of the on-FC mission, whether a mission is loaded, whether it validated).
    # Passed through ONLY when Scout actually sends them — never fabricated here. The
    # operator backend stays a pure proxy: it does not compute a hash or a validity of
    # its own (that would duplicate mission ownership Scout holds).
    for k in ("hash", "loaded", "valid"):
        if k in data:
            out[k] = data[k]
    return out


@app.get("/api/vehicles/{vehicle_id}/pixhawk-mission")
def pixhawk_mission(vehicle_id: str):
    """Fetch the mission currently stored on the vehicle's Pixhawk (view-only). Live
    proxy to Scout — same three-case honesty as control authority:
      unknown vehicle id          → deliberate JSON 404 (no such vehicle)
      known, no Scout API          → 200 available:false (nothing to read here)
      known, Scout API configured  → live proxy; reachable:false + empty list if Scout
                                      does not answer (never a console 5xx)."""
    now = datetime.now(timezone.utc)
    vid = parse_vehicle_id(vehicle_id)
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": vehicle_id})

    base = scout_api_base(vid)
    if base is None:
        return {
            "ok": True, "vehicle_id": vid, "available": False, "reachable": False,
            "fetched_at": now.isoformat(), "count": 0, "current_seq": None,
            "waypoints": [], "partial": False, "source": "scout",
            "reason": "No Scout mission API configured for this vehicle",
        }
    return _scout_mission_read(vid, base, now)


# --- Set Home (deployment: set + read-back-verify the Pixhawk HOME_POSITION) ---
# The lake-deployment workflow: with the Scout floating at a safe recovery point and a
# valid GPS fix, the operator commits its CURRENT position as the Pixhawk HOME / RTL
# recovery point. This is a synchronous Scout proxy (like control authority / pixhawk
# mission), NOT the async local-agent queue — but it still threads the normal command
# record (uuid id, timestamps, lifecycle) so it appears in the command history and the
# result is EXECUTED only on Scout's read-back verification, never on the click.
#
# Scout-side contract (to be exposed by motherpi/services/flask, mirroring
# /agent/control_authority): POST /agent/set_home  body { lat, lng }  ->
#   { accepted: bool, verified: bool, home: { lat, lng }, distance_m: float,
#     error_code?: str, reason?: str }
# where `verified` means Scout re-read HOME_POSITION back off the Pixhawk and it matched.
# Until Scout ships it, this endpoint returns an honest scout_unavailable failure and the
# operator UI never claims Home was set.

# Structured failure vocabulary the UI branches on (E5). Scout's own error_code (if it
# sends one) is normalized onto these; otherwise we derive from accepted/verified.
_SET_HOME_CODE_ALIASES = {
    "NO_GPS": "gps_unavailable", "GPS_UNAVAILABLE": "gps_unavailable", "NO_FIX": "gps_unavailable",
    "STALE": "position_stale", "POSITION_STALE": "position_stale",
    "UNREACHABLE": "scout_unavailable", "PIXHAWK_UNREACHABLE": "scout_unavailable",
    "REJECTED": "command_rejected", "DENIED": "command_rejected", "COMMAND_REJECTED": "command_rejected",
    "ACK_TIMEOUT": "ack_timeout", "NO_ACK": "ack_timeout",
    "READBACK_TIMEOUT": "readback_timeout", "NO_READBACK": "readback_timeout",
    "OUT_OF_TOLERANCE": "verification_out_of_tolerance",
    "VERIFICATION_FAILED": "verification_out_of_tolerance",
}
_SET_HOME_MESSAGES = {
    "gps_unavailable": "No valid GPS fix — Scout could not provide a home position.",
    "position_stale": "Scout position is stale — wait for a current, valid GPS fix.",
    "scout_unavailable": "Pixhawk/Scout unreachable — Home was not set.",
    "command_rejected": "Pixhawk rejected the set-home command.",
    "ack_timeout": "No acknowledgement from the Pixhawk — Home was not confirmed set.",
    "readback_timeout": "Pixhawk did not read HOME back — set could not be verified.",
    "verification_out_of_tolerance": "Home read back too far from the commanded point — not verified.",
}


def _fail_set_home(cmd, code, now, *, detail=None, distance_m=None, home=None):
    """Mark the SET_HOME command REJECTED/FAILED with a structured code and return the
    UI-facing failure body. command_rejected is a REJECTED; everything else is a FAILED."""
    status = "REJECTED" if code == "command_rejected" else "FAILED"
    message = _SET_HOME_MESSAGES.get(code, "Set Home failed.")
    if cmd is not None:
        apply_command_result(cmd, status, None, message, now)
        _command_event(cmd, severity="warning",
                       message=f"Set Home failed — {message}", source="operator-backend")
    body = {"ok": False, "verified": False, "phase": "failed", "code": code,
            "message": message, "distance_m": distance_m, "home": home}
    if detail:
        body["detail"] = detail
    if cmd is not None:
        body["command"] = cmd
    return body


@app.post("/api/vehicles/{vehicle_id}/commands/set-home")
async def set_home(vehicle_id: str, request: Request):
    """Set + read-back-verify the Pixhawk HOME_POSITION at the deployment site.
    Body: { lat, lng, confirm }. `lat`/`lng` are the Scout's CURRENT position (the
    recovery point); confirm:true is required (this is a deliberate, non-one-click
    action). Never reports success before Scout's read-back verification succeeds."""
    now = datetime.now(timezone.utc)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(body, dict):
        body = {}

    vid = parse_vehicle_id(vehicle_id)
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": vehicle_id})

    if not bool(body.get("confirm")):
        return JSONResponse(status_code=409, content={
            "ok": False, "needs_confirmation": True, "type": "SET_HOME",
            "message": "Setting Home moves the RTL recovery point and requires explicit "
                       "confirmation. Resend with confirm:true."})

    lat = _mission_coord(body.get("lat"), body.get("latitude"))
    lng = _mission_coord(body.get("lng"), body.get("lon"), body.get("longitude"))
    comm_state = comms_state_by_id.get(vid, "UNKNOWN")

    # Create the SET_HOME command record up front so even a validation failure is tracked.
    cmd = make_command(vid=vid, ctype="SET_HOME",
                       params={"lat": lat, "lng": lng}, created_by=body.get("created_by"),
                       comm_state=comm_state, now=now)
    cmd["warning"] = RISK_WARNING["SET_HOME"]
    _command_event(cmd, severity="caution",
                   message=f"Set Home requested ({comm_state})", source="operator-backend")

    # GPS present + fresh (validated before we ever touch Scout).
    if lat is None or lng is None:
        return JSONResponse(status_code=422, content=_fail_set_home(cmd, "gps_unavailable", now))
    if comm_state != "CONNECTED":
        return JSONResponse(status_code=409, content=_fail_set_home(
            cmd, "position_stale", now, detail=f"comm_state={comm_state}"))

    base = scout_api_base(vid)
    if base is None:
        return JSONResponse(status_code=409, content=_fail_set_home(
            cmd, "scout_unavailable", now, detail="no Scout API configured for this vehicle"))

    try:
        r = requests.post(f"{base}/agent/set_home", json={"lat": lat, "lng": lng}, timeout=8)
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        return JSONResponse(status_code=502, content=_fail_set_home(
            cmd, "scout_unavailable", now, detail=str(exc)))
    if not isinstance(data, dict):
        data = {}

    # Normalize Scout's response.
    raw_code = str(data.get("error_code") or data.get("code") or "").upper()
    code = _SET_HOME_CODE_ALIASES.get(raw_code)
    accepted = data.get("accepted")
    verified = bool(data.get("verified"))
    rb = data.get("home") if isinstance(data.get("home"), dict) else {}
    home_lat = _mission_coord(rb.get("lat"), rb.get("latitude")) if rb else None
    home_lng = _mission_coord(rb.get("lng"), rb.get("lon"), rb.get("longitude")) if rb else None
    home_out = {"lat": home_lat, "lng": home_lng} if home_lat is not None and home_lng is not None else None
    distance_m = data.get("distance_m")
    if distance_m is None and home_out:
        distance_m = haversine_m(lat, lng, home_lat, home_lng)

    # Scout signalled an explicit failure code, or did not accept the command.
    if code:
        return JSONResponse(status_code=502, content=_fail_set_home(
            cmd, code, now, detail=data.get("reason"), distance_m=distance_m, home=home_out))
    if accepted is False:
        return JSONResponse(status_code=502, content=_fail_set_home(
            cmd, "command_rejected", now, detail=data.get("reason"),
            distance_m=distance_m, home=home_out))
    if not verified:
        return JSONResponse(status_code=502, content=_fail_set_home(
            cmd, "readback_timeout", now, detail=data.get("reason"),
            distance_m=distance_m, home=home_out))
    # Verified by Scout, but the operator backend still enforces the tolerance itself —
    # never trust a "verified" flag that read back kilometres away.
    if distance_m is not None and distance_m > HOME_VERIFY_TOLERANCE_M:
        return JSONResponse(status_code=502, content=_fail_set_home(
            cmd, "verification_out_of_tolerance", now,
            detail=f"{distance_m} m > {HOME_VERIFY_TOLERANCE_M} m tolerance",
            distance_m=distance_m, home=home_out))

    # Success: record verification (fleet payload picks it up) + mark the command EXECUTED.
    verified_at = now.isoformat()
    home_verification_by_id[vid] = {
        "verified": True, "verified_at": verified_at,
        "lat": home_lat if home_lat is not None else lat,
        "lng": home_lng if home_lng is not None else lng,
        "distance_m": distance_m,
    }
    apply_command_result(cmd, "EXECUTED", data, None, now)
    _append_event(severity="info",
                  message=f"Home verified ({distance_m} m from Scout)"
                          if distance_m is not None else "Home verified",
                  etype="command", source="operator-backend",
                  vehicle_id=vid, vehicle=name_of(vid))
    return {
        "ok": True, "verified": True, "phase": "verified", "vehicle_id": vid,
        "home": home_out or {"lat": lat, "lng": lng}, "distance_m": distance_m,
        "verified_at": verified_at, "tolerance_m": HOME_VERIFY_TOLERANCE_M,
        "command": cmd,
    }


@app.get("/api/commands/history/{vehicle_id}")
def command_history(vehicle_id: str):
    """Terminal (completed) commands for one vehicle, newest first — the command log."""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    vid = parse_vehicle_id(vehicle_id)
    items = [c for c in commands
             if c["vehicle_id"] == vid and c["status"] in TERMINAL_STATUSES]
    items.reverse()
    return {"vehicle_id": vid, "commands": items, "generated_at": now.isoformat()}


@app.get("/api/commands/{vehicle_id}")
def vehicle_commands(vehicle_id: str):
    """Every command for one vehicle (active queue + history), newest first — the UI view."""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    vid = parse_vehicle_id(vehicle_id)
    items = [c for c in commands if c["vehicle_id"] == vid]
    items.reverse()
    active = [c for c in items if c["status"] not in TERMINAL_STATUSES]
    return {"vehicle_id": vid, "commands": items, "active": active,
            "generated_at": now.isoformat()}


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
        # Same carry-forward for the agent reasoning group (payload.agent.*), and record
        # any agent-decision / mission-state CHANGE as a first-class event (deduped).
        agent_block = payload.get("agent") if isinstance(payload, dict) else None
        if isinstance(agent_block, dict) and agent_block:
            last_known_agent[vid] = dict(agent_block)
        record_agent_changes(vid, payload, now)

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


# Last successful weather read (source freshness / graceful degradation). A transient
# open-meteo failure then shows the last-known values dimmed with an age, rather than
# blanking the widget or — worse — 500ing the whole endpoint.
_env_cache = {"data": None, "fetched_at": None}
_ENV_KEYS = ("temperature", "weather_code", "wind_speed", "wind_direction")


def safe_local_time():
    """Local wall-clock string that never raises. ZoneInfo needs the tz database, which
    is absent on some hosts (no `tzdata`, no system zoneinfo) — there it raises
    ZoneInfoNotFoundError. Falling back to a fixed Europe/Stockholm offset (CET/CEST is
    a display nicety, not safety data) keeps the endpoint from 500ing on those hosts."""
    try:
        return datetime.now(ZoneInfo("Europe/Stockholm")).strftime("%H:%M:%S")
    except Exception:
        # Approximate CET/CEST without tz data: UTC+1, +2 during summer DST months.
        now = datetime.now(timezone.utc)
        offset = 2 if 3 <= now.month <= 10 else 1
        return (now + timedelta(hours=offset)).strftime("%H:%M:%S")


@app.get("/api/environment")
def environment():
    """Weather/wind for the Map overlay. ALWAYS returns the same stable schema and
    NEVER 500s — missing or unreachable sensors become null values with an
    `available`/freshness indication, so the frontend can hide or dim the widget
    instead of erroring. `available` = this response carries a real reading;
    `stale` + `source_age_s` mark a served-from-cache reading after a fetch failure."""
    now = datetime.now(timezone.utc)
    lat, lng = 56.699893, 13.002148
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        "&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m"
        "&timezone=Europe%2FStockholm"
    )

    base = {"local_time": safe_local_time(), "generated_at": now.isoformat()}

    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        current = (r.json() or {}).get("current", {}) or {}
        data = {
            "temperature": current.get("temperature_2m"),
            "weather_code": current.get("weather_code"),
            "wind_speed": current.get("wind_speed_10m"),
            "wind_direction": current.get("wind_direction_10m"),
        }
        _env_cache["data"] = data
        _env_cache["fetched_at"] = now
        return {**base, **data, "available": True, "stale": False,
                "source": "open-meteo", "source_age_s": 0}
    except Exception as e:
        # Never 500: serve last-known (dimmed, with age) if we have it, else all-null.
        cached = _env_cache["data"]
        if cached:
            age = (now - _env_cache["fetched_at"]).total_seconds() if _env_cache["fetched_at"] else None
            return {**base, **cached, "available": False, "stale": True,
                    "source": "open-meteo", "source_age_s": round(age, 1) if age is not None else None,
                    "error": str(e)}
        return {**base, **{k: None for k in _ENV_KEYS}, "available": False,
                "stale": False, "source": "open-meteo", "source_age_s": None,
                "error": str(e)}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


# New operator station (design-system frontend). Served alongside the classic
# dashboard at "/" during incremental migration — existing routes are unchanged.
app.mount("/app", StaticFiles(directory=BASE_DIR / "operator", html=True), name="operator")

app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")