import time
from typing import Dict, Any


def make_message(message_type: str, source: str, target: str, payload: Dict[str, Any]):
    return {
        "message_type": message_type,
        "schema_version": "1.0",
        "source": source,
        "target": target,
        "timestamp": time.time(),
        "payload": payload
    }


def make_event(event_type: str, message: str = None, detail: Dict[str, Any] = None, severity: str = "info"):
    """Local-Agent-originated event, shaped like the vehicle's event_log entries."""
    entry = {
        "time": round(time.time(), 2),
        "time_str": time.strftime("%H:%M:%S"),
        "type": event_type,
        "source": "local_agent",
        "severity": severity,
    }
    if message:
        entry["message"] = message
    if detail:
        entry["detail"] = detail
    return entry