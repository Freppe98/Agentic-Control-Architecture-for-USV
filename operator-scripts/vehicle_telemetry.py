"""vehicle_telemetry.py — ONE canonical normalization of a vehicle status payload.

WHY THIS MODULE EXISTS
----------------------
Scout's Local Agent POSTs a rich snapshot to /agent/status: `telemetry`, `power`,
`failsafe`, `imu`, `freshness`, `mavlink`, `mission`, `communication`, `service_status`,
`agent`, `health`, `measurements`. The operator backend stored that envelope verbatim
(`raw_latest`) but only forwarded a SUBSET of it onto the fleet row, so half of the
Vehicle page rendered "— NO TELEM" for values Scout was demonstrably sending. Worse, the
one block that WAS normalized (`mavlink_evidence`) read field spellings Scout does not
use (`connected` instead of `mavlink_connected`), so MAVLink read as unavailable while
the link was up.

The fix is a single normalization layer, per vehicle, that every page reads:

    payload (Scout's words)  →  vehicle_telemetry.*_block()  →  fleet row  →  UI

Rules every function here obeys:

  * NOTHING IS INVENTED. A field Scout does not send is None. There are no defaults,
    no zeros standing in for absence, no "OK" standing in for "not observed".
  * 0 IS A VALUE. `0 A`, `0 ms`, waypoint `0`, `0 %` are real readings and survive
    normalization. Only None/absent means "not available" (see `_num`, which also
    rejects bools so `True` can never read as `1`).
  * SCOUT'S WORDS, NOT OURS. Statuses (`OK`, `RECENT_HANDSHAKE`, `3D_FIX`) pass
    through as tokens. Turning a token into operator prose is the UI's job — the
    backend must never upgrade UNKNOWN into OK on the way past.
  * EVIDENCE TRAVELS WITH THE VERDICT. `mission_present` ships next to
    `readback_available`; `leak_detected` ships next to `polarity`. A consumer can
    always tell "the sensor says X" from "the sensor cannot say anything yet".

MERGE SEMANTICS (see `effective_group`)
---------------------------------------
Scout emits FULL group snapshots, so within a group that is present the packet is
AUTHORITATIVE — a field Scout stopped reporting must disappear, not be resurrected from
an older packet. Carry-forward therefore happens at GROUP level only: a packet that omits
a whole group (a degraded/partial update) keeps the last group that vehicle sent, marked
stale. That is what stops a mission-only or health-only update from erasing power/IMU.

The one documented exception is `telemetry`, whose FIELDS are carried forward in main.py
(`last_known_telemetry`) because MAVLink legitimately drops individual fields mid-stream
and sends battery = -1 for "unknown" — see receive_agent_status.

No FastAPI, no globals, no vehicle-specific state: every stateful piece is an object the
caller keys by vehicle id, so usv-3 can never read or overwrite usv-2.
"""

from collections import deque


# --- primitives -------------------------------------------------------------------

def _num(value):
    """A real number, or None. Bools are NOT numbers (True must never render as 1),
    and 0 / 0.0 pass through untouched — absence is None and nothing else."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if isinstance(value, float) else value
    return None


def _tri(value):
    """A tri-state boolean: True / False / None (not observed). Anything that is not a
    real bool is 'not observed' — a string "false" is not evidence of anything."""
    return value if isinstance(value, bool) else None


def _token(value):
    """An uppercase status token, or None. Scout's own vocabulary, never translated."""
    if value is None:
        return None
    text = str(value).strip()
    return text.upper() if text else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _first(*values):
    """First value that is not None. Used only to accept a documented legacy spelling
    alongside the canonical one — never to guess at a field Scout might have."""
    for v in values:
        if v is not None:
            return v
    return None


def payload_of(message):
    """The payload of an envelope, or the message itself when it IS the payload."""
    if not isinstance(message, dict):
        return {}
    inner = message.get("payload")
    return inner if isinstance(inner, dict) else message


# --- group-level carry-forward ----------------------------------------------------

# Groups whose ABSENCE from a packet means "this partial update did not carry it",
# not "this value was cleared". A packet that omits one keeps the vehicle's last one.
CARRIED_GROUPS = (
    "telemetry", "power", "failsafe", "imu", "freshness", "mavlink",
    "communication", "health", "mission", "service_status", "measurements",
)


def observe_groups(store, vid, payload):
    """Record every group THIS packet actually carried, for THIS vehicle only.

    `store` is {vehicle_id: {group_name: dict}} owned by the caller. An empty group ({})
    is treated as absent: Scout sends nothing rather than an empty dict when it has
    nothing, and storing {} would silently replace a real last-known snapshot."""
    if vid is None:
        return
    slot = store.setdefault(vid, {})
    for name in CARRIED_GROUPS:
        value = payload.get(name)
        if isinstance(value, dict) and value:
            slot[name] = value


def effective_group(store, vid, payload, name):
    """(group, stale) for one group of one vehicle.

    Present in this packet  → (that group, False)  — AUTHORITATIVE, no field merge.
    Absent from this packet → (last one this vehicle sent, True) or ({}, False) if it
                              has never sent one.
    """
    value = payload.get(name)
    if isinstance(value, dict) and value:
        return value, False
    remembered = _dict(store.get(vid, {}).get(name))
    return (remembered, True) if remembered else ({}, False)


# --- power ------------------------------------------------------------------------

def power_block(payload, telemetry=None):
    """Canonical power. `payload.power` is Scout's authoritative block; the legacy
    `telemetry.battery_*` fields are a documented backwards-compatible fallback for a
    Local Agent that predates it (and are the ONLY fallback — nothing else is guessed).

    `battery_remaining_pct` treats -1 as absence: MAVLink's BATTERY_REMAINING sends -1
    for "unknown", and reading that as 0 % or as a real value is how a valid 90 % ends
    up flickering to "—". See main.py's last_known_telemetry for the same rule."""
    power = _dict(payload.get("power"))
    tel = _dict(telemetry if telemetry is not None else payload.get("telemetry"))

    remaining = _first(_num(power.get("battery_remaining_pct")), _num(tel.get("battery")))
    if remaining == -1:
        remaining = None
    source = _token(power.get("source"))
    return {
        "battery_voltage_v": _first(_num(power.get("battery_voltage_v")),
                                    _num(tel.get("battery_voltage"))),
        "battery_current_a": _first(_num(power.get("battery_current_a")),
                                    _num(tel.get("battery_current"))),
        "battery_remaining_pct": remaining,
        "board_voltage_v": _num(power.get("board_voltage_v")),
        "brick_valid": _tri(power.get("brick_valid")),
        "usb_connected": _tri(power.get("usb_connected")),
        "source": source,
        # Which of the two shapes actually answered, so a consumer (and a test) can tell
        # a live canonical reading from a legacy-compatibility one.
        "reported_by": "power" if power else ("telemetry" if tel else None),
    }


# --- failsafe ---------------------------------------------------------------------

def failsafe_block(payload):
    """Canonical failsafe. `status` is Scout's token verbatim — OK / ACTIVE / UNKNOWN /
    whatever it says — and an ABSENT status stays None.

    This is deliberately not collapsed to a boolean. "OK" here means Scout observed no
    active failsafe condition, which is NOT the same claim as "every ArduPilot failsafe
    subsystem has been exhaustively verified", and it is certainly not the same as
    "we received no failsafe telemetry". The UI wording keeps those three apart."""
    fs = _dict(payload.get("failsafe"))
    statustext = _dict(fs.get("last_statustext"))
    return {
        "status": _token(fs.get("status")),
        "system_state": _token(fs.get("system_state")),
        "system_state_num": _num(fs.get("system_state_num")),
        "unhealthy_sensors_present": _tri(fs.get("unhealthy_sensors_present")),
        "unhealthy_sensors_mask": _num(fs.get("unhealthy_sensors_mask")),
        "last_statustext": {
            "text": fs.get("last_statustext", {}).get("text")
                    if isinstance(fs.get("last_statustext"), dict) else None,
            "severity": _num(statustext.get("severity")),
            "age_s": _num(statustext.get("age_s")),
        } if statustext else None,
        "reported": bool(fs),
    }


# --- IMU --------------------------------------------------------------------------

def imu_block(payload):
    """Canonical IMU SUMMARY. The health row wants a verdict (OK / WARNING / …) and an
    age — not the attitude/vibration/clipping dictionary, which belongs on an expanded
    diagnostics view and must never be interpolated into a status line."""
    imu = _dict(payload.get("imu"))
    clipping = imu.get("clipping")
    vib = _dict(imu.get("vibration"))
    return {
        "available": _tri(imu.get("imu_available")),
        "health": _token(imu.get("imu_health")),
        "last_seen_s": _num(imu.get("imu_last_seen_s")),
        "vibration": {"x": _num(vib.get("x")), "y": _num(vib.get("y")),
                      "z": _num(vib.get("z"))} if vib else None,
        "clipping": list(clipping) if isinstance(clipping, (list, tuple)) else None,
        "reported": bool(imu),
    }


# --- freshness --------------------------------------------------------------------

FRESHNESS_KEYS = ("attitude_s", "battery_s", "ekf_s", "gps_s", "heartbeat_s",
                  "mission_s", "position_s", "sys_status_s")


def freshness_block(payload):
    """Per-stream MAVLink observation ages, in seconds. This is what lets the operator
    distinguish VALID from STALE from NEVER-OBSERVED for one stream WITHOUT deleting
    another stream's perfectly valid reading. `oldest_s` is the worst of them."""
    fresh = _dict(payload.get("freshness"))
    out = {k: _num(fresh.get(k)) for k in FRESHNESS_KEYS}
    ages = [v for v in out.values() if v is not None]
    out["oldest_s"] = max(ages) if ages else None
    out["reported"] = bool(fresh)
    return out


# --- MAVLink ----------------------------------------------------------------------

def mavlink_block(payload):
    """Canonical Pixhawk↔Pi MAVLink link evidence.

    THIS IS THE USB/serial link INSIDE the vehicle — never the Scout↔Operator 4G/VPN
    link (see link_block). Conflating them is how a healthy autopilot reads as a comms
    outage, and vice versa.

    The primary availability test is `connected` (Scout's `mavlink_connected`).
    `msg_rate_hz` is SUPPORTING detail and is frequently null even on a perfectly live
    link — it must never be the thing that decides availability. The previous
    implementation read `mav.connected` / `mav.last_msg_age_s`, spellings Scout does not
    send, so every field came out None and the row rendered NO TELEM against a
    connected autopilot."""
    mav = _dict(payload.get("mavlink"))
    comm = _dict(payload.get("communication"))
    health = _dict(payload.get("health"))
    if not mav:
        mav = _dict(comm.get("mavlink")) or _dict(health.get("mavlink"))

    heartbeat = _first(_num(mav.get("heartbeat_age_s")), _num(comm.get("heartbeat_age_s")))
    last_msg = _first(_num(mav.get("mavlink_last_msg_age_s")),
                      _num(mav.get("last_message_age_s")),
                      _num(mav.get("last_msg_age_s")),
                      _num(comm.get("mavlink_last_msg_age_s")))
    return {
        "connected": _first(_tri(mav.get("mavlink_connected")), _tri(mav.get("connected")),
                            _tri(comm.get("mavlink_connected"))),
        "heartbeat_age_s": round(heartbeat, 2) if heartbeat is not None else None,
        "last_msg_age_s": round(last_msg, 2) if last_msg is not None else None,
        "msg_rate_hz": _first(_num(mav.get("mavlink_msg_rate_hz")), _num(mav.get("msg_rate_hz"))),
        "parser_errors": _first(_num(mav.get("parser_errors")), _num(comm.get("mavlink_parser_errors"))),
        "reported": bool(mav),
    }


# --- Scout ↔ Operator link (the 4G / WireGuard path) ------------------------------

def vpn_block(payload):
    """WireGuard tunnel state, or None when the vehicle does not report it.

    WireGuard is connectionless: there is no session to be "connected" to. The only
    honest facts are whether the interface is up and how long ago the last handshake
    was, so the status token (RECENT_HANDSHAKE / STALE / NO_HANDSHAKE / DOWN / UNKNOWN)
    is carried through verbatim and never rewritten as "Connected"."""
    comm = _dict(payload.get("communication"))
    vpn = comm.get("vpn_status")
    if not isinstance(vpn, dict) or not vpn:
        return None
    return {
        "interface": vpn.get("interface"),
        "interface_up": _tri(vpn.get("interface_up")),
        "status": _token(vpn.get("status")) or "UNKNOWN",
        "last_handshake_age_s": _num(vpn.get("last_handshake_age_s")),
        "peers": _num(vpn.get("peers")),
    }


def link_block(payload, packet_loss=None):
    """Scout↔Operator application-link diagnostics: is the operator reachable from the
    vehicle, application round-trip time, VPN state, sequence number, and the OPERATOR's
    own packet-loss estimate.

    These are DIAGNOSTIC inputs. They do NOT define the comm state — CONNECTED /
    PARTITIONED / DISCONNECTED stays derived from status-packet arrival age in main.py,
    which is the thesis's degradation model and must not become a ping or a handshake.

    `operator_connected` is the vehicle's canonical claim that its last POST to the
    operator succeeded. It is a different question from `operator_reachable` (can the
    endpoint be reached at all) and a completely different question from who holds
    control authority — never infer one from the others."""
    comm = _dict(payload.get("communication"))
    rtt = _num(comm.get("rtt_ms"))
    return {
        "operator_connected": _tri(comm.get("operator_connected")),
        "operator_reachable": _tri(comm.get("operator_reachable")),
        "connectivity": _token(comm.get("connectivity")),
        "local_state_available": _tri(comm.get("local_state_available")),
        "rtt_ms": round(rtt, 1) if rtt is not None else None,
        "buffered_packets": _num(comm.get("buffered_packets")),
        "bandwidth_estimate_kbps": _num(comm.get("bandwidth_estimate_kbps")),
        "last_successful_transmission": _num(comm.get("last_successful_transmission")),
        "seq": _num(comm.get("seq")),
        "vpn": vpn_block(payload),
        # The vehicle's own loss figure, kept SEPARATE from ours and normally null: the
        # vehicle cannot know which of its outbound packets we failed to receive.
        "vehicle_reported_packet_loss": _num(comm.get("packet_loss")),
        "packet_loss": packet_loss,
    }


# --- service status ---------------------------------------------------------------

# Services whose absence breaks the operator's ability to command or observe the
# vehicle. Everything else is optional: an unknown optional service must never be
# reported as a fleet-wide failure (influx is a logging sink, not a control path).
REQUIRED_SERVICES = ("local_mission_agent", "vehicle_api", "pixhawk_link")


def service_status_block(payload):
    """Counts + names, never a rendered blob. The UI needs a one-line summary
    ("Nominal", "1 offline") with the detail available on demand; interpolating the raw
    {service: state} dict into a status line is exactly the bug that produced
    "[object Object]" elsewhere on this page."""
    services = _dict(payload.get("service_status"))
    if not services:
        return {"reported": False, "services": {}, "online": [], "offline": [],
                "unknown": [], "required_offline": [], "total": 0}

    online, offline, unknown = [], [], []
    normalized = {}
    for name, state in services.items():
        token = _token(state)
        normalized[name] = token
        if token == "ONLINE":
            online.append(name)
        elif token in ("OFFLINE", "ERROR", "FAILED", "DOWN"):
            offline.append(name)
        else:
            unknown.append(name)
    return {
        "reported": True,
        "services": normalized,
        "online": sorted(online),
        "offline": sorted(offline),
        "unknown": sorted(unknown),
        "required_offline": sorted(n for n in offline if n in REQUIRED_SERVICES),
        "total": len(normalized),
    }


# --- sensors ----------------------------------------------------------------------

def leak_sensor_block(payload):
    """Leak sensor with its CALIBRATION state attached.

    Scout reports the pin is readable (`available: true`, `signal: "LOW"`) but that the
    polarity is `uncalibrated` — nobody has established whether LOW means water or dry.
    So `leak_detected` is null and MUST stay null. Rendering "SAFE" from an uncalibrated
    sensor is the single most dangerous thing this page could do; rendering "NO TELEM"
    is merely wrong, because telemetry plainly exists. The honest state is UNCALIBRATED,
    and `state` names it explicitly so no consumer has to re-derive it."""
    health = _dict(payload.get("health"))
    system = _dict(health.get("system"))
    sensor = _dict(system.get("leak_sensor"))
    legacy = _tri(system.get("leak_detected")) if not sensor else None

    if not sensor:
        if legacy is None:
            return {"state": "UNREPORTED", "available": None, "leak_detected": None,
                    "polarity": None, "signal": None}
        return {"state": "LEAK" if legacy else "NO_LEAK", "available": True,
                "leak_detected": legacy, "polarity": None, "signal": None}

    available = _tri(sensor.get("available"))
    polarity = _token(sensor.get("polarity"))
    detected = _tri(sensor.get("leak_detected"))
    if available is False:
        state = "UNAVAILABLE"
    elif detected is True:
        state = "LEAK"
    elif polarity in (None, "UNCALIBRATED", "UNKNOWN"):
        # Readable but uninterpretable. NOT "no leak".
        state = "UNCALIBRATED"
    elif detected is False:
        state = "NO_LEAK"
    else:
        state = "UNKNOWN"
    return {
        "state": state,
        "available": available,
        "leak_detected": detected,
        "polarity": polarity,
        "signal": _token(sensor.get("signal")),
    }


def sampling_block(payload):
    """Environmental-sampling (sonar / bathymetry / water quality) state.

    Scout says `measurements.sampling.enabled` and `health.system.sensors_enabled`. That
    is provable evidence of "sampling is switched OFF", which is a far more useful thing
    to show than a generic NO TELEM — the operator learns the payload is idle rather
    than that the station is blind."""
    meas = _dict(payload.get("measurements"))
    sampling = _dict(meas.get("sampling"))
    latest = _dict(meas.get("latest"))
    health_system = _dict(_dict(payload.get("health")).get("system"))
    enabled = _first(_tri(sampling.get("enabled")), _tri(health_system.get("sensors_enabled")))
    has_reading = any(_num(v) is not None for v in latest.values())
    return {
        "enabled": enabled,
        "reported": bool(meas) or "sensors_enabled" in health_system,
        "last_sample": sampling.get("last_sample"),
        "has_reading": has_reading,
        "latest": {k: _num(v) for k, v in latest.items()} if latest else {},
    }


# --- mission presence vs readback -------------------------------------------------

def mission_block(payload):
    """Mission facts, with PRESENCE and READBACK kept apart.

    The Vehicle page used to answer both "is a mission loaded?" and "what waypoint?"
    from the operator's own /pixhawk_mission proxy fetch, so before that fetch returned
    both rows said "NOT FETCHED" while Scout was continuously reporting
    `mission_count: 15` and `current_waypoint_display: "0 / 15"`. Those are two different
    questions:

        mission_present      — the autopilot HAS a mission (count > 0). Scout knows this
                               from its own MISSION_COUNT and reports it every packet.
        readback_available   — the FULL item list has been read back and hashed. Needed
                               to draw the route or verify a hash; NOT needed to answer
                               "is a mission loaded".

    A null `current_mission_id` stays null — an onboard mission with no operator-issued
    id is exactly what an externally-loaded mission looks like, and inventing one would
    make it look like ours."""
    mission = _dict(payload.get("mission"))
    readback = _dict(mission.get("pixhawk_readback"))
    count = _num(mission.get("mission_count"))
    return {
        "mission_state": _token(mission.get("mission_state")),
        "mission_active": _tri(mission.get("mission_active")),
        "mission_active_evidence": _token(mission.get("mission_active_evidence")),
        "current_mission_id": mission.get("current_mission_id"),
        "current_waypoint": _num(mission.get("current_waypoint")),
        "current_waypoint_display": mission.get("current_waypoint_display"),
        "mission_count": count,
        "mission_present": None if count is None else count > 0,
        "readback_available": bool(readback) and _num(readback.get("count")) is not None,
        "readback": {
            "count": _num(readback.get("count")),
            "current_seq": _num(readback.get("current_seq")),
            "route_hash": readback.get("route_hash"),
            "age_s": _num(readback.get("age_s")),
            "stale": _tri(readback.get("stale")),
            "mission_valid": _tri(readback.get("mission_valid")),
        } if readback else None,
        "reported": bool(mission),
    }


# --- agent reasoning --------------------------------------------------------------

def agent_summary(payload):
    """The Local Agent's reasoning, flattened to the four scalars the UI shows.

    THE `[object Object]` BUG LIVES HERE. Scout's Flask /agent/state exposes
    `agent.current_policy` as the STRING "FULL_REPORTING", but the Local Agent's
    outbound POST sends it as an OBJECT:

        {"communication_policy": "FULL_REPORTING", "mission_policy": "...",
         "autonomy_level": "ASSISTED", "current_behaviour": "monitoring"}

    The page interpolated that object straight into the DOM, so the operator read
    "[object Object]" where a policy belongs. The same object is ALSO why "Current
    behaviour" read "not emitted": `current_behaviour` is nested inside it, not a
    sibling. Both are fixed by looking in both places, in a documented order, and
    returning STRINGS — an object never leaves this function."""
    agent = _dict(payload.get("agent"))
    policy_raw = agent.get("current_policy")
    policy = _dict(policy_raw) if isinstance(policy_raw, dict) else {}
    policy_name = policy_raw if isinstance(policy_raw, str) else _first(
        policy.get("communication_policy"), policy.get("policy"), policy.get("value"),
        policy.get("name"))

    reason = agent.get("decision_reason")
    if isinstance(reason, (list, tuple)):
        reason = reason[0] if reason else None

    return {
        "current_behaviour": _first(agent.get("current_behaviour"),
                                    policy.get("current_behaviour"),
                                    agent.get("behaviour")),
        "current_decision": agent.get("current_decision"),
        "decision_reason": reason if isinstance(reason, str) else None,
        "current_policy": policy_name if isinstance(policy_name, str) else None,
        "communication_policy": policy.get("communication_policy")
                                if isinstance(policy.get("communication_policy"), str) else None,
        "mission_policy": policy.get("mission_policy")
                          if isinstance(policy.get("mission_policy"), str) else None,
        "autonomy_level": _first(agent.get("autonomy_level"), policy.get("autonomy_level")),
        "control_authority": _token(agent.get("control_authority")),
    }


# --- packet-loss estimator --------------------------------------------------------
#
# WHAT "PACKET LOSS" MEANS HERE
# -----------------------------
# The fraction of the Local Agent's OUTBOUND status messages, over a recent window,
# that never arrived at this operator station. Nothing else. In particular it is NOT
# ArduPilot's SYS_STATUS.drop_rate_comm, which measures the Pixhawk↔Pi serial link and
# says nothing whatsoever about the 4G/WireGuard path this number is about.
#
# Only the RECEIVER can measure this: Scout cannot know which of its own sends we
# failed to receive. Scout therefore stamps each status message with a monotonic
# `communication.seq`, and the arithmetic happens here.
#
# The estimator is deliberately conservative — every ambiguous case degrades to
# "unmeasured", never to a large invented loss figure.

PACKET_LOSS_WINDOW_S = 120.0    # how much history one estimate covers
PACKET_LOSS_MAX_SAMPLES = 400   # hard cap so a fast reporter cannot grow it without bound
PACKET_LOSS_MIN_SAMPLES = 20    # below this the answer is "not enough samples", not "0 %"
PACKET_LOSS_MAX_FORWARD_JUMP = 1000  # a jump this large is a counter reinit, not 99.9 % loss


class PacketLossEstimator:
    """Per-vehicle sequence-gap loss estimate over a rolling time window.

    Owned and keyed by the caller (`{vehicle_id: PacketLossEstimator}`), so two vehicles
    never share a window — usv-3's sequence numbers can neither inflate nor mask usv-2's.

    Behaviour on the awkward cases, all of which produce a HONEST answer rather than a
    dramatic one:

      duplicate seq     counted once (a retransmit is not a second delivery, and must
                        not make received exceed expected)
      out-of-order seq  fills its gap retroactively; a late arrival inside the window
                        REDUCES the estimate and can never manufacture loss
      counter restart   a seq below the whole window means the Local Agent restarted its
                        counter — the window is discarded and measurement starts over
                        (an agent restart is not a 100 % loss event)
      huge forward jump likewise treated as a reinit, not as thousands of lost packets
      long outage       old samples age out of the window, so a reconnect reports
                        "not enough samples" until the window refills, rather than
                        reporting the disconnection as loss
      too few samples   state UNMEASURED with loss_pct None — never a fabricated 0 %
    """

    def __init__(self, window_s=PACKET_LOSS_WINDOW_S, max_samples=PACKET_LOSS_MAX_SAMPLES,
                 min_samples=PACKET_LOSS_MIN_SAMPLES):
        self.window_s = window_s
        self.max_samples = max_samples
        self.min_samples = min_samples
        self._samples = deque()      # (seq, monotonic-ish timestamp), ascending in time
        self._seen = set()           # seq values currently inside the window
        self.duplicates = 0
        self.resets = 0

    # -- ingest --
    def observe(self, seq, now):
        """Record one arrival. `seq` may be None (a vehicle that does not stamp them —
        the estimator then simply never becomes measurable) and `now` is seconds."""
        if not isinstance(seq, (int, float)) or isinstance(seq, bool):
            return
        seq = int(seq)
        self._evict(now)
        if self._seen:
            newest = self._samples[-1][0] if self._samples else None
            oldest = min(self._seen)
            if seq in self._seen:
                self.duplicates += 1
                return
            # Below everything we still hold: either a counter restart or a packet so
            # late its slot has already aged out. Both are "start measuring again".
            if seq < oldest:
                self._reset()
            elif newest is not None and seq > newest + PACKET_LOSS_MAX_FORWARD_JUMP:
                self._reset()
        self._samples.append((seq, now))
        self._seen.add(seq)
        while len(self._samples) > self.max_samples:
            old_seq, _ = self._samples.popleft()
            self._seen.discard(old_seq)

    def _reset(self):
        self._samples.clear()
        self._seen.clear()
        self.resets += 1

    def _evict(self, now):
        cutoff = now - self.window_s
        while self._samples and self._samples[0][1] < cutoff:
            old_seq, _ = self._samples.popleft()
            self._seen.discard(old_seq)

    # -- read --
    def estimate(self, now):
        """The current estimate, as a block safe to publish straight onto a fleet row."""
        self._evict(now)
        samples = len(self._seen)
        base = {
            # ASCII only: this string travels through JSON into logs and a Windows console.
            "meaning": "Local Agent -> Operator status messages lost in the last "
                       f"{int(self.window_s)}s",
            "samples": samples,
            "min_samples": self.min_samples,
            "window_s": self.window_s,
            "duplicates": self.duplicates,
            "resets": self.resets,
        }
        if samples < self.min_samples:
            return {**base, "state": "UNMEASURED", "loss_pct": None,
                   "expected": None, "received": None, "lost": None}
        newest = max(self._seen)
        oldest = min(self._seen)
        expected = newest - oldest + 1
        if expected <= 0:
            return {**base, "state": "UNMEASURED", "loss_pct": None,
                    "expected": None, "received": None, "lost": None}
        lost = max(0, expected - samples)
        return {**base, "state": "MEASURED",
                "loss_pct": round(100.0 * lost / expected, 1),
                "expected": expected, "received": samples, "lost": lost}
