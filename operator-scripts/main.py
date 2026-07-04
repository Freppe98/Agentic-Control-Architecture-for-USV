from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import requests
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import time

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent

latest_agent_status = {}
latest_agent_received_at = None


FLEET_TEMPLATE = [
    {
        "id": 1,
        "name": "USV-1",
        "online": False,
        "status": "UNKNOWN",
        "battery": None,
        "comms": "No data",
        "comm_state": "UNKNOWN",
        "heading": 0,
        "speed": None,
        "mission": "Unknown",
        "coverage": None,
        "lat": 56.699893,
        "lng": 13.002148,
        "agent": {},
        "telemetry": {},
    },
    {
        "id": 2,
        "name": "Scout",
        "online": False,
        "status": "LOST",
        "battery": None,
        "comms": "Lost",
        "comm_state": "UNKNOWN",
        "heading": 0,
        "speed": None,
        "mission": "Unknown",
        "coverage": None,
        "lat": 56.699893,
        "lng": 13.002148,
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
        "heading": 0,
        "speed": None,
        "mission": "Unknown",
        "coverage": None,
        "lat": 56.699493,
        "lng": 13.001548,
        "agent": {},
        "telemetry": {},
    },
]


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

    usv_id_raw = payload.get("usv_id", payload.get("id", 2))
    try:
        usv_id = int(str(usv_id_raw).replace("usv-", ""))
    except Exception:
        usv_id = 2

    comm_state = payload.get("comm_state", "UNKNOWN")
    battery = telemetry.get("battery", payload.get("battery"))

    lat = telemetry.get("lat", payload.get("lat", 56.699893))
    lng = telemetry.get("lng", payload.get("lng", 13.002148))

    # Avoid map jumping to 0,0 if GPS is not valid yet.
    if not lat or not lng:
        lat = 56.699893
        lng = 13.002148

    return {
        "id": usv_id,
        "name": payload.get("name", "Scout"),
        "online": True,
        "status": payload.get("mission_state", telemetry.get("mode", "ACTIVE")),
        "battery": battery if battery != -1 else None,
        "comms": comm_state,
        "comm_state": comm_state,
        "heading": telemetry.get("heading", 0),
        "speed": telemetry.get("groundspeed", telemetry.get("speed")),
        "mission": payload.get("mission", payload.get("mission_state", "Unknown")),
        "coverage": payload.get("coverage"),
        "lat": lat,
        "lng": lng,
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


@app.post("/agent/status")
async def receive_agent_status(request: Request):
    global latest_agent_status, latest_agent_received_at

    latest_agent_status = await request.json()
    latest_agent_received_at = datetime.now(timezone.utc).isoformat()

    print("[OPERATOR] Received agent status:")
    print(latest_agent_status)

    return {
        "ok": True,
        "message": "status received",
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


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")