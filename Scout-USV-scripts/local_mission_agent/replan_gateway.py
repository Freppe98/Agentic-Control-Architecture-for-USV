"""
The one adapter through which the replan controller performs vehicle
operations -- always via the existing vehicle Flask service, NEVER a parallel
direct-MAVLink path (requirement 1).

Every method is a thin HTTP call to an endpoint that already exists and already
does verification on the Flask side:

  current_authority()  GET  /agent/control_authority   -> fresh authority string
  home_verified()      GET  /agent/home_status          -> verified & ready_for_auto
  command_loiter()     POST /nav/loiter                 -> mode_verification result
  command_auto()       POST /nav/AutoModeOn             -> mode_verification result
  command_rtl()        POST /nav/rtl                    -> mode_verification result
  upload_mission()     POST /agent/upload_mission       -> mission-contract result
  read_vehicle_state() GET  /agent/state                -> full telemetry/mission/agent
  pixhawk_mission_readback()  GET /agent/pixhawk_mission -> cache-first readback
  prove_pixhawk_mission_readback() GET /agent/pixhawk_mission?refresh=1 -> fresh proof
  upload_preconditions()  GET /agent/upload_mission/preconditions -> dry-run
                                                            precondition check

The controller depends only on these method names (duck-typed), so tests inject
a fake gateway with no HTTP. The mode endpoints already prove the Pixhawk
actually reached and held the mode (services/mode_verification.py); the upload
endpoint already does the fresh readback and content-hash verification
(services/mission_upload_service.py) -- this adapter adds no verification logic
of its own, it only relays and returns the structured result.
"""
import time
from typing import Any, Dict, List, Optional

import requests

from config import LOCAL_FLASK_URL

# LOITER/AUTO/RTL are verified server-side over a stability window (~seconds);
# the upload runs the whole handshake + readback (tens of seconds). Bounds are
# comfortably longer than the Flask side's own internal caps so a slow-but-
# succeeding operation is never cut off here first.
_MODE_TIMEOUT_S = 15.0
_UPLOAD_TIMEOUT_S = 60.0
_READ_TIMEOUT_S = 5.0
# The live Pixhawk mission readback downloads the whole mission over MAVLink
# (~2.5 s on the bench); it gets its own, wider timeout so a healthy readback is
# never clipped as a transient failure. The fresh-proof variant requests a
# refresh and polls the coordinator's refresh generation, exactly as
# mission_execution_gateway does -- one verified readback path, not a second copy.
_PIXHAWK_READ_TIMEOUT_S = 10.0
_PIXHAWK_PROOF_MAX_WAIT_S = 12.0
_PIXHAWK_PROOF_POLL_INTERVAL_S = 0.25


class FlaskReplanGateway:
    def __init__(self, base_url: str = LOCAL_FLASK_URL):
        self.base_url = base_url

    # ── Reads ───────────────────────────────────────────────────────────────
    def current_authority(self) -> str:
        """Fresh control authority. Raises on failure -- the controller treats a
        failed read as "authority unknown" and fails closed (never LOCAL_AGENT)."""
        r = requests.get(f"{self.base_url}/agent/control_authority", timeout=_READ_TIMEOUT_S)
        r.raise_for_status()
        return r.json()["authority"]

    def home_verified(self) -> bool:
        """True only if Home is runtime-verified AND ready for AUTO. Fails safe
        to False on any error -- an unreachable service is never 'verified'."""
        try:
            r = requests.get(f"{self.base_url}/agent/home_status", timeout=_READ_TIMEOUT_S)
            r.raise_for_status()
            status = r.json()
            return bool(status.get("verified")) and bool(status.get("ready_for_auto"))
        except Exception:
            return False

    def read_vehicle_state(self) -> Dict[str, Any]:
        """The full GET /agent/state document -- telemetry (mode/position/armed),
        mission (identity/progress/mission_active_evidence), and agent (authority/
        home_status). The controller builds its own immutable snapshot from this
        for the shared progression verifier (mission_progression.py), so revised-
        mission progression is proven from authoritative fresh state -- not the
        old one-shot mission_active boolean. Raises on failure; a failed read is
        treated as UNKNOWN (retried within the deadline), never as a failure."""
        r = requests.get(f"{self.base_url}/agent/state", timeout=_READ_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def _get_pixhawk(self, refresh: bool = False) -> Dict[str, Any]:
        params = {"refresh": "1"} if refresh else None
        r = requests.get(f"{self.base_url}/agent/pixhawk_mission",
                         params=params, timeout=_PIXHAWK_READ_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def pixhawk_mission_readback(self) -> Dict[str, Any]:
        """Cache-first GET of the coordinator-served Pixhawk mission readback
        (mission-contract-v1 shape). May return cached evidence -- safety proofs
        use prove_pixhawk_mission_readback() instead. Raises on transport error;
        the controller treats a failed readback as "Pixhawk unavailable" and fails
        closed, never inventing a route hash."""
        return self._get_pixhawk(refresh=False)

    def prove_pixhawk_mission_readback(self,
                                       max_wait_s: float = _PIXHAWK_PROOF_MAX_WAIT_S,
                                       poll_interval_s: float = _PIXHAWK_PROOF_POLL_INTERVAL_S
                                       ) -> Dict[str, Any]:
        """A FRESH, proof-grade Pixhawk mission readback for the pre-replan
        ORIGINAL-mission proof (CRITICAL ISSUE 2). Requests a refresh (capturing
        the pre-refresh refresh_generation) and polls until the coordinator's
        refresh_generation advances AND refreshing clears -- proof a genuinely new
        MAVLink download completed. The caller still applies the full freshness
        gate (planning_package.readback_is_fresh). Runs on the replan transaction
        thread; every underlying GET returns immediately from the coordinator."""
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

    def command_rtl(self) -> Dict[str, Any]:
        r = requests.post(f"{self.base_url}/nav/rtl", timeout=_MODE_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def upload_mission(self, route: List[dict], command_id: str,
                       upload_context: str = "AGENT_REPLAN") -> Dict[str, Any]:
        """POST the revised route to the verified upload service. `route` is the
        operator-route-equivalent (Home is Scout-owned seq 0, excluded).
        upload_context defaults to AGENT_REPLAN (diagnostic/audit metadata only;
        it never bypasses a Flask safety precondition)."""
        waypoints = [
            {"latitude": wp["latitude"], "longitude": wp["longitude"],
             "loiter_time_s": wp.get("loiter_time_s", 0.0) or 0.0}
            for wp in route
        ]
        body = {"command_id": command_id, "waypoints": waypoints, "upload_context": upload_context}
        r = requests.post(f"{self.base_url}/agent/upload_mission", json=body, timeout=_UPLOAD_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def upload_preconditions(self, upload_context: str = "AGENT_REPLAN") -> Dict[str, Any]:
        """Read-only DRY RUN of the armed-LOITER upload safety preconditions
        (GET /agent/upload_mission/preconditions) -- the SAME fail-closed check
        upload_mission() itself performs (services/mission_upload_service.py's
        check_upload_preconditions/_evaluate_preconditions), with no vehicle
        write and no mission-protocol lock involved. Used by the replan
        controller's bounded HOLD-SETTLE wait to prove an armed vehicle has
        physically settled (fresh groundspeed at/below the armed-LOITER
        threshold, itself read back from this same response -- never a second
        local copy of that number) BEFORE spending a real upload attempt.
        Raises on a transport failure -- the controller treats that as "not
        yet provably settled", never as settled."""
        r = requests.get(f"{self.base_url}/agent/upload_mission/preconditions",
                         params={"upload_context": upload_context}, timeout=_READ_TIMEOUT_S)
        r.raise_for_status()
        return r.json()
