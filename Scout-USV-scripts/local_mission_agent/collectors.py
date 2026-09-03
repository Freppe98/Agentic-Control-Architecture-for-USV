from config import BUFFER_FILE
from state_machine import MissionState
from communication import wireguard_status


def get_communication_status(comm_state, last_success_ts=None, rtt_ms=None, seq=None, source=None):
    """
    Communication block for the Scout<->Operator link (NOT the Pixhawk<->USB
    link -- that lives in payload.mavlink). Everything here is a real
    measurement of *this* process's link to the operator or an explicit
    unknown:

      * rtt_ms          -- the last measured application-level round-trip of a
                           POST /agent/status (measured in local_agent.py, passed
                           in). None until the first successful send this run; a
                           genuine sub-millisecond 0.x is preserved. This is the
                           communications RTT the thesis cares about, not USB
                           latency.
      * vpn_status      -- structured WireGuard status (services communication.
                           wireguard_status): UP/RECENT_HANDSHAKE/STALE/... plus
                           handshake age, or UNKNOWN if wg can't be read.
      * operator_connected -- whether we can currently reach the operator backend
                           (comm_state CONNECTED). Distinct from the backend
                           merely being "live": this is the vehicle's own view of
                           the link being usable right now.
      * seq             -- monotonic status sequence number. Scout cannot measure
                           its own uplink packet loss; the operator derives loss
                           from gaps in this sequence over a window. packet_loss
                           stays None here (honestly unmeasured on this side)
                           rather than a fabricated 0%.
      * source           -- "REAL" (comm_state came from actual measured
                           evidence) or "SIMULATED" (an active experiment
                           injection override -- see communication.
                           resolve_comm_state, task E3). None when the caller
                           doesn't pass one (pre-E3 call sites) -- never
                           fabricated to "REAL" by default, so an omitted
                           value stays honestly unlabelled rather than
                           silently implying a guarantee this function isn't
                           making.
    """
    return {
        "connectivity": comm_state,
        "operator_reachable": comm_state == "CONNECTED",
        "operator_connected": comm_state == "CONNECTED",
        "last_successful_transmission": last_success_ts,
        "buffered_packets": count_buffered_packets(),
        "rtt_ms": rtt_ms,
        "packet_loss": None,  # unmeasured on this side; operator derives from seq gaps
        "bandwidth_estimate_kbps": None,
        "vpn_status": wireguard_status(),
        "seq": seq,
        "source": source,
    }


def build_service_status(vehicle_state_ok, mavlink_connected, health):
    """
    Health of the critical onboard Scout services, composed entirely from
    evidence the Local Agent already has this iteration -- no per-tick systemctl
    call. Each entry is "online" / "offline" / "unknown":

      * local_mission_agent -- this process; "online" by construction (it is the
        one building and sending this payload).
      * vehicle_api -- the Flask vehicle service; "online" iff GET /agent/state
        succeeded this iteration (vehicle_state_ok).
      * pixhawk_link -- mavlink2rest<->Pixhawk; from the Flask side's measured
        mavlink_connected (True/False/None -> online/offline/unknown).
      * sensor / gpio / influx -- passed through from the Flask health.docker
        probes (already measured there; None -> unknown).
    """
    def _tri(v):
        if v is True:
            return "online"
        if v is False:
            return "offline"
        return "unknown"

    docker = (health or {}).get("docker", {}) if isinstance(health, dict) else {}
    return {
        "local_mission_agent": "online",
        "vehicle_api": "online" if vehicle_state_ok else "offline",
        "pixhawk_link": _tri(mavlink_connected),
        "sensor_service": _tri(docker.get("sensor")),
        "gpio_service": _tri(docker.get("gpio")),
        "influx": _tri(docker.get("influx")),
    }


def get_agent_status(comm_state, mission_state=MissionState.IDLE):
    """
    current_policy/decision_reason/current_behaviour/autonomy_level moved to
    decision_engine.py (build_policy/decide) -- that module is now the single
    source of truth for policy and decision reasoning, evaluated against the
    full observation set (battery/GPS/mavlink/mission/authority), not just
    comm_state. local_agent.py merges its output into this dict.
    """
    return {
        "current_communication_state": comm_state,
        "current_mission_state": mission_state,
        "buffer_usage": count_buffered_packets(),
        "last_operator_command": None,
    }


def count_buffered_packets():
    try:
        with open(BUFFER_FILE, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def policy_for_comm(comm_state):
    if comm_state == "CONNECTED":
        return "FULL_REPORTING"
    if comm_state == "PARTITIONED":
        return "REDUCED_REPORTING_LOCAL_AUTONOMY"
    return "BUFFER_AND_LOCAL_FALLBACK"