import os

USV_ID = "usv-2"
USV_NAME = "Scout"

LOCAL_FLASK_URL = "http://127.0.0.1:8080"

# Fallback operator endpoints, used only if neither of the overrides below
# is set. Keep these in sync with whichever operator station is the
# "default" one, but don't rely on editing this list for day-to-day
# switching -- use OPERATOR_URLS env var or local_config.py instead.

DEFAULT_OPERATOR_URLS = [ #(backend moved 8200 -> 8210)
    "http://10.0.0.23:8210",  # Fredrik desktop
    "http://10.0.0.24:8210",  # Fredrik laptop
    "http://10.0.0.25:8210",    # aquality laptop

]


def _parse_operator_urls(value: str):
    return [u.strip() for u in value.split(",") if u.strip()]


def _load_operator_urls():
    """
    Resolve OPERATOR_URLS without requiring a source edit per machine.

    Precedence (highest wins):
      1. OPERATOR_URLS environment variable, comma-separated
      2. local_config.py (gitignored, machine-specific, see
         local_config.example.py)
      3. DEFAULT_OPERATOR_URLS above
    """
    env_value = os.environ.get("OPERATOR_URLS")
    if env_value:
        return _parse_operator_urls(env_value), "environment (OPERATOR_URLS)"

    try:
        import local_config
        local_urls = getattr(local_config, "OPERATOR_URLS", None)
        if local_urls:
            return list(local_urls), "local_config.py"
    except ImportError:
        pass

    return list(DEFAULT_OPERATOR_URLS), "default (config.DEFAULT_OPERATOR_URLS)"


OPERATOR_URLS, OPERATOR_URLS_SOURCE = _load_operator_urls()

CONNECTED_INTERVAL = 1
PARTITIONED_INTERVAL = 10
DISCONNECTED_INTERVAL = 5

OPERATOR_CONNECT_TIMEOUT = 2
OPERATOR_READ_TIMEOUT = 3

# Inbound HTTP server the operator station polls for on-demand diagnostics/
# system_check (GET /agent/diagnostics, POST /agent/system_check) -- separate
# from LOCAL_FLASK_URL above, which is the vehicle Flask service the Local
# Agent calls *out* to. Read-only surface only; see agent_server.py.
LOCAL_AGENT_HTTP_HOST = os.environ.get("LOCAL_AGENT_HTTP_HOST", "0.0.0.0")
LOCAL_AGENT_HTTP_PORT = int(os.environ.get("LOCAL_AGENT_HTTP_PORT", "8090"))

BUFFER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_buffer.jsonl")

# Upper bound on how many undelivered messages buffer.py will hold. Without
# a cap, a send that fails for a reason that will never resolve on its own
# (e.g. the operator backend 404/405ing a real endpoint -- a route mismatch,
# not a connectivity gap) would grow agent_buffer.jsonl without bound, since
# buffer.py's own flush retries forever and never ages anything out. Once
# full, buffer_message() drops the oldest entries first -- the most recent
# backlog is more useful to a reconnecting operator than the oldest.
MAX_BUFFERED_MESSAGES = 500

# Persisted record of operator command_ids already processed, so a command
# re-delivered by the operator backend (e.g. re-queued after a poll retry)
# is never executed twice. Only the most recent N ids are kept -- commands
# carry their own expires_at, so we only need enough history to catch a
# retry of a recently-issued command, not a full audit log.
COMMAND_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "command_log.jsonl")
MAX_TRACKED_COMMAND_IDS = 200

# Persisted authoritative terminal result per operator command_id -- the
# actual command_result payload (status/reason/result/lifecycle), not just
# the bare id COMMAND_LOG_FILE tracks. Lets a redelivered command_id resend
# the exact original result (see command_results.py) instead of fabricating
# a fresh generic "duplicate" rejection on every poll. Survives a Local
# Agent restart, same as COMMAND_LOG_FILE. Entries are removed once the
# operator backend has successfully acknowledged receipt, not on any fixed
# expiry.
COMMAND_RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "command_results.json")

# The single authoritative persistent record of the most recent MISSION_UPLOAD
# or MISSION_CLEAR (see mission_operation_status.py). Holds exactly one
# operation -- the latest -- so the terminal details stay fetchable after the
# ephemeral mission_upload_worker block has reverted to idle, and after a
# process restart. A non-terminal state found in this file at startup is how
# an interrupted MAVLink transaction is detected and failed closed with
# UNKNOWN_AFTER_RESTART rather than silently resumed.
MISSION_OPERATION_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "mission_operation_state.json"
)

# --- Replanning lifecycle (thesis: agent-controlled mission replanning) ------
# The approved planning package the Operator Station hands Scout (original
# route with semantic segment tags, verified Home, optional no-go geometry and
# survey graph, revision metadata). Persisted locally so a safe-return replan
# remains possible while DISCONNECTED from the Operator Station and after a
# Local Agent process restart. See planning_package.py.
PLANNING_PACKAGE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "planning_package.json"
)
# Restart-safe status of the replanning controller (current FSM state, active
# transition id, last revision/outcome). A non-terminal state found here at
# startup means a replan transaction was interrupted -- see
# replan_controller.recover_after_restart(). Behaviour flags/thresholds for the
# feature live in the typed replan_config.ReplanConfig, not here.
REPLAN_STATUS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "replan_status.json"
)

# Restart-safe status of the mission-execution controller (the original mission
# lifecycle: preparation, start, running, pause, resume, return completion). A
# non-terminal *operation* state found here at startup means a Start/Pause/
# Resume/Final-hold was interrupted -- see
# mission_execution_controller.recover_after_restart(), which fails it closed
# (never resumes an interrupted vehicle-write sequence). This is deliberately
# SEPARATE from REPLAN_STATUS_FILE: the mission-execution lifecycle and the
# energy replanning FSM are distinct controllers with distinct persisted state.
MISSION_EXECUTION_STATUS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "mission_execution_status.json"
)

# Decision engine thresholds (decision_engine.py) -- drive current_decision
# and watch_conditions. Battery/GPS numbers match the WARNING/FAIL tiers the
# vehicle Flask service's own diagnostics already use
# (services/diagnostics_service.py: battery WARNING <=30%, GPS FAIL below a
# 2D fix) so the same number doesn't mean two different things on two sides
# of the same status payload.
BATTERY_RTL_THRESHOLD_PERCENT = 30
GPS_MIN_FIX_TYPE = 2  # ArduPilot GPS_FIX_TYPE: 0/1=no fix, 2=2D, 3=3D+
# Mirrors services/mavlink_health.py's HEARTBEAT_TIMEOUT_S on the vehicle
# Flask side (same 1Hz-nominal/3-missed-periods convention). Used only to
# label the watch condition's threshold value -- mavlink_connected itself is
# trusted as-is from payload.mavlink, not recomputed here.
MAVLINK_HEARTBEAT_TIMEOUT_S = 3.0

# --- Obstacle event / graph-detour thresholds (thesis replanning feature) ---
# An obstacle at or within this range is "close": the only safe response is an
# immediate LOITER (station-keep). We do NOT reverse and do NOT replan while
# still moving. Beyond this range the obstacle is "long-range": there is room
# to keep the vehicle where it is and compute a graph-based detour proposal
# before acting. The two experiment injections are ~3 m (close) and ~10 m
# (long-range), so the boundary sits between them.
OBSTACLE_CLOSE_DISTANCE_M = 5.0
# Default lifetime of an injected obstacle event if it doesn't carry its own
# expires_after_s -- a stale detection must not keep forcing a decision.
OBSTACLE_DEFAULT_EXPIRES_AFTER_S = 30

# mavlink2rest REST endpoint. The Local Agent runs on the host, so it reaches
# the daemon on localhost; the Flask container reaches the same daemon via
# host.docker.internal (see services/flask/app.py MAVLINK2REST). Read-only
# use only: bench_preflight.py reads the HEARTBEAT/GLOBAL_POSITION_INT message
# cache directly to prove Pixhawk<->link liveness independently of Flask being
# up. Nothing writes to mavlink2rest through this URL.
MAVLINK2REST_URL = os.environ.get("MAVLINK2REST_URL", "http://127.0.0.1:6040")

# bench_preflight.py freshness thresholds. Heartbeat reuses the same 3s bound
# the rest of the system uses (MAVLINK_HEARTBEAT_TIMEOUT_S). Position is
# allowed to be a little staler -- GLOBAL_POSITION_INT streams slower than
# HEARTBEAT and a few seconds of gap on the bench is not a link fault.
BENCH_HEARTBEAT_MAX_AGE_S = MAVLINK_HEARTBEAT_TIMEOUT_S
BENCH_POSITION_MAX_AGE_S = float(os.environ.get("BENCH_POSITION_MAX_AGE_S", "5.0"))

# Two HEARTBEAT samples this far apart must show an advancing mavlink2rest
# counter (status.time.counter) for the "heartbeat advancing" check. Slightly
# longer than one nominal 1Hz period so at least one beat is guaranteed to land
# between the two samples.
BENCH_HEARTBEAT_SAMPLE_INTERVAL_S = float(
    os.environ.get("BENCH_HEARTBEAT_SAMPLE_INTERVAL_S", "1.5")
)