"""
The one adapter through which the mission-execution controller performs vehicle
operations -- always via the existing vehicle Flask service, NEVER a parallel
direct-MAVLink path (same rule as replan_gateway.py).

Every method is a thin HTTP call to an endpoint that already exists and already
does its own verification on the Flask side:

  current_authority()      GET  /agent/control_authority -> fresh authority string
  read_vehicle_state()     GET  /agent/state             -> full telemetry/mission/agent
  home_status()            GET  /agent/home_status        -> verified Home + readiness
  pixhawk_mission_readback() GET /agent/pixhawk_mission   -> fresh live mission readback
  command_loiter()         POST /nav/loiter               -> mode_verification result
  command_auto()           POST /nav/AutoModeOn           -> mode_verification result
  set_home(command_id)     POST /agent/set_home           -> verified Set Home result

The controller depends only on these method names (duck-typed), so tests inject
a fake gateway with no HTTP. This adapter adds NO verification logic of its own:
the mode endpoints already prove the Pixhawk reached and held the mode
(services/mode_verification.py), and the Set Home endpoint already reads Home
back and computes the verification distance (services/set_home_service.py). It
only relays and returns the structured result.

Note the mission-execution controller deliberately reuses the SAME verified
LOITER/AUTO/Set Home endpoints the replan gateway and the operator command path
already use -- there is exactly one verified implementation of each vehicle
write in this system, not a second copy (task section 11).
"""
import time
from typing import Any, Dict, List, Optional

import requests

from config import LOCAL_FLASK_URL

_MODE_TIMEOUT_S = 15.0
_SET_HOME_TIMEOUT_S = 30.0
_READ_TIMEOUT_S = 5.0
# The full upload handshake + verified readback (tens of seconds); the same
# verified /agent/upload_mission service the replan path uses (one upload
# implementation, not a second copy). Used by Stop to RESTORE the immutable
# original mission when a revised safe-return route is installed.
_UPLOAD_TIMEOUT_S = 60.0
# The live Pixhawk mission readback downloads the whole mission over MAVLink and
# is legitimately the slowest read on this surface (measured ~2.5 s on the bench);
# it gets its own, wider timeout so a healthy readback is never clipped as a
# transient failure. Still bounded so a genuinely hung download fails closed.
_PIXHAWK_READ_TIMEOUT_S = 10.0
# Bounds for the fresh-proof readback (prove_pixhawk_mission_readback): GET
# /agent/pixhawk_mission is non-blocking on the Flask side, so a safety proof
# must REQUEST a refresh and then poll until the coordinator's refresh
# generation advances (proof a genuinely new download completed) and refreshing
# clears. This polling runs on the Local Agent's own background/command thread,
# NEVER blocking a gunicorn request thread.
_PIXHAWK_PROOF_MAX_WAIT_S = 12.0
_PIXHAWK_PROOF_POLL_INTERVAL_S = 0.25


class FlaskMissionExecutionGateway:
    def __init__(self, base_url: str = LOCAL_FLASK_URL):
        self.base_url = base_url

    # ── Reads ───────────────────────────────────────────────────────────────
    def current_authority(self) -> str:
        """Fresh control authority. Raises on failure -- the controller treats a
        failed read as "authority unknown" and fails closed (never LOCAL_AGENT)."""
        r = requests.get(f"{self.base_url}/agent/control_authority", timeout=_READ_TIMEOUT_S)
        r.raise_for_status()
        return r.json()["authority"]

    def read_vehicle_state(self) -> Dict[str, Any]:
        """The full GET /agent/state document -- telemetry (mode/position/armed),
        mission (identity/progress), and agent (authority/home_status). The
        controller builds its own immutable snapshot from this, so Start/Pause/
        Resume derive authoritative state themselves rather than trusting a
        stale value passed from the caller."""
        r = requests.get(f"{self.base_url}/agent/state", timeout=_READ_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def home_status(self) -> Dict[str, Any]:
        """Read-only Home verification/readiness. Fails safe to an unverified
        stub on any error -- an unreachable service is never 'verified'."""
        try:
            r = requests.get(f"{self.base_url}/agent/home_status", timeout=_READ_TIMEOUT_S)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {"reachable": False, "verified": False, "ready_for_auto": False}

    def _get_pixhawk(self, refresh: bool = False) -> Dict[str, Any]:
        params = {"refresh": "1"} if refresh else None
        r = requests.get(f"{self.base_url}/agent/pixhawk_mission",
                         params=params, timeout=_PIXHAWK_READ_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def pixhawk_mission_readback(self) -> Dict[str, Any]:
        """A read-only GET of the coordinator-served Pixhawk mission readback
        (mission-contract-v1 shape: reachable / partial / mission_valid /
        route_content_hash / route_waypoint_count / mission_id, now also
        cached / stale / refreshing / age_s / observed_at / refresh_generation).

        This is a CACHE-FIRST display read -- it may return cached evidence.
        Safety proofs must use prove_pixhawk_mission_readback() instead. Raises
        on a transport failure; the controller treats a failed readback as
        "Pixhawk unavailable" and fails closed, never inventing a route hash."""
        return self._get_pixhawk(refresh=False)

    def prove_pixhawk_mission_readback(self,
                                       max_wait_s: float = _PIXHAWK_PROOF_MAX_WAIT_S,
                                       poll_interval_s: float = _PIXHAWK_PROOF_POLL_INTERVAL_S
                                       ) -> Dict[str, Any]:
        """A FRESH, proof-grade Pixhawk mission readback for the Start identity
        proof / READY evaluation.

        GET /agent/pixhawk_mission is non-blocking, so this requests a refresh
        (`?refresh=1`, capturing the pre-refresh refresh_generation) and then
        polls the endpoint until the coordinator's refresh_generation advances
        past that value AND refreshing has cleared -- proof that a genuinely new
        MAVLink download actually completed as a result of this request, not
        that a stale cache happened to look young. Returns that fresh readback
        (the caller still applies the full freshness gate -- reachable / valid /
        non-partial / not stale / age within the proof limit -- via
        planning_package.readback_is_fresh + build_readiness).

        If the refresh does not complete within max_wait_s, returns the latest
        readback observed (still flagged refreshing/stale), which the caller's
        freshness gate rejects -- a transient not-ready, never a false READY.

        Runs on the Local Agent's own thread (readiness worker / command
        handler); it NEVER blocks a gunicorn request thread -- every underlying
        GET returns immediately from the Flask coordinator."""
        latest = self._get_pixhawk(refresh=True)
        baseline_gen = latest.get("refresh_generation")
        deadline = time.monotonic() + max_wait_s
        while True:
            gen = latest.get("refresh_generation")
            advanced = (isinstance(gen, int)
                        and (baseline_gen is None or gen > baseline_gen))
            if advanced and not latest.get("refreshing"):
                return latest
            if time.monotonic() >= deadline:
                return latest
            time.sleep(poll_interval_s)
            latest = self._get_pixhawk(refresh=False)

    # ── Writes (each already verified on the Flask side) ──────────────────────
    def command_loiter(self) -> Dict[str, Any]:
        r = requests.post(f"{self.base_url}/nav/loiter", timeout=_MODE_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def command_auto(self) -> Dict[str, Any]:
        r = requests.post(f"{self.base_url}/nav/AutoModeOn", timeout=_MODE_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def command_arm(self) -> Dict[str, Any]:
        """Arm the vehicle via the existing verified /nav/ArmOn route. That route
        (services/arm_verification.py) sends MAV_CMD_COMPONENT_ARM_DISARM and
        proves the vehicle actually reached and HELD armed=true from fresh
        HEARTBEAT base_mode evidence before ever reporting verified -- a sent
        request or a bare COMMAND_ACK is never conflated with armed. Returns
        {accepted, verified, armed, ack_result, reason, error, samples, ...}.
        The controller STILL independently re-reads fresh vehicle state and
        requires armed=true there before continuing to AUTO (task section 11:
        an acknowledgement alone is not sufficient)."""
        r = requests.post(f"{self.base_url}/nav/ArmOn", timeout=_MODE_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def set_mission_current(self, seq: int) -> Dict[str, Any]:
        """Rewind (or jump) the Pixhawk mission progression to `seq` via the
        existing /nav/jump_to_waypoint route (MAV_CMD_DO_SET_MISSION_CURRENT).

        This route only sends the command -- it does NOT itself verify the
        sequence actually changed. The mission-execution controller therefore
        NEVER trusts this call's acknowledgement alone: after it, the controller
        independently polls FRESH vehicle state until the mission sequence is
        proven at the start (task: verify fresh mission state/readback/sequence
        evidence, do not assume the command succeeded from ACK alone)."""
        r = requests.post(f"{self.base_url}/nav/jump_to_waypoint",
                          json={"seq": int(seq)}, timeout=_MODE_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def upload_mission(self, route: List[dict], command_id: str,
                       upload_context: str = "AGENT_STOP_RESTORE") -> Dict[str, Any]:
        """Upload `route` (the operator-route-equivalent; Home is Scout-owned seq 0,
        excluded) to the existing verified /agent/upload_mission service, which does
        its own fresh readback + content-hash verification (mission_upload_service).
        Used by Stop to RESTORE the immutable original mission when a revised
        safe-return route is the one currently installed on the Pixhawk. Returns
        {accepted, verified, observed_route_content_hash, observed_route_waypoint_count,
        ...}; the controller STILL independently re-proves the restored route with a
        fresh readback/hash/count before rewinding (task section 11)."""
        waypoints = [
            {"latitude": wp["latitude"], "longitude": wp["longitude"],
             "loiter_time_s": wp.get("loiter_time_s", 0.0) or 0.0}
            for wp in route
        ]
        body = {"command_id": command_id, "waypoints": waypoints, "upload_context": upload_context}
        r = requests.post(f"{self.base_url}/agent/upload_mission", json=body, timeout=_UPLOAD_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def set_control_authority(self, authority: str) -> Dict[str, Any]:
        """Hand supervisory control authority back to `authority` (Stop returns it
        to the OPERATOR once the vehicle is held safely and the reset is verified),
        via the existing POST /agent/control_authority route. Returns the vehicle
        Flask service's confirmed authority document. Raises on transport failure;
        the controller records the returned authority and never fabricates it."""
        r = requests.post(f"{self.base_url}/agent/control_authority",
                          json={"authority": authority}, timeout=_READ_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def set_home(self, command_id: str, tolerance_m: Optional[float] = None,
                 freshness_s: Optional[float] = None) -> Dict[str, Any]:
        """Set Home to the vehicle's current position and verify the read-back,
        via the existing verified Set Home service. `command_id` is required by
        the Flask route (it never sets Home except for an explicit command id).
        Returns {accepted, verified, home_position, verification_distance_m,
        requested_position, ack_result, error}."""
        body = {"command_id": command_id, "mode": "current_position"}
        if tolerance_m is not None:
            body["tolerance_m"] = tolerance_m
        if freshness_s is not None:
            body["freshness_s"] = freshness_s
        r = requests.post(f"{self.base_url}/agent/set_home", json=body, timeout=_SET_HOME_TIMEOUT_S)
        r.raise_for_status()
        return r.json()
