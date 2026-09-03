"""
Read-only preflight / bench-readiness verification for a repeatable Scout
single-USV experiment.

    python3 bench_preflight.py            # human summary + JSON to stdout
    python3 bench_preflight.py --json     # JSON only (for thesis evidence capture)

WHAT THIS IS
------------
A GET-only gate that answers one question -- "is Scout in a known-good state to
start a repeatable bench run?" -- and records the evidence for it. Every check
is an observation; nothing here commands or mutates the vehicle. There is no
ARM, no mode change, no Set Home, no mission upload/clear, no authority write,
and no service restart. It reads:

  * mavlink2rest (127.0.0.1:6040) HEARTBEAT / GLOBAL_POSITION_INT -- liveness
    proven directly from the message cache, so a stale-but-present link can't
    pass. Two HEARTBEAT samples prove the counter is *advancing*, not merely
    that a cached message exists.
  * vehicle Flask (8080) /agent/state, /agent/control_authority,
    /agent/pixhawk_mission, /agent/home_status
  * the Local Agent's own persistent mission-operation record (in-process read)
  * the Local Agent inbound HTTP surface (8090), if the agent is running
  * operator reachability (config.OPERATOR_URLS)
  * the outbound buffer (surfaced read-only -- NEVER drained or dropped here)

Each check is a pure function over already-fetched data, so it is trivially
mockable in test_bench_preflight.py; run_all() does the I/O and hands the
results to those pure functions, the same split the rest of this service uses
(decision_engine / mavlink_health are pure over fetched envelopes).

EXIT CODE: 0 if overall PASS (no FAIL), 1 otherwise.
"""
import json
import re
import sys
import time
from datetime import datetime, timezone

import requests

import config
import mission_operation_status
from mission_operation_status import INTERRUPTIBLE_STATES, STATE_DELIVERING_RESULT
from api_client import (
    get_vehicle_state,
    get_control_authority,
    get_pixhawk_mission,
    get_home_status,
)
from buffer import read_buffered_messages

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

EXPECTED_CONTRACT_VERSION = "mission-contract-v1"
# Contract fields the operator's Pixhawk Mission card relies on. current_seq
# and route_waypoint_count are legitimately 0 on an empty/home-only mission, so
# they are checked for presence (is not None), never truthiness.
REQUIRED_MISSION_CONTRACT_FIELDS = (
    "contract_version",
    "mission_valid",
    "pixhawk_item_count",
    "route_waypoint_count",
    "route_content_hash",
    "full_mission_hash",
    "current_seq",
    "generation",
)


# ── result + freshness helpers ────────────────────────────────────────────────

def _result(name, status, summary, evidence=None):
    return {
        "name": name,
        "status": status,
        "summary": summary,
        "evidence": evidence or {},
    }


def _last_update_epoch(envelope):
    """Unix epoch of a mavlink2rest envelope's status.time.last_update
    (RFC3339, nanosecond precision), or None. Mirrors the vehicle Flask side's
    services/mavlink_message_utils.last_update_epoch -- kept local rather than
    importing across the Flask package boundary, since this process only ever
    reaches mavlink2rest for these bench liveness reads."""
    try:
        last_update = envelope["status"]["time"]["last_update"]
    except (KeyError, TypeError):
        return None
    ts = re.sub(r"\.(\d{6})\d*Z?$", r".\1+00:00", last_update)
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _message_age_s(envelope):
    """Seconds since mavlink2rest last cached this message, or None if the
    timestamp is missing/unparseable -- callers must treat None as
    'freshness unprovable', never 'assume fresh'."""
    epoch = _last_update_epoch(envelope)
    if epoch is None:
        return None
    return datetime.now(timezone.utc).timestamp() - epoch


# ── I/O fetchers (mockable; each degrades to (None, error) on failure) ─────────

def _read_mavlink_message(message_type):
    url = (
        f"{config.MAVLINK2REST_URL}"
        f"/mavlink/vehicles/1/components/1/messages/{message_type}"
    )
    r = requests.get(url, timeout=3)
    r.raise_for_status()
    return r.json()


def _probe_http(url, timeout=2):
    """True if `url` answers with a non-5xx status, else False. Used for the
    operator and Local-Agent reachability checks -- a 4xx still proves the
    listener is up, which is all reachability asks."""
    try:
        r = requests.get(url, timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def _local_agent_running():
    """True if a local_agent.py process is running on this host. Kept separate
    from the 8090 probe so 'not running' (SKIP) is distinguishable from
    'running but its HTTP surface is down' (FAIL)."""
    import subprocess

    try:
        r = subprocess.run(
            ["pgrep", "-f", "[l]ocal_agent.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except Exception:
        return False


def _safe(fetch, *args):
    try:
        return fetch(*args), None
    except Exception as e:
        return None, str(e)


# ── pure checks (data in, result out; no I/O) ─────────────────────────────────

def check_heartbeat_counter(sample_a, sample_b, fetch_error=None):
    if fetch_error or sample_a is None or sample_b is None:
        return _result(
            "heartbeat_counter_advancing", FAIL,
            f"could not read HEARTBEAT from mavlink2rest ({fetch_error})",
        )
    try:
        counter_a = sample_a["status"]["time"]["counter"]
        counter_b = sample_b["status"]["time"]["counter"]
    except (KeyError, TypeError):
        return _result(
            "heartbeat_counter_advancing", FAIL,
            "HEARTBEAT envelope has no status.time.counter",
        )
    advancing = counter_b > counter_a
    return _result(
        "heartbeat_counter_advancing",
        PASS if advancing else FAIL,
        f"mavlink2rest HEARTBEAT counter {counter_a} -> {counter_b} "
        f"({'advancing' if advancing else 'NOT advancing'})",
        {"counter_before": counter_a, "counter_after": counter_b},
    )


def check_heartbeat_age(sample, fetch_error=None):
    if fetch_error or sample is None:
        return _result(
            "heartbeat_age", FAIL,
            f"could not read HEARTBEAT from mavlink2rest ({fetch_error})",
        )
    age = _message_age_s(sample)
    threshold = config.BENCH_HEARTBEAT_MAX_AGE_S
    if age is None:
        return _result(
            "heartbeat_age", FAIL,
            "HEARTBEAT freshness unprovable (no parseable last_update)",
            {"threshold_s": threshold},
        )
    age = round(age, 2)
    ok = age < threshold
    return _result(
        "heartbeat_age",
        PASS if ok else FAIL,
        f"heartbeat age {age}s "
        f"({'<' if ok else '>='} {threshold}s threshold)",
        {"age_s": age, "threshold_s": threshold},
    )


def check_position_age(sample, fetch_error=None):
    if fetch_error or sample is None:
        return _result(
            "position_age", FAIL,
            f"could not read GLOBAL_POSITION_INT from mavlink2rest ({fetch_error})",
        )
    age = _message_age_s(sample)
    threshold = config.BENCH_POSITION_MAX_AGE_S
    if age is None:
        return _result(
            "position_age", FAIL,
            "GLOBAL_POSITION_INT freshness unprovable (no parseable last_update)",
            {"threshold_s": threshold},
        )
    age = round(age, 2)
    ok = age < threshold
    return _result(
        "position_age",
        PASS if ok else FAIL,
        f"position age {age}s "
        f"({'<' if ok else '>='} {threshold}s threshold)",
        {"age_s": age, "threshold_s": threshold},
    )


def check_disarmed(telemetry, fetch_error=None):
    if fetch_error or telemetry is None:
        return _result(
            "disarmed", FAIL,
            f"could not read vehicle state ({fetch_error})",
        )
    armed = telemetry.get("armed")
    if armed is None:
        return _result("disarmed", FAIL, "armed state unknown (no HEARTBEAT base_mode)")
    return _result(
        "disarmed",
        FAIL if armed else PASS,
        "vehicle is ARMED" if armed else "vehicle is disarmed",
        {"armed": armed},
    )


def check_mode_manual(telemetry, fetch_error=None):
    if fetch_error or telemetry is None:
        return _result(
            "mode_manual", FAIL,
            f"could not read vehicle state ({fetch_error})",
        )
    mode = telemetry.get("mode_name")
    if mode is None:
        return _result("mode_manual", FAIL, "vehicle mode unknown")
    ok = mode == "MANUAL"
    return _result(
        "mode_manual",
        PASS if ok else FAIL,
        f"mode is {mode}" + ("" if ok else " (expected MANUAL)"),
        {"mode_name": mode},
    )


def check_flask(state, fetch_error=None):
    if fetch_error or state is None:
        return _result(
            "flask_8080", FAIL,
            f"vehicle Flask /agent/state unreachable ({fetch_error})",
        )
    return _result(
        "flask_8080", PASS, "vehicle Flask (8080) /agent/state responds",
        {"state_timestamp": state.get("state_timestamp")},
    )


def check_local_agent(running, responded):
    if not running:
        return _result(
            "local_agent_8090", SKIP,
            "Local Agent process not running -- 8090 check skipped",
            {"running": False},
        )
    return _result(
        "local_agent_8090",
        PASS if responded else FAIL,
        "Local Agent (8090) responds"
        if responded
        else "Local Agent process is running but 8090 does not respond",
        {"running": True, "responded": responded},
    )


def check_operator(reachability):
    """`reachability` is an ordered list of (url, reachable) tuples."""
    any_reachable = any(ok for _, ok in reachability)
    evidence = {"endpoints": {url: ok for url, ok in reachability}}
    if not reachability:
        return _result("operator_reachable", FAIL, "no operator endpoints configured", evidence)
    return _result(
        "operator_reachable",
        PASS if any_reachable else FAIL,
        "operator reachable at "
        + ", ".join(url for url, ok in reachability if ok)
        if any_reachable
        else "no configured operator endpoint is reachable",
        evidence,
    )


def check_authority(authority, fetch_error=None):
    if fetch_error or authority is None:
        return _result(
            "authority_operator", FAIL,
            f"could not read control authority ({fetch_error})",
        )
    ok = authority == "OPERATOR"
    return _result(
        "authority_operator",
        PASS if ok else FAIL,
        f"control authority is {authority}"
        + ("" if ok else " (expected OPERATOR)"),
        {"authority": authority},
    )


def check_mission_readback(pix, fetch_error=None):
    if fetch_error or pix is None:
        return _result(
            "mission_readback", FAIL,
            f"pixhawk mission readback failed ({fetch_error})",
        )
    if pix.get("error"):
        return _result(
            "mission_readback", FAIL,
            f"pixhawk mission readback error: {pix.get('error')}",
        )
    missing = [f for f in REQUIRED_MISSION_CONTRACT_FIELDS if pix.get(f) is None]
    version = pix.get("contract_version")
    evidence = {
        "contract_version": version,
        "mission_valid": pix.get("mission_valid"),
        "pixhawk_item_count": pix.get("pixhawk_item_count"),
        "route_waypoint_count": pix.get("route_waypoint_count"),
        "route_content_hash": pix.get("route_content_hash"),
        "full_mission_hash": pix.get("full_mission_hash"),
        "current_seq": pix.get("current_seq"),
        "generation": pix.get("generation"),
        "reachable": pix.get("reachable"),
        "partial": pix.get("partial"),
        "stale": pix.get("stale"),
        "cached": pix.get("cached"),
        "missing_contract_fields": missing,
    }
    problems = []
    if version != EXPECTED_CONTRACT_VERSION:
        problems.append(f"contract_version={version!r} (expected {EXPECTED_CONTRACT_VERSION!r})")
    if missing:
        problems.append(f"null contract fields: {missing}")
    if pix.get("reachable") is False:
        problems.append("pixhawk unreachable")
    if pix.get("partial"):
        problems.append("readback is partial")
    if pix.get("mission_valid") is False:
        problems.append("mission_valid is False")
    if problems:
        return _result("mission_readback", FAIL, "; ".join(problems), evidence)
    if pix.get("stale") or pix.get("cached"):
        return _result(
            "mission_readback", WARN,
            "contract complete but served from cache/stale readback",
            evidence,
        )
    return _result(
        "mission_readback", PASS,
        f"mission readback complete: {pix.get('pixhawk_item_count')} items / "
        f"{pix.get('route_waypoint_count')} route waypoints, contract fields present",
        evidence,
    )


def check_home(home, fetch_error=None):
    if fetch_error or home is None:
        return _result(
            "home_verification", FAIL,
            f"could not read home status ({fetch_error})",
        )
    if home.get("error"):
        return _result("home_verification", FAIL, f"home status error: {home.get('error')}")
    if home.get("reachable") is False:
        return _result("home_verification", FAIL, "home service unreachable")
    verified = home.get("verified")
    evidence = {
        "verified": verified,
        "ready_for_auto": home.get("ready_for_auto"),
        "ready_for_rtl": home.get("ready_for_rtl"),
        "verification_distance_m": home.get("verification_distance_m"),
        "verification_method": home.get("verification_method"),
        "verified_at": home.get("verified_at"),
        "reason": home.get("reason"),
    }
    if verified:
        return _result(
            "home_verification", PASS,
            f"home verified (distance {home.get('verification_distance_m')}m, "
            f"method {home.get('verification_method')})",
            evidence,
        )
    # Not verified is surfaced as a WARNING rather than a hard fail: Set Home is
    # a deliberate operator step performed at the start of a run, so an
    # unverified home before that step is expected, not a fault.
    return _result(
        "home_verification", WARN,
        f"home NOT verified ({home.get('reason')}) -- Set Home before the run",
        evidence,
    )


def check_no_unresolved_mission_op(record):
    state = record.get("state")
    error = record.get("error") or {}
    evidence = {
        "state": state,
        "command_id": record.get("command_id"),
        "command_type": record.get("command_type"),
        "error_code": error.get("code"),
    }
    if error.get("code") == "UNKNOWN_AFTER_RESTART":
        return _result(
            "no_unresolved_mission_op", FAIL,
            "mission operation is UNKNOWN_AFTER_RESTART -- vehicle mission state "
            "is indeterminate; a fresh operator retry is required before running",
            evidence,
        )
    if state in INTERRUPTIBLE_STATES or state == STATE_DELIVERING_RESULT:
        return _result(
            "no_unresolved_mission_op", FAIL,
            f"a mission operation is still in flight (state {state})",
            evidence,
        )
    if state == mission_operation_status.STATE_FAILED:
        return _result(
            "no_unresolved_mission_op", WARN,
            "last mission operation FAILED (resolved, outcome known)",
            evidence,
        )
    return _result(
        "no_unresolved_mission_op", PASS,
        f"no unresolved mission operation (state {state})",
        evidence,
    )


def check_outbound_buffer(buffered_messages, retained_result_ids):
    """Surface the outbound buffer read-only. This NEVER drains, retries, or
    drops anything -- per the bench protocol, buffered terminal results are
    retained until the operator's retry/drop contract is settled (see
    OUTBOUND_BUFFER_REVIEW.md). Stuck command_results are reported as a WARNING
    so they are visible in the evidence without being acted on here."""
    buffered_result_ids = [
        (m.get("payload") or {}).get("command_id")
        for m in buffered_messages
        if isinstance(m, dict) and m.get("message_type") == "command_result"
    ]
    evidence = {
        "buffered_messages_total": len(buffered_messages),
        "buffered_command_results": len(buffered_result_ids),
        "buffered_command_result_ids": [i for i in buffered_result_ids if i],
        "retained_command_result_ids": list(retained_result_ids),
        "note": "read-only surface; no buffered data dropped or drained here",
    }
    if buffered_result_ids or retained_result_ids:
        return _result(
            "outbound_buffer", WARN,
            f"{len(buffered_result_ids)} buffered + {len(retained_result_ids)} retained "
            "command_result(s) awaiting operator ack (not dropped)",
            evidence,
        )
    return _result(
        "outbound_buffer", PASS,
        "outbound buffer holds no undelivered command results",
        evidence,
    )


# ── orchestration ─────────────────────────────────────────────────────────────

def _operator_reachability():
    return [
        (url, _probe_http(f"{url}/agent/status", timeout=config.OPERATOR_CONNECT_TIMEOUT))
        for url in config.OPERATOR_URLS
    ]


def _retained_command_result_ids():
    """Ids in command_results.json (persisted authoritative terminal results
    still awaiting operator ack). Read-only; nothing is cleared."""
    try:
        with open(config.COMMAND_RESULTS_FILE, "r") as f:
            return list(json.load(f).keys())
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        return []


def run_all(sample_interval=None):
    """Perform every check and return the aggregated evidence record. Does the
    I/O, then delegates each verdict to a pure check function above."""
    if sample_interval is None:
        sample_interval = config.BENCH_HEARTBEAT_SAMPLE_INTERVAL_S

    checks = []

    # mavlink2rest liveness -- two HEARTBEAT samples to prove the counter moves.
    hb_a, hb_a_err = _safe(_read_mavlink_message, "HEARTBEAT")
    time.sleep(sample_interval)
    hb_b, hb_b_err = _safe(_read_mavlink_message, "HEARTBEAT")
    pos, pos_err = _safe(_read_mavlink_message, "GLOBAL_POSITION_INT")

    checks.append(check_heartbeat_counter(hb_a, hb_b, hb_a_err or hb_b_err))
    checks.append(check_heartbeat_age(hb_b, hb_b_err))
    checks.append(check_position_age(pos, pos_err))

    # vehicle Flask state
    state, state_err = _safe(get_vehicle_state)
    telemetry = (state or {}).get("telemetry", {}) if state else None
    checks.append(check_flask(state, state_err))
    checks.append(check_disarmed(telemetry, state_err))
    checks.append(check_mode_manual(telemetry, state_err))

    authority, auth_err = _safe(get_control_authority)
    checks.append(check_authority(authority, auth_err))

    pix, pix_err = _safe(get_pixhawk_mission)
    checks.append(check_mission_readback(pix, pix_err))

    home, home_err = _safe(get_home_status)
    checks.append(check_home(home, home_err))

    # Local Agent inbound HTTP surface (only meaningful if the process is up).
    running = _local_agent_running()
    responded = _probe_http(
        f"http://127.0.0.1:{config.LOCAL_AGENT_HTTP_PORT}/agent/diagnostics", timeout=3
    ) if running else False
    checks.append(check_local_agent(running, responded))

    checks.append(check_operator(_operator_reachability()))

    checks.append(check_no_unresolved_mission_op(mission_operation_status.get()))

    checks.append(check_outbound_buffer(read_buffered_messages(), _retained_command_result_ids()))

    return summarize(checks)


def summarize(checks):
    counts = {PASS: 0, FAIL: 0, WARN: 0, SKIP: 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    overall = FAIL if counts[FAIL] else PASS
    return {
        "schema": "scout-bench-preflight-v1",
        "generated_at": round(time.time(), 2),
        "usv_id": config.USV_ID,
        "usv_name": config.USV_NAME,
        "overall": overall,
        "ready": counts[FAIL] == 0,
        "counts": counts,
        "checks": checks,
    }


def _print_human(record):
    print("=== Scout bench preflight -- read-only readiness check ===")
    print(f"{record['usv_name']} ({record['usv_id']})  "
          f"generated_at={record['generated_at']}")
    print()
    for c in record["checks"]:
        print(f"  [{c['status']:<4}] {c['name']}: {c['summary']}")
    counts = record["counts"]
    print()
    print(f"  PASS={counts[PASS]} FAIL={counts[FAIL]} "
          f"WARN={counts[WARN]} SKIP={counts[SKIP]}")
    print()
    print(f"OVERALL: {record['overall']}")
    print()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    json_only = "--json" in argv

    record = run_all()

    if json_only:
        print(json.dumps(record, indent=2))
    else:
        _print_human(record)
        print("--- JSON (thesis evidence) ---")
        print(json.dumps(record, indent=2))

    return 0 if record["overall"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
