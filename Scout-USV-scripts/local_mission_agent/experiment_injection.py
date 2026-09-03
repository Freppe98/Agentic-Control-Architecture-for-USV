"""
Deterministic, Scout-side experiment injection for the replanning lifecycle.

This is how the first safe-return scenario is triggered on demand without
waiting for a real low battery: an operator/bench test injects a simulated
energy-margin (or battery) override, and the energy policy consumes it exactly
as if it were telemetry -- except it is ALWAYS tagged source=SIMULATED and
surfaced in diagnostics and event logs, so a simulated trigger can never be
confused with a real one.

What the first (E2 energy) scenario needs:
  * force_safe_return  -- force the energy policy to decide REPLAN_SAFE_RETURN.
  * energy_margin_percent -- override the computed safe-return margin.
  * battery_percent    -- override the observed battery percentage.

What the E3 (communication-degradation) scenario needs:
  * communication_state -- override communication.get_comm_state()'s result
    with one of the SAME three values (CONNECTED/PARTITIONED/DISCONNECTED)
    real evidence would otherwise produce this iteration. Consumed by
    communication.resolve_comm_state(), which is the ONLY place that reads
    this field -- everything downstream (risk_model.evaluate_communication,
    decision_policy, the replan FSM) sees an ordinary comm_state string and
    has no idea whether it came from real evidence or this override. This is
    a fallback path for DISCONNECTED specifically: the real Scout<->Operator
    tc/netem impairment (services/flask/services/network_impairment/) can
    deterministically reach PARTITIONED (egress loss/latency on wg0) but
    cannot yet reach a genuine DISCONNECTED (no "full_disconnect" support in
    Stage 1) without also risking other paths over the same interface.

The structure is deliberately open so future GPS / obstacle / upload-failure /
readback-mismatch injections slot in as additional fields without reworking the
store: each is just another optional key an evaluator opts into reading. Those
are NOT implemented here (see reserved_* below, always None).

In-memory only and single-slot (one active injection at a time), same lifetime
convention as control_authority: cleared on process restart, never persisted --
a simulated override must never silently outlive the process that set it.
"""
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Optional

SOURCE_SIMULATED = "SIMULATED"

# A simulated injection must never linger indefinitely. If the caller omits a
# duration, a conservative default is applied; any supplied duration is capped.
DEFAULT_DURATION_S = 300.0     # 5 min
MAX_DURATION_S = 3600.0        # 1 h hard cap
_ENERGY_MARGIN_RANGE = (-100.0, 100.0)
_BATTERY_RANGE = (0.0, 100.0)

# The EXACT same three-value vocabulary communication.py's real get_comm_state()
# produces (CONNECTED/PARTITIONED/DISCONNECTED) -- an injected communication_
# state is never a second, parallel comm-state system, only an alternate SOURCE
# (SIMULATED, see communication.resolve_comm_state) for the identical value real
# evidence would otherwise supply this iteration. Duplicated here as a literal,
# rather than imported from communication.py, so THAT module can import this one
# (to read the active override) without a circular import.
COMMUNICATION_STATES = ("CONNECTED", "PARTITIONED", "DISCONNECTED")

_lock = threading.Lock()
_injection: Optional["Injection"] = None


def validate(body: Any, expected_usv_id: Optional[str]) -> "tuple":
    """
    Validate an Operator-Station injection request. Returns (kwargs, error_code,
    error_message); kwargs is the dict to pass to inject() on success, else None.

    Rules: at least one override present; numeric ranges enforced; target must
    match this Scout (defaults to it when omitted); duration required or the
    conservative default applied and any value hard-capped.
    """
    if not isinstance(body, dict):
        return None, "INVALID_REQUEST", "request body must be a JSON object"

    force = bool(body.get("force_safe_return", False))
    margin = body.get("energy_margin_percent")
    battery = body.get("battery_percent")
    comm_state = body.get("communication_state")

    if not force and margin is None and battery is None and comm_state is None:
        return None, "INVALID_REQUEST", (
            "at least one of force_safe_return, energy_margin_percent, battery_percent, "
            "communication_state is required"
        )

    if comm_state is not None:
        if not isinstance(comm_state, str) or comm_state not in COMMUNICATION_STATES:
            return None, "INVALID_VALUE", (
                f"communication_state must be one of {list(COMMUNICATION_STATES)}"
            )

    if margin is not None:
        if isinstance(margin, bool) or not isinstance(margin, (int, float)):
            return None, "INVALID_VALUE", "energy_margin_percent must be a number"
        if not (_ENERGY_MARGIN_RANGE[0] <= margin <= _ENERGY_MARGIN_RANGE[1]):
            return None, "OUT_OF_BOUNDS", f"energy_margin_percent must be within {_ENERGY_MARGIN_RANGE}"
        margin = float(margin)
    if battery is not None:
        if isinstance(battery, bool) or not isinstance(battery, (int, float)):
            return None, "INVALID_VALUE", "battery_percent must be a number"
        if not (_BATTERY_RANGE[0] <= battery <= _BATTERY_RANGE[1]):
            return None, "OUT_OF_BOUNDS", f"battery_percent must be within {_BATTERY_RANGE}"
        battery = float(battery)

    target = body.get("target_vehicle")
    if target is not None and expected_usv_id is not None and target != expected_usv_id:
        return None, "WRONG_TARGET_USV", (
            f"injection targets {target!r}; this Scout is {expected_usv_id!r}"
        )
    if target is None:
        target = expected_usv_id

    duration = body.get("duration_s")
    if duration is None:
        duration = DEFAULT_DURATION_S
    else:
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
            return None, "INVALID_VALUE", "duration_s must be a positive number"
        duration = min(float(duration), MAX_DURATION_S)

    return {
        "force_safe_return": force,
        "energy_margin_percent": margin,
        "battery_percent": battery,
        "communication_state": comm_state,
        "duration_s": duration,
        "target_vehicle": target,
    }, None, None


def _record(kind: str, message: str) -> None:
    """Record injection lifecycle via the existing transition audit trail so it
    shows up in status like any other transition. Best-effort."""
    try:
        import transition_log
        transition_log.record_transition("experiment", "SIMULATED", kind, message)
    except Exception:
        pass


@dataclass
class Injection:
    created_at: float
    expires_at: Optional[float]
    target_vehicle: Optional[str]
    source: str = SOURCE_SIMULATED
    # First-scenario energy overrides.
    force_safe_return: bool = False
    energy_margin_percent: Optional[float] = None
    battery_percent: Optional[float] = None
    # E3 communication-degradation override -- one of COMMUNICATION_STATES, or
    # None (no override; communication.resolve_comm_state falls through to real
    # evidence). Consumed exactly like battery_percent/energy_margin_percent:
    # the SAME evaluate_communication() in risk_model.py reads whatever value
    # ends up as comm_state, real or injected, with no branching of its own.
    communication_state: Optional[str] = None
    # Reserved for future injections -- present so the shape is stable, always
    # None in this phase (obstacle/gps/upload-failure/readback-mismatch).
    reserved_obstacle: Any = None
    reserved_gps: Any = None
    reserved_upload_failure: Any = None
    reserved_readback_mismatch: Any = None

    def is_active(self, now: float, vehicle_id: Optional[str]) -> bool:
        if self.expires_at is not None and now >= self.expires_at:
            return False
        if self.target_vehicle is not None and vehicle_id is not None:
            if self.target_vehicle != vehicle_id:
                return False
        return True

    def to_dict(self) -> dict:
        return asdict(self)


def inject(
    force_safe_return: bool = False,
    energy_margin_percent: Optional[float] = None,
    battery_percent: Optional[float] = None,
    communication_state: Optional[str] = None,
    duration_s: Optional[float] = None,
    target_vehicle: Optional[str] = None,
    now: Optional[float] = None,
) -> dict:
    """Set (replacing any existing) the single active simulated injection.
    Returns its dict form. `duration_s` sets an expiry so a stale injection
    cannot keep forcing decisions forever; None means until explicitly
    cleared or the process restarts."""
    global _injection
    now = time.time() if now is None else now
    with _lock:
        _injection = Injection(
            created_at=round(now, 3),
            expires_at=None if duration_s is None else round(now + duration_s, 3),
            target_vehicle=target_vehicle,
            force_safe_return=force_safe_return,
            energy_margin_percent=energy_margin_percent,
            battery_percent=battery_percent,
            communication_state=communication_state,
        )
        result = _injection.to_dict()
    _record("applied", f"simulated injection set (expires_at={result['expires_at']}, "
                       f"force={force_safe_return}, margin={energy_margin_percent}, "
                       f"battery={battery_percent}, communication_state={communication_state})")
    return result


def clear() -> bool:
    """Remove any active injection immediately. Idempotent -- returns True if
    something was actually cleared, False if there was nothing to clear."""
    global _injection
    with _lock:
        had = _injection is not None
        _injection = None
    if had:
        _record("cleared", "simulated injection cleared")
    return had


def active(vehicle_id: Optional[str] = None, now: Optional[float] = None) -> Optional[dict]:
    """The active injection for `vehicle_id` as a dict, or None if none is set,
    it has expired, or it targets a different vehicle. Expired injections are
    cleared as a side effect so they stop appearing in status."""
    global _injection
    now = time.time() if now is None else now
    expired = False
    with _lock:
        if _injection is None:
            return None
        if not _injection.is_active(now, vehicle_id):
            # Drop an expired injection so it stops lingering in status; a
            # wrong-target one is left in place (it may still match a different
            # vehicle_id on another call).
            expired = _injection.expires_at is not None and now >= _injection.expires_at
            if expired:
                _injection = None
            result = None
        else:
            result = _injection.to_dict()
    if expired:
        _record("expired", "simulated injection expired and was cleared")
    return result


def status(vehicle_id: Optional[str] = None, now: Optional[float] = None) -> dict:
    """Diagnostics view: whether a simulated injection is active right now and,
    if so, what it overrides. Always marks source=SIMULATED so the operator can
    never mistake it for real telemetry."""
    now = time.time() if now is None else now
    a = active(vehicle_id, now)
    return {
        "active": a is not None,
        "source": SOURCE_SIMULATED,
        "injection": a,
    }
