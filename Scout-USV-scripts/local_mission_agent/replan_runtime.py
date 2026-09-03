"""
Process-local registry that lets the Local Agent's inbound HTTP server
(agent_server.py, running on its own daemon thread) reach the single live
ReplanController + EnergyPolicy that main()'s loop owns.

Both live in the same process; this is the same "shared module holds the live
object" pattern runtime_status.py already uses for the main-loop heartbeat. The
controller is registered once at startup, before the HTTP server starts, so a
status/config/reset request always finds it.

Config changes flow through here so they are applied atomically to BOTH the
controller and the energy policy, and are refused while a transaction is in
flight (never mid-transaction). Runtime overrides are in-memory only
(replan_config._runtime_overrides) -- they never rewrite the deployment
environment.
"""
import threading

import replan_config

_lock = threading.Lock()
_controller = None
_energy = None


def register(controller, energy) -> None:
    global _controller, _energy
    with _lock:
        _controller = controller
        _energy = energy


def get_controller():
    return _controller


def get_energy():
    return _energy


def resolved_config() -> dict:
    """Current resolved config values + per-field source, plus the patchable
    subset and its bounds -- the GET /agent/replan/config body."""
    cfg, sources = replan_config.resolve()
    return {
        "values": cfg.to_dict(),
        "sources": sources,
        "patchable": replan_config.patchable_fields(),
    }


def apply_config_patch(body: dict) -> "tuple":
    """
    Validate + apply a runtime config patch. Returns (result_or_None,
    error_code, error_message).

    Refused (never deferred silently) while a transaction is active: changing
    thresholds/flags mid-transaction could alter its behaviour partway through.
    On success the override is stored in-memory and applied live to the
    controller and energy policy, and the fully resolved config (with sources)
    is returned.
    """
    cleaned, err_code, err_msg = replan_config.validate_patch(body)
    if cleaned is None:
        return None, err_code, err_msg

    with _lock:
        controller = _controller
        energy = _energy
        if controller is not None and controller.is_running():
            return None, "TRANSACTION_ACTIVE", (
                "a replan transaction is in progress; configuration changes are refused "
                "until it reaches a terminal state"
            )
        replan_config.apply_overrides(cleaned)
        cfg, sources = replan_config.resolve()
        # Apply live. ReplanConfig is frozen, but the attribute holding it is not.
        if controller is not None:
            controller.cfg = cfg
        if energy is not None:
            energy.cfg = cfg

    print(f"[REPLAN] runtime config patched: {cleaned}")
    try:
        import transition_log
        transition_log.record_transition(
            "replan_config", "runtime_patch", "applied", f"patched {sorted(cleaned)}")
    except Exception:
        pass
    return {"applied": cleaned, "values": cfg.to_dict(), "sources": sources}, None, None
