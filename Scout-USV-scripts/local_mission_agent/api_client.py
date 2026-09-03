import time

import requests
from config import (
    LOCAL_FLASK_URL,
    OPERATOR_URLS,
    OPERATOR_CONNECT_TIMEOUT,
    OPERATOR_READ_TIMEOUT,
)

# Fresh-proof readback (get_pixhawk_mission_proof) polling bounds -- see that
# function. Runs on the Local Agent's own request/worker thread, never a
# gunicorn request thread.
_PIXHAWK_PROOF_MAX_WAIT_S = 12.0
_PIXHAWK_PROOF_POLL_INTERVAL_S = 0.25

# Remembers the last operator URL that worked so we try it first, instead of
# re-probing dead URLs on every message during a flush.
_last_good_url = None


def _ordered_urls():
    if _last_good_url in OPERATOR_URLS:
        return [_last_good_url] + [u for u in OPERATOR_URLS if u != _last_good_url]
    return OPERATOR_URLS


def get_vehicle_state():
    r = requests.get(f"{LOCAL_FLASK_URL}/agent/state", timeout=5)
    r.raise_for_status()
    return r.json()


def get_diagnostics():
    """
    Read-only fetch of the vehicle Flask service's half of Vehicle Health
    diagnostics (mavlink/pixhawk/gps/battery/rc_receiver/camera/
    mission_service/storage/cpu/memory). Raises on failure; the caller
    (diagnostics.py) is responsible for turning that into UNKNOWN entries
    rather than guessing values.
    """
    r = requests.get(f"{LOCAL_FLASK_URL}/agent/diagnostics", timeout=5)
    r.raise_for_status()
    return r.json()


def get_mission():
    """
    Read-only fetch of the mission currently stored on the Pixhawk, via the
    vehicle Flask service's real MAVLink mission-download handshake (see
    services/mission_service.py there). Raises on failure; the caller
    (mission.py) is responsible for degrading to a cached/last-known result
    or an explicit error rather than guessing. Timeout is longer than the
    Flask side's own internal bounded wait (mission_service._OVERALL_TIMEOUT_S,
    20s) so a download that would have finished normally isn't cut off here
    first.
    """
    r = requests.get(f"{LOCAL_FLASK_URL}/agent/mission", timeout=25)
    r.raise_for_status()
    return r.json()


def get_pixhawk_mission():
    """
    Read-only fetch of the mission currently stored on the Pixhawk, in the
    schema the operator station's Pixhawk Mission card consumes
    (mission_loaded/mission_valid/count/current_seq/hash/waypoints/partial)
    -- via the vehicle Flask service's download_pixhawk_mission() (see
    services/mission_service.py there). Raises on failure; the caller
    (pixhawk_mission.py) is responsible for degrading to a cached/
    last-known result or an explicit error rather than guessing. Timeout is
    longer than the Flask side's own internal bounded wait
    (mission_service._OVERALL_TIMEOUT_S, 20s) so a download that would have
    finished normally isn't cut off here first.
    """
    r = requests.get(f"{LOCAL_FLASK_URL}/agent/pixhawk_mission", timeout=25)
    r.raise_for_status()
    return r.json()


def get_pixhawk_mission_proof(max_wait_s: float = _PIXHAWK_PROOF_MAX_WAIT_S,
                              poll_interval_s: float = _PIXHAWK_PROOF_POLL_INTERVAL_S):
    """
    A FRESH, proof-grade Pixhawk mission readback for the replan acceptance /
    route-consistency path. GET /agent/pixhawk_mission is cache-first and
    non-blocking, so a safety proof must request a refresh (`?refresh=1`,
    capturing the pre-refresh refresh_generation) and poll until the
    coordinator's refresh_generation advances and refreshing clears -- proof a
    genuinely new MAVLink download completed. Returns that readback; the caller
    still applies planning_package.readback_is_fresh / verify_pixhawk_consistency
    as the authoritative gate. Never blocks a gunicorn request thread (every
    underlying GET returns immediately from the Flask coordinator); the polling
    happens here on the Local Agent's own thread. Raises on transport failure,
    same contract as get_pixhawk_mission().
    """
    def _get(refresh):
        params = {"refresh": "1"} if refresh else None
        r = requests.get(f"{LOCAL_FLASK_URL}/agent/pixhawk_mission", params=params, timeout=25)
        r.raise_for_status()
        return r.json()

    latest = _get(refresh=True)
    baseline_gen = latest.get("refresh_generation")
    deadline = time.monotonic() + max_wait_s
    while True:
        gen = latest.get("refresh_generation")
        advanced = isinstance(gen, int) and (baseline_gen is None or gen > baseline_gen)
        if advanced and not latest.get("refreshing"):
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(poll_interval_s)
        latest = _get(refresh=False)


def get_control_authority():
    """
    Read the vehicle's current control authority (OPERATOR/LOCAL_AGENT)
    from the vehicle Flask service -- the same process/port as
    get_vehicle_state, not a separate server. Raises on failure; the
    caller (local_agent.py) is responsible for failing safe to OPERATOR
    if this can't be reached, exactly as it would for any other vehicle
    state it can't fetch.
    """
    r = requests.get(f"{LOCAL_FLASK_URL}/agent/control_authority", timeout=3)
    r.raise_for_status()
    return r.json()["authority"]


def set_control_authority(authority: str):
    """Write the vehicle's control authority. Not used by the Local Agent's
    own main loop (it only ever reads) -- provided for tooling/tests that
    need to grant/revoke authority the same way an operator console would."""
    r = requests.post(f"{LOCAL_FLASK_URL}/agent/control_authority", json={"authority": authority}, timeout=3)
    r.raise_for_status()
    return r.json()["authority"]


def get_home_status():
    """
    Read-only fetch of Pixhawk Home verification/readiness -- see
    services/set_home_service.py's get_home_status() there. Internal to
    this process (used by command_executor.home_verified()'s AUTO/RTL/
    RESUME gate); never exposed on this process's own inbound HTTP surface
    -- the Operator Backend, not this call, is what the frontend talks to.
    Raises on failure; callers are responsible for failing safe (never
    assuming verified) rather than guessing.
    """
    r = requests.get(f"{LOCAL_FLASK_URL}/agent/home_status", timeout=5)
    r.raise_for_status()
    return r.json()


def _response_body(response):
    try:
        return response.json()
    except ValueError:
        return response.text


# An unparseable *successful* body is kept only as diagnostic breadcrumb, so it
# is truncated before it reaches a return value or a log line: a 2xx that is
# accidentally an HTML error page or a proxy banner can be arbitrarily large,
# and the Local Agent must never let that size land in a printed line.
MAX_NON_JSON_BODY_CHARS = 200


def _success_body(response):
    """
    Body of a 2xx to return to the caller.

    A 2xx is a terminal acknowledgement on its own -- the HTTP status, not the
    body, is what says "stop retaining" (see OUTBOUND_BUFFER_REVIEW.md). So an
    empty (204) or non-JSON success must not raise out of the success path:
    doing so made the caller buffer an *actually delivered* result and retry it
    forever. The body is instead retained in a safe, explicitly-marked form and
    classified as an ordinary accepted delivery.
    """
    try:
        return response.json()
    except ValueError:
        text = getattr(response, "text", "") or ""
        return {
            "body_format": "non_json",
            "text": text[:MAX_NON_JSON_BODY_CHARS],
        }


# How the Operator acknowledged a delivered command_result. All three are
# terminal -- send_to_operator only returns at all on a 2xx, and a 2xx has
# always meant "stop retaining" (see OUTBOUND_BUFFER_REVIEW.md). They are kept
# distinct because *what happened to the result* differs, and an orphan must
# never be reported as an applied result.
ACK_APPLIED = "applied"
ACK_TERMINAL_ORPHAN = "terminal_orphan"
ACK_ACCEPTED = "accepted"


def classify_command_result_ack(response):
    """
    Classify a successful send_to_operator() return for /agent/command_result.

    Operator commit 6a9214b answers a *present but unknown* command_id with
    HTTP 200 and {"ok": true, "found": false, "applied": false,
    "orphaned": true, "error": "unknown command id"}: the result was not
    applied to any current Operator command, it was archived as an orphaned
    historical audit record, and the acknowledgement is terminal -- Scout
    must stop retrying it.

    Both flags are required, and compared with `is` against the literal
    booleans, so terminal-orphan disposition is only ever read from an
    affirmative Operator statement. A body that merely omits them, carries a
    partial pair, or isn't a JSON object is ACK_ACCEPTED -- an ordinary
    successful delivery, classified as such rather than inferred to be an
    orphan. Orphan is tested before applied so a contradictory body carrying
    both can never be reported as applied.

    A 2xx whose body was empty or unparseable arrives here as the
    `{"body_format": "non_json", ...}` marker from _success_body and lands on
    the same ACK_ACCEPTED path: it carries neither flag, and disposition is
    never read out of response *text*. Only an affirmative JSON statement can
    make a delivery APPLIED or TERMINAL_ORPHAN.
    """
    body = (response or {}).get("response")
    if not isinstance(body, dict):
        return ACK_ACCEPTED
    if body.get("orphaned") is True and body.get("found") is False:
        return ACK_TERMINAL_ORPHAN
    if body.get("applied") is True:
        return ACK_APPLIED
    return ACK_ACCEPTED


def send_to_operator(endpoint, message):
    """
    Raises on failure so the caller buffers `message` for retry (see
    local_agent.py). Two genuinely different failure modes are kept
    distinct in the error text: a 4xx response means the operator *was*
    reached and rejected the request at the protocol level (bad payload,
    wrong route, auth, ...) -- logged immediately with the actual response
    body, since that's a diagnosable bug on this side, not a connectivity
    gap. Everything else (connection refused, timeout, DNS failure, a 5xx)
    is a genuine reachability/availability problem and is reported as
    such. Conflating the two previously made every operator rejection
    print as "No operator reachable", which is actively misleading when
    the operator is up and simply refusing the request.
    """
    global _last_good_url

    errors = []
    protocol_rejection = False
    for base_url in _ordered_urls():
        try:
            r = requests.post(
                f"{base_url}{endpoint}",
                json=message,
                timeout=(OPERATOR_CONNECT_TIMEOUT, OPERATOR_READ_TIMEOUT),
            )
        except requests.exceptions.RequestException as e:
            errors.append(f"{base_url}: {e}")
            continue

        if 400 <= r.status_code < 500:
            body = _response_body(r)
            print(f"[API CLIENT] Operator {base_url}{endpoint} rejected request: "
                  f"HTTP {r.status_code} -- {body}")
            errors.append(f"{base_url}: HTTP {r.status_code} protocol rejection: {body}")
            protocol_rejection = True
            continue

        try:
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            errors.append(f"{base_url}: {e}")
            continue

        _last_good_url = base_url
        body = _success_body(r)
        if isinstance(body, dict) and body.get("body_format") == "non_json":
            print(f"[API CLIENT] Operator {base_url}{endpoint} acknowledged with "
                  f"HTTP {r.status_code} and a non-JSON body (accepted, not retried): "
                  f"{body['text']!r}")
        return {
            "ok": True,
            "operator": base_url,
            "response": body,
        }

    _last_good_url = None
    if protocol_rejection and len(errors) == 1:
        # The only operator configured (or the only one tried) was reached
        # and rejected the request -- never describe that as unreachable.
        raise RuntimeError("Operator rejected request: " + errors[0])
    raise RuntimeError("No operator reachable: " + " | ".join(errors))


def get_pending_commands(usv_id):
    """
    Poll the operator backend for commands queued for this USV.

    Unlike send_to_operator, this does not raise when no operator is
    reachable -- the operator backend is the source of truth for pending
    commands and keeps them queued until the next successful poll, so a
    failed poll here is just "nothing to report this iteration", not an
    error the caller needs to handle (see README: comm DISCONNECTED means
    rely on backend queueing, not local buffering of inbound commands).
    """
    global _last_good_url

    for base_url in _ordered_urls():
        try:
            r = requests.get(
                f"{base_url}/agent/commands",
                params={"usv_id": usv_id},
                timeout=(OPERATOR_CONNECT_TIMEOUT, OPERATOR_READ_TIMEOUT),
            )
            r.raise_for_status()
            _last_good_url = base_url
            return r.json().get("commands", [])
        except Exception:
            continue

    return []
