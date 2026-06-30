from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent

# Hardcoded fleet data for prototype.
# Later these values can come from each USV Pi over Wi-Fi/VPN/4G.
FLEET = [
    {
        "id": 1,
        "name": "USV-1",
        "online": True,
        "status": "AUTO",
        "battery": 82,
        "comms": "Excellent",
        "heading": 35,
        "speed": 1.2,
        "mission": "Survey",
        "coverage": 42,
        "lat": 56.699893,
        "lng": 13.002148,
    },
    {
        "id": 2,
        "name": "USV-2",
        "online": True,
        "status": "STANDBY",
        "battery": 67,
        "comms": "Good",
        "heading": 140,
        "speed": 0.0,
        "mission": "Waiting",
        "coverage": 0,
        "lat": 56.700293,
        "lng": 13.002748,
    },
    {
        "id": 3,
        "name": "USV-3",
        "online": False,
        "status": "LOST",
        "battery": None,
        "comms": "Lost",
        "heading": 270,
        "speed": None,
        "mission": "Unknown",
        "coverage": None,
        "lat": 56.699493,
        "lng": 13.001548,
    },
]


@app.get("/api/fleet/status")
def fleet_status():
    return FLEET


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


# Serve the frontend from the operator-scripts/static directory without shadowing /api routes.
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
