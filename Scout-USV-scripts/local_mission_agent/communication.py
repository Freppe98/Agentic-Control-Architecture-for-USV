#VPN alive?

#RTT

#Packet loss

#CONNECTED

#PARTITIONED

#DISCONNECTED

import time
import subprocess
import requests
from config import OPERATOR_URLS, OPERATOR_CONNECT_TIMEOUT, USV_ID

import experiment_injection

# Evidence-source tags for resolve_comm_state() below -- mirrors experiment_
# injection.SOURCE_SIMULATED so the two are always compared against the same
# pair of literals, never a locally-invented third value.
SOURCE_REAL = "REAL"
SOURCE_SIMULATED = experiment_injection.SOURCE_SIMULATED

_vpn_check_warned = False

# WireGuard is connectionless: a peer is "reachable now" only if it has
# handshaked recently. WireGuard rekeys/keepalives on the order of a couple
# minutes, so a handshake within this window is treated as a live link.
WG_RECENT_HANDSHAKE_S = 180
_WG_TTL_S = 10.0
_wg_cache = {"at": 0.0, "value": None}


def internet_ok():
    return subprocess.run(
        ["ping", "-c", "1", "-W", "1", "8.8.8.8"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    ).returncode == 0


def vpn_ok():
    """
    True only if the WireGuard peer has handshaked within
    WG_RECENT_HANDSHAKE_S -- i.e. wireguard_status()'s freshness-aware
    parse reports RECENT_HANDSHAKE. A "latest handshake" value merely being
    *present* is not sufficient: STALE (handshake exists but is older than
    the threshold), NO_HANDSHAKE, DOWN, and UNKNOWN (command failed / wg
    absent / unparseable) all fail closed to False here, the same as no
    evidence at all. This delegates to wireguard_status()/_parse_wg_dump()
    -- the one place handshake age is computed -- rather than re-deriving
    freshness from a second, looser check.
    """
    global _vpn_check_warned
    status = wireguard_status()
    if status["status"] == "UNKNOWN" and not _vpn_check_warned:
        # Distinguish "the command itself failed" (e.g. passwordless sudo
        # isn't set up on this Pi) from "no/stale handshake" -- otherwise
        # this silently misreports every PARTITIONED period as DISCONNECTED
        # with no signal that the check itself is broken.
        _vpn_check_warned = True
        print(
            "[COMM] wireguard_status() came back UNKNOWN ('sudo -n wg show "
            "wg0 dump' failed or produced nothing parseable) -- check "
            "passwordless sudo is configured for wg on this Pi; PARTITIONED "
            "will misreport as DISCONNECTED until this is fixed."
        )
    return status["status"] == "RECENT_HANDSHAKE"


def _parse_wg_dump(stdout: str, now: float, interface: str = "wg0") -> dict:
    """
    Parse `wg show <iface> dump` into a status dict. The dump format is
    tab-separated: the first line is the interface (privkey pubkey port fwmark),
    each subsequent line is a peer (pubkey psk endpoint allowed_ips
    latest_handshake rx tx keepalive), where latest_handshake is a Unix epoch
    (0 == never handshaked). Pure function so it can be unit-tested without wg.

    status:
      * "UNKNOWN"        -- empty output (command produced nothing to parse).
      * "DOWN"           -- interface line present but no peers configured.
      * "NO_HANDSHAKE"   -- peers present, none has ever handshaked.
      * "STALE"          -- most recent handshake age >= WG_RECENT_HANDSHAKE_S.
      * "RECENT_HANDSHAKE" -- most recent handshake age < WG_RECENT_HANDSHAKE_S (link live).
    """
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return {"interface": interface, "interface_up": None,
                "status": "UNKNOWN", "last_handshake_age_s": None, "peers": 0}
    peer_lines = lines[1:]
    handshakes = []
    for ln in peer_lines:
        cols = ln.split("\t")
        if len(cols) >= 5:
            try:
                hs = int(cols[4])
            except ValueError:
                continue
            if hs > 0:
                handshakes.append(hs)
    if not peer_lines:
        status, age = "DOWN", None
    elif not handshakes:
        status, age = "NO_HANDSHAKE", None
    else:
        age = now - max(handshakes)
        # age < threshold = fresh, age >= threshold = stale (exact boundary
        # matters: WG_RECENT_HANDSHAKE_S itself must NOT count as fresh).
        status = "RECENT_HANDSHAKE" if age < WG_RECENT_HANDSHAKE_S else "STALE"
    return {
        "interface": interface,
        "interface_up": True,
        "status": status,
        "last_handshake_age_s": round(age, 1) if age is not None else None,
        "peers": len(peer_lines),
    }


def wireguard_status(interface: str = "wg0") -> dict:
    """
    Structured WireGuard link status for the Scout<->Operator VPN path, cached
    for _WG_TTL_S so the sudo/wg spawn doesn't run on every telemetry tick.
    Reads `sudo -n wg show <iface> dump` (the private key stays hidden in the
    dump; only handshake/transfer counters are read). If the command itself
    can't run -- passwordless sudo not configured, wg absent, interface missing
    -- status is "UNKNOWN" (we genuinely can't tell), never a fabricated "down".
    """
    now = time.time()
    cached = _wg_cache["value"]
    if cached is not None and now - _wg_cache["at"] < _WG_TTL_S:
        return cached
    try:
        result = subprocess.run(
            ["sudo", "-n", "wg", "show", interface, "dump"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            status = {"interface": interface, "interface_up": None,
                      "status": "UNKNOWN", "last_handshake_age_s": None, "peers": 0}
        else:
            status = _parse_wg_dump(result.stdout, now, interface)
    except Exception:
        status = {"interface": interface, "interface_up": None,
                  "status": "UNKNOWN", "last_handshake_age_s": None, "peers": 0}
    _wg_cache.update(at=now, value=status)
    return status


def operator_ok():
    for url in OPERATOR_URLS:
        try:
            r = requests.get(f"{url}/agent/status", timeout=OPERATOR_CONNECT_TIMEOUT)
            if r.status_code < 500:
                return True
        except Exception:
            pass
    return False


def get_comm_state():
    if operator_ok():
        return "CONNECTED"

    if not internet_ok():
        return "DISCONNECTED"

    if vpn_ok():
        return "PARTITIONED"

    return "DISCONNECTED"


def resolve_comm_state(vehicle_id=None, now=None):
    """
    The communication_state this iteration should use, plus which EVIDENCE
    produced it: SOURCE_REAL (the get_comm_state() polling above) or
    SOURCE_SIMULATED (an active experiment_injection.communication_state
    override -- task E3). The override is drawn from the EXACT same three-
    value vocabulary get_comm_state() itself produces -- this is never a
    second, parallel comm-state system, only an alternate SOURCE for the
    identical value real evidence would otherwise supply this iteration;
    every caller downstream (risk_model.evaluate_communication, decision_
    engine, decision_snapshot, the recorder) keeps consuming a plain comm_
    state string exactly as before and has no branch of its own for which
    source produced it.

    Real polling (operator_ok/internet_ok/vpn_ok -- each a subprocess or
    network call) is skipped entirely while an override is active, so a
    synthetic DISCONNECTED trial doesn't also pay their cost only to
    discard the answer. experiment_injection.active() already auto-expires
    and auto-clears a stale override, so the very next poll after expiry
    falls straight through to real evidence with no extra bookkeeping here
    -- this is how "reconnection" (real CONNECTED resuming) happens
    automatically once a synthetic DISCONNECTED/PARTITIONED trial ends.
    """
    vehicle_id = USV_ID if vehicle_id is None else vehicle_id
    injection = experiment_injection.active(vehicle_id, now=now)
    if injection is not None and injection.get("communication_state") is not None:
        return injection["communication_state"], SOURCE_SIMULATED
    return get_comm_state(), SOURCE_REAL


class CommunicationMonitor:
    """
    Tracks the Local Agent's own perceived communication state over time.

    The reported comm_state is always one of CONNECTED / PARTITIONED /
    DISCONNECTED -- that is the model. RECOVERED is not a persisted state,
    it is the edge of transitioning back into CONNECTED after having been
    PARTITIONED or DISCONNECTED, exposed via `just_recovered` so the caller
    can trigger a one-time backlog flush. This holds regardless of whether a
    given state came from real evidence or a SIMULATED experiment override
    (see `source` below, task E3) -- recovery detection itself never needs
    to know which.
    """

    def __init__(self):
        self.state = None
        self.previous_state = None
        self.just_recovered = False
        # "REAL" or "SIMULATED" -- which evidence produced `state` on the
        # most recent poll() (see resolve_comm_state()). None before the
        # first poll().
        self.source = None

    def poll(self) -> str:
        self.previous_state = self.state
        current, source = resolve_comm_state()

        self.just_recovered = (
            self.previous_state in ("PARTITIONED", "DISCONNECTED")
            and current == "CONNECTED"
        )
        self.state = current
        self.source = source
        return current